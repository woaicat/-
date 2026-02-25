"""FastAPI application exposing the plan generation endpoints."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import mimetypes
import re
import shutil
import sqlite3
import uuid
import zipfile
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from urllib.parse import quote

from .agent import PlanGenerationAgent
from .config import get_settings
from .document_parser import DocumentAnalyzer
from .file_manager import (
    build_manifest,
    clean_expired_outputs,
    create_request_directory,
    find_request_directory,
    load_manifest_from_directory,
    sanitize_filename,
)
from langgraph.checkpoint.sqlite import SqliteSaver

from .langgraph_demo import (
    CHECKPOINT_PATH as DEMO_CHECKPOINT_PATH,
    build_graph as build_demo_graph,
    _build_initial_state as build_demo_state,
)
from .context_manager import ContextManager
from .langgraph_runner import LangGraphAgentRunner
from .vector_service import VectorService
from .models import (
    DocumentSummary,
    EvaluationRequest,
    GeneratePlanResponse,
    Manifest,
    StepStatus,
    StreamEvent,
    WorkflowNode,
)
from .tool_service import ToolService
from .telemetry import build_langsmith_callbacks


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class TaskState:
    """In-memory registry tracking an ongoing generation task."""

    def __init__(self, request_id: str, product_name: str):
        self.request_id = request_id
        self.product_name = product_name
        self.events: List[StreamEvent] = []
        self.listeners: List[asyncio.Queue[Optional[StreamEvent]]] = []
        self.manifest: Optional[Manifest] = None
        self.error: Optional[str] = None
        self.done = asyncio.Event()
        self.finished_at: Optional[datetime] = None

    async def publish(self, event: StreamEvent) -> None:
        self.events.append(event)
        for queue in self.listeners:
            await queue.put(event)

    async def complete(self) -> None:
        for queue in self.listeners:
            await queue.put(None)
        self.done.set()
        self.finished_at = datetime.utcnow()

    def listen(self) -> asyncio.Queue[Optional[StreamEvent]]:
        queue: asyncio.Queue[Optional[StreamEvent]] = asyncio.Queue()
        self.listeners.append(queue)
        return queue

    def remove_listener(self, queue: asyncio.Queue[Optional[StreamEvent]]) -> None:
        if queue in self.listeners:
            self.listeners.remove(queue)


tasks: Dict[str, TaskState] = {}
TASK_RETENTION_SECONDS = 600


def _purge_finished_tasks(now: Optional[datetime] = None) -> None:
    if not tasks:
        return
    current_time = now or datetime.utcnow()
    cutoff = current_time - timedelta(seconds=TASK_RETENTION_SECONDS)
    stale_ids = [
        task_id
        for task_id, state in list(tasks.items())
        if state.finished_at and state.finished_at < cutoff
    ]
    for task_id in stale_ids:
        tasks.pop(task_id, None)


def _generate_demo_events_sync(request_id: str) -> List[str]:
    """Run the LangGraph demo graph once to produce SSE payloads."""

    request = EvaluationRequest(
        product_name="演示产品",
        application_scenario="本地联调",
        target_users="前端工程师",
        business_goals="验证 LangGraph SSE 结构",
        core_features=["Planner", "Executor", "Summary"],
        workflow=[],
        constraints="演示用途",
    )

    initial_state = build_demo_state(request, request_id)
    graph = build_demo_graph()
    conn = sqlite3.connect(DEMO_CHECKPOINT_PATH, check_same_thread=False)
    runner = graph.compile(checkpointer=SqliteSaver(conn))

    final_state: Optional[Dict] = None
    for event in runner.stream(
        initial_state,
        config={"configurable": {"thread_id": request_id}},
        stream_mode="values",
    ):
        final_state = event

    payloads: List[str] = []
    if final_state and final_state.get("stream_events"):
        for evt in final_state["stream_events"]:
            payloads.append(format_sse(evt))
    return payloads


def get_task(request_id: str) -> TaskState:
    try:
        return tasks[request_id]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc


async def run_generation_task(
    request_id: str,
    payload: EvaluationRequest,
    task: TaskState,
    callbacks: Optional[List] = None,
) -> None:
    tools = ToolService(request_id=request_id, product_name=payload.product_name)
    context_manager = ContextManager(VectorService())
    runner = LangGraphAgentRunner(
        request_id=request_id,
        emitter=task.publish,
        tools=tools,
        context_manager=context_manager,
        callbacks=callbacks,
    )

    try:
        context = runner.build_context(payload)

        snapshot = {
            "request": payload.model_dump(mode="json"),
            "context": context,
        }
        try:
            tools.write_text(
                "context_snapshot.json",
                json.dumps(snapshot, ensure_ascii=False, indent=2),
                description="生成任务前的上下文快照。",
            )
        except Exception as snapshot_exc:  # pragma: no cover
            logger.warning("Failed to write context snapshot for %s: %s", request_id, snapshot_exc)

        logger.info(
            "Merged request %s -> scenario=%s, target=%s, features=%d, doc_notes=%s",
            request_id,
            (payload.application_scenario or "None"),
            (payload.target_users or "None"),
            len(payload.core_features or []),
            "有内容" if (payload.document_notes and payload.document_notes.strip()) else "空",
        )

        final_state = await runner.run(payload, context)

        files = tools.list_generated_files()
        plan_steps = final_state.get("plan_steps", []) if isinstance(final_state, dict) else []
        executed_records = final_state.get("executed") if isinstance(final_state, dict) else []
        manifest = build_manifest(
            request_id=request_id,
            product_name=payload.product_name,
            directory=tools.output_dir,
            files=files,
        )
        task.manifest = manifest
        summary_text = final_state.get("summary") if isinstance(final_state, dict) else None
        await task.publish(
            StreamEvent(
                request_id=request_id,
                step="finalize",
                status=StepStatus.SUCCEEDED,
                message="结果已写入输出目录。",
                metadata={
                    "total_steps": len(plan_steps),
                    "completed_steps": len(executed_records or files),
                    "files": [file.filename for file in files],
                },
                phase="finalize",
            )
        )
        if summary_text:
            await task.publish(
                StreamEvent(
                    request_id=request_id,
                    step="final_summary",
                    step_id="final_summary",
                    status=StepStatus.SUCCEEDED,
                    message=summary_text,
                    metadata={
                        "type": "insight",
                        "title": "任务总结",
                        "summary": summary_text,
                        "files": [file.filename for file in files],
                    },
                    phase="finalize",
                )
            )
    except Exception as exc:  # pylint: disable=broad-except
        task.error = str(exc)
        await task.publish(
            StreamEvent(
                request_id=request_id,
                step="error",
                status=StepStatus.FAILED,
                message=str(exc),
                phase="error",
            )
        )
    finally:
        await task.complete()
        _purge_finished_tasks()


def format_sse(event: StreamEvent) -> str:
    payload = event.json()
    return f"data: {payload}\n\n"


async def verify_api_key(request: Request) -> None:
    settings = get_settings()
    expected = settings.api_token
    if not expected:
        return
    provided = request.headers.get("x-api-key") or request.query_params.get("api_key")
    if expected != provided:
        raise HTTPException(status_code=401, detail="Invalid API token")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    clean_expired_outputs()
    yield
    if settings.max_output_age_hours > 0:
        clean_expired_outputs()


app = FastAPI(
    title="LLM Evaluation Plan Agent",
    description="Generates evaluation plans and artefacts for LLM applications.",
    version="0.1.0",
    lifespan=lifespan,
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def index():
    if not FRONTEND_DIR.exists():
        raise HTTPException(status_code=404, detail="Front-end not found.")
    return FileResponse(FRONTEND_DIR / "index.html")


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.post("/generate-plan", response_model=GeneratePlanResponse)
async def generate_plan(
    request: Request,
    background_tasks: BackgroundTasks,
    _: None = Depends(verify_api_key),
) -> GeneratePlanResponse:
    payload_data, upload_file = await _parse_incoming_request(request)
    form_request = _parse_evaluation_payload(payload_data)
    request_id = uuid.uuid4().hex
    langsmith_callbacks = build_langsmith_callbacks(request_id, form_request.product_name)

    document_summary = DocumentSummary()
    document_notes: Optional[str] = None

    logger.debug(
        "Upload payload detected? %s",
        bool(upload_file) if upload_file is not None else "None",
    )
    if upload_file:
        logger.info(
            "Processing upload for request %s: %s",
            request_id,
            upload_file.filename,
        )
        document_notes, document_summary = await _process_uploaded_document(
            request_id,
            form_request.product_name,
            upload_file,
            callbacks=langsmith_callbacks,
        )
        vector_context = ContextManager(VectorService())
        await vector_context.add_source_document(
            request_id=request_id,
            source_id="uploaded_document",
            content=document_notes or "",
            metadata={"step_title": upload_file.filename or "上传文档"},
        )
    else:
        logger.debug("No document uploaded for request %s", request_id)

    merged_request = _merge_form_and_document(form_request, document_summary, document_notes)

    task = TaskState(request_id=request_id, product_name=merged_request.product_name)
    tasks[request_id] = task
    await task.publish(
        StreamEvent(
            request_id=request_id,
            step="accepted",
            status=StepStatus.STARTED,
            message="任务已受理，准备调用模型。",
        )
    )
    background_tasks.add_task(run_generation_task, request_id, merged_request, task, langsmith_callbacks)
    background_tasks.add_task(clean_expired_outputs)
    return GeneratePlanResponse(request_id=request_id, status="success", manifest=None)


@app.get("/events/{request_id}")
async def stream_events(request_id: str, _: None = Depends(verify_api_key)):
    task = get_task(request_id)
    queue = task.listen()

    async def event_generator():
        try:
            for event in task.events:
                yield format_sse(event)
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield format_sse(event)
        finally:
            task.remove_listener(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/demo-events")
async def demo_events():
    request_id = f"demo-{uuid.uuid4().hex[:8]}"
    payloads = await asyncio.to_thread(_generate_demo_events_sync, request_id)

    async def demo_stream():
        for payload in payloads:
            yield payload

    return StreamingResponse(demo_stream(), media_type="text/event-stream")


@app.get("/manifest/{request_id}", response_model=Manifest)
async def get_manifest(request_id: str, _: None = Depends(verify_api_key)):
    try:
        task = get_task(request_id)
    except HTTPException:
        directory = find_request_directory(request_id)
        if not directory:
            raise
        return load_manifest_from_directory(directory)

    if task.error:
        raise HTTPException(status_code=500, detail=task.error)
    if task.manifest is None:
        directory = find_request_directory(request_id)
        if directory:
            return load_manifest_from_directory(directory)
        raise HTTPException(status_code=202, detail="Task still in progress.")
    return task.manifest


def _resolve_output_file(request_id: str, filename: str) -> Path:
    directory = _resolve_request_directory(request_id)
    file_path = directory / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found.")
    return file_path


def _resolve_request_directory(request_id: str) -> Path:
    directory: Optional[Path] = None
    try:
        task = get_task(request_id)
    except HTTPException:
        directory = find_request_directory(request_id)
        if directory is None:
            raise
    else:
        if task.manifest is None:
            directory = find_request_directory(request_id)
        else:
            directory = create_request_directory(request_id, task.product_name)

    if directory is None:
        raise HTTPException(status_code=404, detail="Artefact not available yet.")
    return directory


@app.get("/download/{request_id}/{filename}")
async def download_file(request_id: str, filename: str, _: None = Depends(verify_api_key)):
    file_path = _resolve_output_file(request_id, filename)
    return FileResponse(path=file_path, filename=filename)


@app.get("/download-all/{request_id}")
async def download_all_files(request_id: str, _: None = Depends(verify_api_key)):
    directory = _resolve_request_directory(request_id)
    file_paths: List[Path] = []
    try:
        manifest = load_manifest_from_directory(directory)
        for file_meta in manifest.files:
            candidate = Path(file_meta.path)
            if not candidate.is_absolute():
                candidate = directory / candidate.name
            if candidate.exists():
                file_paths.append(candidate)
    except Exception as exc:  # pragma: no cover
        logger.debug("Failed to load manifest for %s: %s", request_id, exc)
    if not file_paths:
        file_paths = [path for path in directory.glob("**/*") if path.is_file()]
    if not file_paths:
        raise HTTPException(status_code=404, detail="暂无可下载文件。")

    buffer = io.BytesIO()
    try:
        with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for file_path in file_paths:
                arcname = file_path.relative_to(directory)
                bundle.write(file_path, arcname=str(arcname))
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=500, detail=f"打包文件失败：{exc}") from exc
    buffer.seek(0)
    filename = f"{directory.name}.zip"
    quoted = quote(filename)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
            "X-Request-Id": request_id,
        },
    )


@app.get("/preview/{request_id}/{filename}")
async def preview_file(request: Request, request_id: str, filename: str, _: None = Depends(verify_api_key)):
    file_path = _resolve_output_file(request_id, filename)
    api_key = request.query_params.get("api_key")
    download_url = f"/download/{request_id}/{filename}"
    if api_key:
        download_url = f"{download_url}?api_key={api_key}"
    body_html, kind = _render_preview_content(file_path)
    page_html = _render_preview_page(filename, download_url, body_html, kind)
    return HTMLResponse(page_html)


@app.middleware("http")
async def add_request_id_header(request: Request, call_next):
    response = await call_next(request)
    if "x-request-id" not in response.headers:
        response.headers["x-request-id"] = uuid.uuid4().hex
    return response


async def _parse_incoming_request(request: Request) -> Tuple[Dict, Optional[UploadFile]]:
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        payload_raw = form.get("payload")
        if payload_raw is None:
            raise HTTPException(status_code=400, detail="Missing form field 'payload'.")
        try:
            payload_data = json.loads(payload_raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload.") from exc
        logger.debug("Received multipart form fields: %s", list(form.keys()))
        document = form.get("document")
        logger.debug("Document field type: %s", type(document))
        if document is not None and getattr(document, "filename", ""):
            upload = document
        else:
            upload = None
        return payload_data, upload

    try:
        payload_data = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body.") from exc
    return payload_data, None


def _parse_evaluation_payload(data: Dict) -> EvaluationRequest:
    product_name = data.get("product_name")
    if not product_name or not str(product_name).strip():
        raise HTTPException(status_code=400, detail="产品名称为必填项。")

    return EvaluationRequest(
        product_name=str(product_name).strip(),
        application_scenario=None,
        target_users=None,
        business_goals=None,
        core_features=[],
        workflow=[],
        constraints=data.get("constraints"),
    )


async def _process_uploaded_document(
    request_id: str,
    product_name: str,
    upload: UploadFile,
    *,
    callbacks: Optional[List] = None,
) -> Tuple[str, DocumentSummary]:
    settings = get_settings()
    allowed_suffixes = {".md", ".markdown", ".docx"}
    original_suffix = Path(upload.filename).suffix.lower()
    if original_suffix not in allowed_suffixes:
        raise HTTPException(status_code=400, detail="仅支持 Markdown (.md/.markdown) 或 Word (.docx) 文件。")

    size = 0
    safe_original = sanitize_filename(upload.filename, fallback="document")
    if original_suffix and not safe_original.lower().endswith(original_suffix):
        safe_original = f"{safe_original}{original_suffix}"
    if not Path(safe_original).suffix:
        safe_original = f"{safe_original}.md"

    temp_path = settings.uploads_dir / f"{request_id}_{safe_original}"
    with temp_path.open("wb") as buffer:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > 50 * 1024 * 1024:
                buffer.close()
                temp_path.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail="文件大小超出 50MB 限制。")
            buffer.write(chunk)

    source_path = temp_path
    converted_markdown: Optional[Path] = None
    if original_suffix == ".docx":
        try:
            converted_markdown = _convert_docx_to_markdown(temp_path)
        except HTTPException:
            temp_path.unlink(missing_ok=True)
            raise
        source_path = converted_markdown

    analyzer = DocumentAnalyzer(callbacks=callbacks)
    try:
        extraction = await analyzer.analyze(product_name, source_path)
        logger.info(
            "Document parsed for %s -> summary_len=%s features=%s",
            request_id,
            len(extraction.summary_notes or ""),
            len(extraction.structured.core_features or []),
        )
    except Exception as exc:  # pylint: disable=broad-except
        temp_path.unlink(missing_ok=True)
        if converted_markdown:
            converted_markdown.unlink(missing_ok=True)
        logger.error("Document parsing failed for %s: %s", request_id, exc)
        raise HTTPException(status_code=400, detail=f"文档解析失败：{exc}") from exc
    finally:
        if converted_markdown:
            converted_markdown.unlink(missing_ok=True)

    # 保留原始文档副本到输出目录，执行完任务后会一并存在
    output_dir = create_request_directory(request_id, product_name)
    destination = output_dir / safe_original
    try:
        shutil.copy2(temp_path, destination)
    except Exception:
        destination.write_bytes(temp_path.read_bytes())
    finally:
        temp_path.unlink(missing_ok=True)

    return extraction.summary_notes, extraction.structured


def _convert_docx_to_markdown(docx_path: Path) -> Path:
    try:
        from docx import Document  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise HTTPException(status_code=500, detail="服务器缺少 python-docx 依赖，无法解析 Word 文档。") from exc

    document = Document(str(docx_path))
    lines: List[str] = []

    def append_line(text: str) -> None:
        lines.append(text or "")

    for paragraph in document.paragraphs:
        raw_text = paragraph.text.strip()
        if not raw_text:
            append_line("")
            continue
        style_name = (paragraph.style.name or "").lower()
        heading_level = _detect_heading_level(style_name)
        if heading_level:
            prefix = "#" * heading_level
            append_line(f"{prefix} {raw_text}")
        elif any(token in style_name for token in ("bullet", "list")):
            append_line(f"- {raw_text}")
        else:
            append_line(raw_text)

    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                append_line("| " + " | ".join(cells) + " |")

    markdown_text = "\n".join(lines).strip()
    target = docx_path.with_suffix(".converted.md")
    target.write_text(markdown_text, encoding="utf-8")
    return target


def _detect_heading_level(style_name: str) -> Optional[int]:
    match = re.search(r"heading\s*(\d)", style_name)
    if match:
        try:
            level = int(match.group(1))
            return max(1, min(level, 6))
        except ValueError:
            return None
    return None



def _merge_form_and_document(
    form: EvaluationRequest,
    summary: DocumentSummary,
    notes: Optional[str],
) -> EvaluationRequest:
    def choose(*values: Optional[str]) -> Optional[str]:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def merge_features(primary: List[str], secondary: List[str]) -> List[str]:
        seen = set()
        merged: List[str] = []
        for item in primary + secondary:
            value = item.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            merged.append(value)
        return merged

    def merge_workflow(primary: List[WorkflowNode], secondary: List[WorkflowNode]) -> List[WorkflowNode]:
        if primary and secondary:
            combined: List[WorkflowNode] = []
            seen = set()
            for node in primary + secondary:
                key = (node.name, node.input_spec, node.output_spec)
                if key in seen:
                    continue
                seen.add(key)
                combined.append(node)
            return combined
        return primary or secondary

    combined_features = merge_features(summary.core_features, form.core_features)
    combined_workflow = merge_workflow(summary.workflow, form.workflow)

    return EvaluationRequest(
        product_name=form.product_name,
        application_scenario=choose(summary.application_scenario, form.application_scenario),
        target_users=choose(summary.target_users, form.target_users),
        business_goals=choose(summary.business_goals, form.business_goals),
        core_features=combined_features,
        workflow=combined_workflow,
        constraints=choose(summary.constraints, form.constraints),
        document_notes=notes or form.document_notes,
    )

def _render_preview_content(file_path: Path) -> Tuple[str, str]:
    suffix = file_path.suffix.lower()
    if suffix == ".docx":
        return _render_docx_preview(file_path), "Word 文档"
    if suffix == ".csv":
        return _render_csv_preview(file_path), "CSV 表格"
    if suffix in {".txt", ".log", ".json", ".py", ".js", ".ts", ".md", ".markdown"}:
        return _render_text_preview(file_path), "文本"
    return (
        "<p class=\"preview-empty\">暂不支持该格式的在线预览，请下载后查看。</p>",
        "不支持的格式",
    )


def _render_docx_preview(file_path: Path) -> str:
    try:
        document = Document(file_path)
    except Exception as exc:  # pragma: no cover
        logger.error("Failed to render docx preview for %s: %s", file_path, exc)
        return "<p class=\"preview-error\">无法解析 Word 文档内容。</p>"

    blocks: List[str] = []
    for block in _iter_docx_blocks(document):
        if isinstance(block, Paragraph):
            text = escape(block.text.strip()) or "&nbsp;"
            style_name = (block.style.name or "").lower() if block.style else ""
            if style_name.startswith("heading"):
                level_match = re.search(r"(\d+)", style_name)
                level = level_match.group(1) if level_match else "2"
                blocks.append(f"<h{level}>{text}</h{level}>")
            elif "bullet" in style_name:
                blocks.append(f"<p class=\"list-bullet\">• {text}</p>")
            elif "number" in style_name:
                blocks.append(f"<p class=\"list-number\">{text}</p>")
            else:
                blocks.append(f"<p>{text}</p>")
        elif isinstance(block, Table):
            rows_html = []
            for row in block.rows:
                cells = "".join(
                    f"<td>{escape(cell.text.strip()) or '&nbsp;'}</td>" for cell in row.cells
                )
                rows_html.append(f"<tr>{cells}</tr>")
            blocks.append("<table class=\"preview-table\">" + "".join(rows_html) + "</table>")
    return "".join(blocks) or "<p class=\"preview-empty\">（文档为空）</p>"


def _render_csv_preview(file_path: Path) -> str:
    rows: List[List[str]] = []
    try:
        with file_path.open("r", encoding="utf-8", newline="") as csv_file:
            reader = csv.reader(csv_file)
            for idx, row in enumerate(reader):
                rows.append(row)
                if idx >= 500:
                    break
    except Exception as exc:  # pragma: no cover
        logger.error("Failed to render CSV preview for %s: %s", file_path, exc)
        return "<p class=\"preview-error\">无法解析 CSV 文件。</p>"

    if not rows:
        return "<p class=\"preview-empty\">（文件为空）</p>"

    header = rows[0]
    body_rows = rows[1:] if len(rows) > 1 else []
    header_html = "".join(f"<th>{escape(cell)}</th>" for cell in header)
    body_html = "".join(
        "<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in row) + "</tr>"
        for row in body_rows
    )
    note = "" if len(rows) < 500 else "<p class=\"preview-note\">仅展示前 500 行。</p>"
    return (
        f"<table class=\"preview-table\"><thead><tr>{header_html}</tr></thead><tbody>{body_html}</tbody></table>"
        + note
    )


def _render_text_preview(file_path: Path) -> str:
    try:
        text = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return "<p class=\"preview-error\">文件为二进制格式，无法直接预览。</p>"
    snippet = escape(text[:200_000])
    return f"<pre class=\"preview-code\">{snippet}</pre>"


def _render_preview_page(filename: str, download_url: str, body_html: str, kind: str) -> str:
    safe_name = escape(filename)
    title = f"文件预览 · {safe_name}"
    return f"""
<!DOCTYPE html>
<html lang=\"zh\">
  <head>
    <meta charset=\"UTF-8\" />
    <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\" />
    <title>{title}</title>
    <style>
      body {{
        font-family: 'PingFang SC', 'Microsoft YaHei', system-ui;
        margin: 0;
        background: #f5f7fa;
        color: #0f172a;
      }}
      .preview-shell {{
        max-width: 960px;
        margin: 40px auto;
        background: #fff;
        border-radius: 16px;
        padding: 28px;
        box-shadow: 0 24px 45px rgba(15,23,42,0.12);
      }}
      .preview-actions {{
        display: flex;
        gap: 12px;
        align-items: center;
        margin-bottom: 20px;
      }}
      .preview-actions a {{
        padding: 8px 16px;
        border-radius: 999px;
        background: #1f6feb;
        color: #fff;
        text-decoration: none;
        font-weight: 600;
      }}
      .preview-body {{
        line-height: 1.65;
      }}
      .preview-body h1,
      .preview-body h2,
      .preview-body h3,
      .preview-body h4 {{
        margin: 24px 0 12px;
      }}
      .preview-body p {{
        margin: 10px 0;
      }}
      .preview-table {{
        border-collapse: collapse;
        width: 100%;
        margin: 16px 0;
        font-size: 14px;
      }}
      .preview-table th,
      .preview-table td {{
        border: 1px solid #dfe4ea;
        padding: 8px 10px;
        text-align: left;
        vertical-align: top;
        background: #fff;
      }}
      .preview-table thead th {{
        background: #f1f5f9;
      }}
      .list-bullet,
      .list-number {{
        margin-left: 18px;
      }}
      .preview-empty {{
        color: #6b7280;
        font-style: italic;
      }}
      .preview-code {{
        background: #0f172a;
        color: #e2e8f0;
        padding: 16px;
        border-radius: 12px;
        overflow: auto;
        font-size: 13px;
      }}
      .preview-note {{
        font-size: 12px;
        color: #6b7280;
      }}
      .preview-error {{
        color: #b91c1c;
      }}
    </style>
  </head>
  <body>
    <div class=\"preview-shell\">
      <div class=\"preview-actions\">
        <strong>{safe_name}</strong>
        <span>（{kind}）</span>
        <a href=\"{download_url}\" target=\"_blank\" rel=\"noopener\">下载原文件</a>
      </div>
      <div class=\"preview-body\">{body_html}</div>
    </div>
  </body>
</html>
"""


def _iter_docx_blocks(document: Document):
    parent_element = document.element.body
    for child in parent_element.iterchildren():
        if child.tag.endswith("}p"):
            yield Paragraph(child, document)
        elif child.tag.endswith("}tbl"):
            yield Table(child, document)
async def _generate_demo_events(request_id: str) -> List[str]:
    """Run the LangGraph demo once and yield SSE-formatted strings."""

    request = EvaluationRequest(
        product_name="演示产品",
        application_scenario="本地联调",
        target_users="前端工程师",
        business_goals="验证 LangGraph SSE 结构",
        core_features=["Planner", "Executor", "Summary"],
        workflow=[],
        constraints="演示用途",
    )
    initial_state = build_demo_state(request, request_id)
    graph = build_demo_graph()
    # Demo graph 使用同一个 sqlite saver，与 CLI 保持一致
    conn = sqlite3.connect(CHECKPOINT_PATH, check_same_thread=False)
    runner = graph.compile(checkpointer=SqliteSaver(conn))

    final_state = None
    for event in runner.stream(initial_state, config={"configurable": {"thread_id": request_id}}, stream_mode="values"):
        final_state = event

    payloads: List[str] = []
    if final_state and final_state.get("stream_events"):
        for evt in final_state["stream_events"]:
            payloads.append(format_sse(evt))
    return payloads
