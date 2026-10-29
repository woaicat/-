"""Application state and replayable events, separate from agent checkpoints."""

import json
import re
import sqlite3
from urllib.parse import unquote, urlsplit
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4


TERMINAL_STATES = frozenset({"completed", "cancelled", "failed", "interrupted"})
SCOPE_KINDS = frozenset({"discussion", "package_outline", "package_generate", "package_continue", "focused_create", "focused_edit", "focused_rename"})
RENAME_REQUEST = re.compile(r"重命名|改名|(?:文件名|名称).{0,30}(?:改|换|中文)|(?:改|换|重设).{0,20}(?:文件名|名称|中文名)")
CONTENT_EDIT_REQUEST = re.compile(r"(?:修改|更新|重写|补充|调整|同步).{0,12}(?:正文|内容|阈值|指标|样本|手册|章节)")
PACKAGE_REQUEST_MARKERS = ("一整套", "整套", "全套", "完整评测方案", "全部产物")
CONTINUATION_MARKERS = ("继续", "接着", "接续", "完成剩余", "剩余工作", "补完")
REQUIRED_OUTLINE_SECTIONS = ("评测目标", "评测指标", "评测数据样本", "评测执行方案")
DELIVERABLE_SECTION = re.compile(
    r"^#{1,4}\s*(?:(?:[一二三四五六七八九十]+|\d+)[、.．]\s*)?"
    r"(?:(?:本次)?(?:计划生成的)?产物清单|交付文件清单|交付清单|计划生成的产物)"
    r"(?:\s*[（(][^）)\n]*[）)])?\s*$", re.M,
)
DELIVERABLE_ITEM = re.compile(r"^\s*(?:[-*]\s+|\d+[.、．]\s+)(.+?)\s*$")
DELIVERABLE_FILENAME = re.compile(
    r"^(?:`|\*\*)?(?P<name>.+?\.[A-Za-z0-9]{1,8})(?:`|\*\*)?"
    r"(?=$|[\s|：:—–，,（(])",
)
LOCAL_FILE_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
FILE_SUFFIXES = frozenset({".md", ".csv", ".docx"})
NEGATED_FILE_MENTION = re.compile(r"(?:不要|不改|不用|无需|不必|别|保持原样|保持不变)[^，。；,.;]{0,24}$")
NEGATED_AFTER_FILE = re.compile(r"(?:保持原样|保持不变|不改|无需修改|不用修改)\s*$")
CLAUSE_BOUNDARY = re.compile(r"[，。；,.;]")
RELATED_FILE_REQUEST = re.compile(r"(?:相关|关联|其余|其他)(?:的)?(?:文件|产物).{0,8}(?:同步|修改|更新)")


def _explicit_file_mentions(request_text: str, filename: str, product_name: str) -> tuple[bool, bool]:
    """Find a clearly named existing file without treating excluded files as targets."""
    stem = re.sub(r"^\d{1,3}[_\-\s]+", "", Path(filename).stem)
    short_name = stem.replace(product_name, "").strip("_- ")
    aliases = [filename, short_name, stem]
    if Path(filename).suffix.lower() == ".csv" and "样本" in short_name:
        aliases.extend(["CSV 样本", "样本 CSV"])
    positive = negative = False
    for alias in dict.fromkeys(alias for alias in aliases if len(alias) >= 2):
        for match in re.finditer(re.escape(alias), request_text, re.I):
            prefix = CLAUSE_BOUNDARY.split(request_text[:match.start()])[-1][-40:]
            suffix = CLAUSE_BOUNDARY.split(request_text[match.end():], maxsplit=1)[0][:40]
            if NEGATED_FILE_MENTION.search(prefix) or NEGATED_AFTER_FILE.search(suffix):
                negative = True
            else:
                positive = True
    return positive, negative and not positive


def _broken_file_links(content: str, filenames: set[str]) -> list[str]:
    broken = []
    for match in LOCAL_FILE_LINK.finditer(content):
        raw = match.group(1).strip().split(" ", 1)[0].strip("<>\"'")
        target = urlsplit(raw)
        if target.scheme or target.netloc or not target.path:
            continue
        path = unquote(target.path)
        if Path(path).suffix.lower() not in FILE_SUFFIXES:
            continue
        if path.startswith("/") or "/" in path or "\\" in path or path not in filenames:
            broken.append(path)
    return broken


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _id() -> str:
    return uuid4().hex


def parse_outline_deliverables(body: str) -> list[tuple[str, str]]:
    """Parse the editable card's deliverable section into an exact allowlist."""
    if any(section not in body for section in REQUIRED_OUTLINE_SECTIONS):
        raise ValueError("大纲缺少评测目标、指标、数据样本或执行方案")
    match = DELIVERABLE_SECTION.search(body)
    if match is None:
        raise ValueError("大纲缺少产物清单")
    tail = body[match.end():]
    lines = []
    for line in tail.splitlines():
        if line.lstrip().startswith("#"):
            break
        if line.lstrip().startswith("|"):
            item = line.strip().strip("|").split("|", 1)[0].strip()
        else:
            found = DELIVERABLE_ITEM.match(line)
            if not found:
                continue
            item = found.group(1)
        found = DELIVERABLE_FILENAME.match(item)
        if not found:
            continue
        filename = found.group("name").strip()
        if "/" in filename or "\\" in filename or filename.startswith("."):
            raise ValueError("产物名不能包含路径")
        kind = filename.rsplit(".", 1)[1].lower()
        if f".{kind}" not in FILE_SUFFIXES:
            raise ValueError("产物清单仅支持 .md、.csv 或 .docx 文件")
        lines.append((filename, kind))
    if not lines or len(lines) != len(set(name for name, _ in lines)):
        raise ValueError("产物清单必须列出不重复的 .md、.csv 或 .docx 文件")
    return lines


class NotFoundError(ValueError):
    """The resource does not belong to this conversation, or does not exist."""


class ConflictError(ValueError):
    """A revision or active-turn constraint rejected the request."""

    def __init__(self, message: str, *, turn_id: str | None = None):
        super().__init__(message)
        self.turn_id = turn_id


SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    agent_thread_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evaluations (
    evaluation_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    name TEXT NOT NULL,
    package_status TEXT NOT NULL DEFAULT 'not_requested'
        CHECK (package_status IN ('not_requested','outline_pending','running','incomplete','completed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (evaluation_id, conversation_id)
);
CREATE TABLE IF NOT EXISTS evaluation_notes (
    note_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    evaluation_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('fact','assumption','question')),
    body TEXT NOT NULL,
    source_message_id TEXT,
    source_attachment_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (evaluation_id, conversation_id)
        REFERENCES evaluations(evaluation_id, conversation_id),
    FOREIGN KEY (source_message_id, conversation_id)
        REFERENCES messages(message_id, conversation_id),
    FOREIGN KEY (source_attachment_id, conversation_id)
        REFERENCES attachments(attachment_id, conversation_id)
);
CREATE TABLE IF NOT EXISTS turns (
    turn_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    evaluation_id TEXT,
    trigger_message_id TEXT,
    scope_kind TEXT NOT NULL DEFAULT 'undecided',
    scope_json TEXT NOT NULL DEFAULT '{}',
    outline_id TEXT,
    outline_revision INTEGER,
    status TEXT NOT NULL CHECK (status IN
        ('running','cancel_requested','completed','cancelled','failed','interrupted')),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_ms INTEGER,
    client_key TEXT,
    FOREIGN KEY (evaluation_id, conversation_id)
        REFERENCES evaluations(evaluation_id, conversation_id),
    UNIQUE (turn_id, conversation_id),
    UNIQUE (conversation_id, client_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_turn_per_conversation
    ON turns(conversation_id) WHERE status IN ('running','cancel_requested');
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    turn_id TEXT,
    role TEXT NOT NULL CHECK (role IN ('user','assistant')),
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('streaming','completed','failed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (turn_id, conversation_id) REFERENCES turns(turn_id, conversation_id),
    UNIQUE (message_id, conversation_id)
);
CREATE TABLE IF NOT EXISTS attachments (
    attachment_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    evaluation_id TEXT,
    message_id TEXT,
    filename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    extracted_path TEXT NOT NULL,
    upload_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (evaluation_id, conversation_id)
        REFERENCES evaluations(evaluation_id, conversation_id),
    FOREIGN KEY (message_id, conversation_id)
        REFERENCES messages(message_id, conversation_id),
    UNIQUE (conversation_id, upload_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS attachments_identity ON attachments(attachment_id, conversation_id);
CREATE TABLE IF NOT EXISTS outlines (
    outline_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    evaluation_id TEXT NOT NULL,
    body TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL CHECK (status IN ('pending','confirmed','stale')),
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    FOREIGN KEY (evaluation_id, conversation_id)
        REFERENCES evaluations(evaluation_id, conversation_id),
    UNIQUE (outline_id, conversation_id)
);
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    evaluation_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_path TEXT NOT NULL,
    export_path TEXT,
    content_revision INTEGER NOT NULL DEFAULT 1,
    ready_at TEXT NOT NULL,
    FOREIGN KEY (evaluation_id, conversation_id)
        REFERENCES evaluations(evaluation_id, conversation_id),
    UNIQUE (artifact_id, conversation_id)
);
CREATE TABLE IF NOT EXISTS deliverables (
    deliverable_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    evaluation_id TEXT NOT NULL,
    outline_id TEXT NOT NULL,
    outline_revision INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','ready')),
    artifact_id TEXT,
    FOREIGN KEY (evaluation_id, conversation_id)
        REFERENCES evaluations(evaluation_id, conversation_id),
    FOREIGN KEY (outline_id, conversation_id)
        REFERENCES outlines(outline_id, conversation_id),
    FOREIGN KEY (artifact_id, conversation_id)
        REFERENCES artifacts(artifact_id, conversation_id)
);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    v INTEGER NOT NULL DEFAULT 1,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    turn_id TEXT,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (turn_id, conversation_id) REFERENCES turns(turn_id, conversation_id)
);
CREATE TABLE IF NOT EXISTS artifact_aliases (
    conversation_id TEXT NOT NULL,
    evaluation_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    PRIMARY KEY (conversation_id,evaluation_id,filename),
    FOREIGN KEY (artifact_id,conversation_id) REFERENCES artifacts(artifact_id,conversation_id),
    FOREIGN KEY (evaluation_id,conversation_id) REFERENCES evaluations(evaluation_id,conversation_id)
);
CREATE TABLE IF NOT EXISTS artifact_rename_batches (
    batch_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    turn_id TEXT NOT NULL UNIQUE,
    evaluation_id TEXT NOT NULL,
    renames_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (turn_id,conversation_id) REFERENCES turns(turn_id,conversation_id)
);
CREATE INDEX IF NOT EXISTS events_by_conversation ON events(conversation_id, event_id);
CREATE INDEX IF NOT EXISTS messages_by_conversation ON messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS evaluations_by_conversation ON evaluations(conversation_id, created_at);
"""


class WorkspaceStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise RuntimeError(f"Unsupported workspace schema version: {version}")
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA user_version=2")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _require_conversation(conn: sqlite3.Connection, conversation_id: str) -> None:
        if conn.execute(
            "SELECT 1 FROM conversations WHERE conversation_id=?", (conversation_id,)
        ).fetchone() is None:
            raise NotFoundError("Conversation not found")

    @staticmethod
    def _event(
        conn: sqlite3.Connection,
        conversation_id: str,
        turn_id: str | None,
        kind: str,
        payload: dict,
    ) -> int:
        cursor = conn.execute(
            "INSERT INTO events(conversation_id,turn_id,type,payload_json,created_at) VALUES(?,?,?,?,?)",
            (conversation_id, turn_id, kind, json.dumps(payload, ensure_ascii=False), _now()),
        )
        return cursor.lastrowid

    def create_conversation(self, title: str = "新对话") -> dict:
        conversation_id = _id()
        now = _now()
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO conversations VALUES(?,?,?,?,?)",
                (conversation_id, conversation_id, title, now, now),
            )
        return {"conversation_id": conversation_id, "agent_thread_id": conversation_id, "title": title}

    def list_conversations(self) -> list[dict]:
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM conversations ORDER BY updated_at DESC, conversation_id DESC"
            )]

    def create_evaluation(self, conversation_id: str, name: str) -> dict:
        evaluation_id = _id()
        now = _now()
        with self._transaction() as conn:
            self._require_conversation(conn, conversation_id)
            conn.execute(
                "INSERT INTO evaluations(evaluation_id,conversation_id,name,created_at,updated_at) VALUES(?,?,?,?,?)",
                (evaluation_id, conversation_id, name, now, now),
            )
        return {"evaluation_id": evaluation_id, "conversation_id": conversation_id, "name": name}

    def select_evaluation_scope(
        self,
        conversation_id: str,
        turn_id: str,
        *,
        scope_kind: str,
        evaluation_id: str | None = None,
        product_name: str | None = None,
        target_names: list[str] | None = None,
        target_artifact_ids: list[str] | None = None,
        attachment_ids: list[str] | None = None,
    ) -> dict:
        """Bind a product and finite turn scope; model labels alone grant nothing."""
        if scope_kind not in SCOPE_KINDS - {"package_generate"}:
            raise ValueError("Invalid model-selectable scope")
        target_names = target_names or []
        target_artifact_ids = target_artifact_ids or []
        attachment_ids = attachment_ids or []
        if len(target_names) != len(set(target_names)) or len(target_artifact_ids) != len(set(target_artifact_ids)):
            raise ValueError("Duplicate targets")
        if scope_kind == "package_outline" and target_artifact_ids:
            raise ConflictError("package_outline plans a new package; use focused_edit to modify existing artifacts", turn_id=turn_id)
        with self._transaction() as conn:
            self._require_running_turn(conn, conversation_id, turn_id)
            turn = conn.execute(
                "SELECT scope_kind,trigger_message_id FROM turns WHERE turn_id=? AND conversation_id=?",
                (turn_id, conversation_id),
            ).fetchone()
            if turn["scope_kind"] != "undecided":
                raise ConflictError("Turn scope already selected", turn_id=turn_id)
            request = conn.execute(
                "SELECT body FROM messages WHERE message_id=? AND conversation_id=?",
                (turn["trigger_message_id"], conversation_id),
            ).fetchone()
            request_text = request[0] if request else ""
            rename_requested = bool(RENAME_REQUEST.search(request_text))
            if scope_kind == "focused_rename" and not rename_requested:
                raise ConflictError("Rename needs an explicit filename-change request", turn_id=turn_id)
            if scope_kind in ("focused_create", "focused_edit") and rename_requested and not CONTENT_EDIT_REQUEST.search(request_text):
                raise ConflictError("Filename-only changes require focused_rename; do not rewrite file contents", turn_id=turn_id)
            if scope_kind == "focused_create" and any(
                marker in request_text for marker in PACKAGE_REQUEST_MARKERS
            ):
                raise ConflictError("Full package request requires an outline", turn_id=turn_id)
            if scope_kind == "focused_create":
                if not target_names or len(target_names) >= 4 or any(name not in request_text for name in target_names):
                    raise ConflictError("Focused creation needs explicit, limited file targets", turn_id=turn_id)
            if scope_kind in ("focused_edit", "focused_rename") and not target_artifact_ids:
                raise ConflictError("Focused change needs existing artifact IDs", turn_id=turn_id)
            if evaluation_id is not None:
                product = conn.execute(
                    "SELECT evaluation_id FROM evaluations WHERE evaluation_id=? AND conversation_id=?",
                    (evaluation_id, conversation_id),
                ).fetchone()
                if product is None:
                    raise NotFoundError("Evaluation not found in conversation")
            elif product_name and product_name.strip():
                evaluation_id = _id()
                now = _now()
                conn.execute(
                    "INSERT INTO evaluations(evaluation_id,conversation_id,name,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (evaluation_id, conversation_id, product_name.strip(), now, now),
                )
            else:
                raise ValueError("Choose an existing evaluation or name a new one")
            if attachment_ids:
                rows = conn.execute(
                    f"SELECT attachment_id,evaluation_id,message_id FROM attachments WHERE conversation_id=? AND attachment_id IN ({','.join('?' for _ in attachment_ids)})",
                    (conversation_id, *attachment_ids),
                ).fetchall()
                if len(rows) != len(attachment_ids) or any(
                    row["message_id"] != turn["trigger_message_id"] or
                    row["evaluation_id"] not in (None, evaluation_id) for row in rows
                ):
                    raise NotFoundError("Attachment not in this turn or already belongs to another product")
                conn.executemany(
                    "UPDATE attachments SET evaluation_id=? WHERE attachment_id=? AND conversation_id=?",
                    [(evaluation_id, item, conversation_id) for item in attachment_ids],
                )
            if target_artifact_ids:
                rows = conn.execute(
                    f"SELECT artifact_id FROM artifacts WHERE conversation_id=? AND evaluation_id=? AND artifact_id IN ({','.join('?' for _ in target_artifact_ids)})",
                    (conversation_id, evaluation_id, *target_artifact_ids),
                ).fetchall()
                if len(rows) != len(target_artifact_ids):
                    raise NotFoundError("Artifact not in selected product")
            if scope_kind in ("focused_edit", "focused_rename"):
                product_name_row = conn.execute(
                    "SELECT name FROM evaluations WHERE conversation_id=? AND evaluation_id=?",
                    (conversation_id, evaluation_id),
                ).fetchone()
                named = conn.execute(
                    "SELECT artifact_id,filename FROM artifacts WHERE conversation_id=? AND evaluation_id=?",
                    (conversation_id, evaluation_id),
                ).fetchall()
                required = []
                excluded = []
                for item in named:
                    positive, negative = _explicit_file_mentions(
                        request_text, item["filename"], product_name_row["name"]
                    )
                    for alias in conn.execute(
                        "SELECT filename FROM artifact_aliases WHERE conversation_id=? AND artifact_id=?",
                        (conversation_id, item["artifact_id"]),
                    ):
                        alias_positive, alias_negative = _explicit_file_mentions(request_text, alias[0], product_name_row["name"])
                        positive |= alias_positive
                        negative |= alias_negative
                    if positive:
                        required.append(item)
                    elif negative:
                        excluded.append(item)
                missing = [item["filename"] for item in required
                           if item["artifact_id"] not in target_artifact_ids]
                forbidden = [item["filename"] for item in excluded
                             if item["artifact_id"] in target_artifact_ids]
                if missing:
                    raise ConflictError(
                        "Focused edit must include every named file: " + "、".join(missing), turn_id=turn_id
                    )
                if forbidden:
                    raise ConflictError(
                        "Focused edit includes a file the user excluded: " + "、".join(forbidden), turn_id=turn_id
                    )
                if required and not RELATED_FILE_REQUEST.search(request_text):
                    requested_ids = {item["artifact_id"] for item in required}
                    extra = [item["filename"] for item in named
                             if item["artifact_id"] in target_artifact_ids
                             and item["artifact_id"] not in requested_ids]
                    if extra:
                        raise ConflictError(
                            "Focused edit includes files outside the named request: " + "、".join(extra),
                            turn_id=turn_id,
                        )
            scope = {"target_names": target_names, "target_artifact_ids": target_artifact_ids}
            if scope_kind == "package_continue":
                outline = conn.execute(
                    """SELECT outline_id,revision FROM outlines WHERE conversation_id=? AND evaluation_id=?
                       AND status='confirmed' ORDER BY confirmed_at DESC LIMIT 1""",
                    (conversation_id, evaluation_id),
                ).fetchone()
                if outline is None or not any(marker in request_text for marker in CONTINUATION_MARKERS):
                    raise ConflictError("Continuation needs an explicit request and confirmed outline", turn_id=turn_id)
                pending = conn.execute(
                    """SELECT deliverable_id FROM deliverables WHERE conversation_id=? AND outline_id=?
                       AND outline_revision=? AND status='pending'""",
                    (conversation_id, outline["outline_id"], outline["revision"]),
                ).fetchall()
                package_status = conn.execute(
                    "SELECT package_status FROM evaluations WHERE conversation_id=? AND evaluation_id=?",
                    (conversation_id, evaluation_id),
                ).fetchone()[0]
                if not pending:
                    if package_status != "incomplete":
                        raise ConflictError("No remaining deliverables", turn_id=turn_id)
                allowed = [row[0] for row in pending]
                if package_status == "incomplete":
                    rows = conn.execute(
                        """SELECT d.deliverable_id,d.name,d.kind,a.source_path,a.export_path
                           FROM deliverables d LEFT JOIN artifacts a ON a.artifact_id=d.artifact_id
                           WHERE d.conversation_id=? AND d.outline_id=? AND d.outline_revision=?
                             AND d.status='ready'""",
                        (conversation_id, outline["outline_id"], outline["revision"]),
                    ).fetchall()
                    names = {row[0] for row in conn.execute(
                        "SELECT name FROM deliverables WHERE conversation_id=? AND outline_id=? AND outline_revision=?",
                        (conversation_id, outline["outline_id"], outline["revision"]),
                    )}
                    names.update(self._known_filenames(conn, conversation_id, evaluation_id))
                    for item in rows:
                        source = Path(item["source_path"]) if item["source_path"] else None
                        export = Path(item["export_path"]) if item["export_path"] else None
                        needs_repair = not source or not source.is_file() or not export or not export.is_file()
                        if not needs_repair and item["kind"] in ("md", "docx"):
                            needs_repair = bool(_broken_file_links(source.read_text(encoding="utf-8"), names))
                        if needs_repair:
                            allowed.append(item["deliverable_id"])
                scope["deliverable_ids"] = allowed
                conn.execute(
                    """UPDATE turns SET evaluation_id=?,scope_kind=?,scope_json=?,outline_id=?,outline_revision=?
                       WHERE turn_id=?""",
                    (evaluation_id, scope_kind, json.dumps(scope, ensure_ascii=False), outline["outline_id"],
                     outline["revision"], turn_id),
                )
            else:
                conn.execute(
                    "UPDATE turns SET evaluation_id=?,scope_kind=?,scope_json=? WHERE turn_id=?",
                    (evaluation_id, scope_kind, json.dumps(scope, ensure_ascii=False), turn_id),
                )
            return {"evaluation_id": evaluation_id, "scope_kind": scope_kind, **scope}

    @staticmethod
    def _known_filenames(conn, conversation_id: str, evaluation_id: str) -> set[str]:
        return {row[0] for row in conn.execute(
            """SELECT filename FROM artifacts WHERE conversation_id=? AND evaluation_id=?
               UNION SELECT filename FROM artifact_aliases WHERE conversation_id=? AND evaluation_id=?""",
            (conversation_id, evaluation_id, conversation_id, evaluation_id),
        )}

    @staticmethod
    def _artifact_name_matches(conn, conversation_id: str, artifact_id: str, expected: str, current: str) -> bool:
        return expected == current or conn.execute(
            "SELECT 1 FROM artifact_aliases WHERE conversation_id=? AND artifact_id=? AND filename=?",
            (conversation_id, artifact_id, expected),
        ).fetchone() is not None

    def rename_artifacts(self, conversation_id: str, turn_id: str, renames: list[dict]) -> dict:
        """One authorized atomic metadata batch; source/export bytes never change."""
        if not renames or len(renames) > 100 or any(
            not isinstance(item, dict) or set(item) != {"artifact_id", "filename"} or
            not isinstance(item["artifact_id"], str) or not isinstance(item["filename"], str)
            for item in renames
        ):
            raise ValueError("Provide 1-100 artifact_id/filename pairs")
        mapping = {item["artifact_id"]: item["filename"] for item in renames}
        if len(mapping) != len(renames) or len(set(mapping.values())) != len(renames):
            raise ValueError("Duplicate artifact IDs or destination names")
        for name in mapping.values():
            if (not name.strip() or name != name.strip() or name.startswith(".") or
                    any(char in name for char in '/\\') or any(ord(char) < 32 or ord(char) == 127 for char in name) or
                    len(name.encode("utf-8")) > 240):
                raise ValueError("Invalid destination filename")
        with self._transaction() as conn:
            self._require_running_turn(conn, conversation_id, turn_id)
            turn = conn.execute("SELECT * FROM turns WHERE conversation_id=? AND turn_id=?",
                                (conversation_id, turn_id)).fetchone()
            scope = json.loads(turn["scope_json"])
            if (turn["scope_kind"] != "focused_rename" or scope.get("finalizing") or
                    set(mapping) != set(scope.get("target_artifact_ids", []))):
                raise ConflictError("Rename must cover exactly the authorized batch", turn_id=turn_id)
            prior = conn.execute("SELECT * FROM artifact_rename_batches WHERE conversation_id=? AND turn_id=?",
                                 (conversation_id, turn_id)).fetchone()
            if prior:
                saved = json.loads(prior["renames_json"])
                if {row["artifact_id"]: row["filename"] for row in saved} != mapping:
                    raise ConflictError("This turn already renamed a different batch", turn_id=turn_id)
                return {"batch_id": prior["batch_id"], "artifacts": saved}
            rows = [dict(row) for row in conn.execute(
                "SELECT * FROM artifacts WHERE conversation_id=? AND evaluation_id=?",
                (conversation_id, turn["evaluation_id"]),
            )]
            owned = {row["artifact_id"]: row for row in rows}
            if not set(mapping).issubset(owned):
                raise NotFoundError("Artifact not in selected product")
            occupied = {row["filename"]: row["artifact_id"] for row in rows}
            occupied.update({row["filename"]: row["artifact_id"] for row in conn.execute(
                "SELECT filename,artifact_id FROM artifact_aliases WHERE conversation_id=? AND evaluation_id=?",
                (conversation_id, turn["evaluation_id"]),
            )})
            for artifact_id, name in mapping.items():
                if Path(name).suffix != Path(owned[artifact_id]["filename"]).suffix:
                    raise ValueError("Renaming cannot change the file extension")
                if name in occupied and occupied[name] != artifact_id:
                    raise ConflictError("Destination name or historical alias already belongs to another file", turn_id=turn_id)
            batch_id, now, renamed = _id(), _now(), []
            for artifact_id, name in mapping.items():
                row = owned[artifact_id]
                conn.execute("INSERT OR IGNORE INTO artifact_aliases VALUES(?,?,?,?)",
                             (conversation_id, turn["evaluation_id"], row["filename"], artifact_id))
                conn.execute("UPDATE artifacts SET filename=? WHERE artifact_id=? AND conversation_id=?",
                             (name, artifact_id, conversation_id))
                item = {"artifact_id": artifact_id, "evaluation_id": turn["evaluation_id"],
                        "old_filename": row["filename"], "filename": name, "kind": row["kind"],
                        "content_revision": row["content_revision"], "batch_id": batch_id}
                renamed.append(item)
                self._event(conn, conversation_id, turn_id, "artifact.renamed", item)
            conn.execute("INSERT INTO artifact_rename_batches VALUES(?,?,?,?,?,?)",
                         (batch_id, conversation_id, turn_id, turn["evaluation_id"], json.dumps(renamed, ensure_ascii=False), now))
            self._event(conn, conversation_id, turn_id, "tool.progress", {
                "message": f"已重命名 {len(renamed)} 个文件，正在完成回复", "action": "rename_artifacts",
            })
            return {"batch_id": batch_id, "artifacts": renamed}

    def submit_outline(self, conversation_id: str, turn_id: str, body: str) -> dict:
        deliverables = parse_outline_deliverables(body)
        with self._transaction() as conn:
            self._require_running_turn(conn, conversation_id, turn_id)
            turn = conn.execute(
                "SELECT evaluation_id,scope_kind FROM turns WHERE turn_id=? AND conversation_id=?",
                (turn_id, conversation_id),
            ).fetchone()
            if turn["scope_kind"] != "package_outline" or not turn["evaluation_id"]:
                raise ConflictError("This turn cannot submit a package outline", turn_id=turn_id)
            conn.execute(
                "UPDATE outlines SET status='stale' WHERE conversation_id=? AND evaluation_id=? AND status='pending'",
                (conversation_id, turn["evaluation_id"]),
            )
            outline_id = _id()
            conn.execute(
                """INSERT INTO outlines(outline_id,conversation_id,evaluation_id,body,status,created_at)
                   VALUES(?,?,?,?,'pending',?)""",
                (outline_id, conversation_id, turn["evaluation_id"], body, _now()),
            )
            conn.execute(
                "UPDATE evaluations SET package_status='outline_pending',updated_at=? WHERE evaluation_id=?",
                (_now(), turn["evaluation_id"]),
            )
            self._event(conn, conversation_id, turn_id, "outline.ready", {
                "outline_id": outline_id, "evaluation_id": turn["evaluation_id"],
                "revision": 1, "body": body, "deliverables": [name for name, _ in deliverables],
            })
            return {"outline_id": outline_id, "revision": 1, "deliverables": deliverables}

    def edit_outline(self, conversation_id: str, outline_id: str, *, expected_revision: int, body: str) -> int:
        parse_outline_deliverables(body)
        with self._transaction() as conn:
            self._require_conversation(conn, conversation_id)
            outline = conn.execute(
                "SELECT revision,status FROM outlines WHERE conversation_id=? AND outline_id=?",
                (conversation_id, outline_id),
            ).fetchone()
            if outline is None:
                raise NotFoundError("Outline not found")
            if outline["status"] != "pending" or outline["revision"] != expected_revision:
                raise ConflictError("Outline revision changed")
            active = conn.execute(
                "SELECT turn_id FROM turns WHERE conversation_id=? AND status IN ('running','cancel_requested')",
                (conversation_id,),
            ).fetchone()
            if active:
                raise ConflictError("Conversation is running", turn_id=active[0])
            revision = expected_revision + 1
            conn.execute(
                "UPDATE outlines SET body=?,revision=? WHERE outline_id=?",
                (body, revision, outline_id),
            )
            self._event(conn, conversation_id, None, "outline.updated", {"outline_id": outline_id, "revision": revision, "body": body})
            return revision

    def confirm_outline(
        self, conversation_id: str, outline_id: str, *, expected_revision: int, client_key: str
    ) -> dict:
        if not client_key.strip():
            raise ValueError("Confirmation idempotency key is required")
        with self._transaction() as conn:
            self._require_conversation(conn, conversation_id)
            existing = conn.execute(
                "SELECT turn_id,outline_id FROM turns WHERE conversation_id=? AND client_key=?",
                (conversation_id, client_key),
            ).fetchone()
            if existing:
                if existing["outline_id"] != outline_id:
                    raise ConflictError("Idempotency key belongs to another operation", turn_id=existing["turn_id"])
                return {"turn_id": existing[0]}
            active = conn.execute(
                "SELECT turn_id FROM turns WHERE conversation_id=? AND status IN ('running','cancel_requested')",
                (conversation_id,),
            ).fetchone()
            if active:
                raise ConflictError("Conversation is running", turn_id=active[0])
            outline = conn.execute(
                "SELECT * FROM outlines WHERE conversation_id=? AND outline_id=?",
                (conversation_id, outline_id),
            ).fetchone()
            if outline is None:
                raise NotFoundError("Outline not found")
            if outline["status"] != "pending" or outline["revision"] != expected_revision:
                raise ConflictError("Outline revision changed")
            deliverables = parse_outline_deliverables(outline["body"])
            now, turn_id = _now(), _id()
            conn.execute(
                "UPDATE outlines SET status='confirmed',confirmed_at=? WHERE outline_id=?",
                (now, outline_id),
            )
            ids = []
            for name, kind in deliverables:
                deliverable_id = _id()
                ids.append(deliverable_id)
                conn.execute(
                    """INSERT INTO deliverables(deliverable_id,conversation_id,evaluation_id,outline_id,
                       outline_revision,name,kind,status) VALUES(?,?,?,?,?,?,?,'pending')""",
                    (deliverable_id, conversation_id, outline["evaluation_id"], outline_id,
                     expected_revision, name, kind),
                )
            scope = json.dumps({"deliverable_ids": ids}, ensure_ascii=False)
            conn.execute(
                """INSERT INTO turns(turn_id,conversation_id,evaluation_id,scope_kind,scope_json,
                   outline_id,outline_revision,status,started_at,client_key)
                   VALUES(?,?,?,?,?,?,?,'running',?,?)""",
                (turn_id, conversation_id, outline["evaluation_id"], "package_generate", scope,
                 outline_id, expected_revision, now, client_key),
            )
            conn.execute(
                "UPDATE evaluations SET package_status='running',updated_at=? WHERE evaluation_id=?",
                (now, outline["evaluation_id"]),
            )
            self._event(conn, conversation_id, turn_id, "turn.started", {
                "turn_id": turn_id, "started_at": now, "outline_id": outline_id,
                "outline_revision": expected_revision,
            })
            return {"turn_id": turn_id, "deliverable_ids": ids}

    def create_attachment(
        self,
        conversation_id: str,
        *,
        upload_key: str,
        filename: str,
        mime_type: str,
        storage_path: str,
        extracted_path: str,
    ) -> dict:
        with self._transaction() as conn:
            self._require_conversation(conn, conversation_id)
            active = conn.execute(
                "SELECT turn_id FROM turns WHERE conversation_id=? AND status IN ('running','cancel_requested')",
                (conversation_id,),
            ).fetchone()
            if active:
                raise ConflictError("Conversation is running", turn_id=active[0])
            existing = conn.execute(
                "SELECT * FROM attachments WHERE conversation_id=? AND upload_key=?",
                (conversation_id, upload_key),
            ).fetchone()
            if existing:
                return dict(existing)
            attachment_id = _id()
            conn.execute(
                """INSERT INTO attachments(attachment_id,conversation_id,filename,mime_type,
                   storage_path,extracted_path,upload_key,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (attachment_id, conversation_id, filename, mime_type, storage_path,
                 extracted_path, upload_key, _now()),
            )
            return dict(conn.execute("SELECT * FROM attachments WHERE attachment_id=?", (attachment_id,)).fetchone())

    def begin_message_turn(
        self,
        conversation_id: str,
        *,
        body: str,
        client_key: str,
        attachment_ids: list[str] | None = None,
    ) -> dict:
        if not client_key.strip():
            raise ValueError("Message idempotency key is required")
        attachment_ids = attachment_ids or []
        if not body.strip() and not attachment_ids:
            raise ValueError("Message needs text or attachments")
        if len(attachment_ids) != len(set(attachment_ids)):
            raise ValueError("Duplicate attachment IDs")
        with self._transaction() as conn:
            self._require_conversation(conn, conversation_id)
            existing = conn.execute(
                "SELECT turn_id,trigger_message_id FROM turns WHERE conversation_id=? AND client_key=?",
                (conversation_id, client_key),
            ).fetchone()
            if existing:
                if existing["trigger_message_id"] is None:
                    raise ConflictError("Idempotency key belongs to another operation", turn_id=existing["turn_id"])
                return dict(existing)
            active = conn.execute(
                "SELECT turn_id FROM turns WHERE conversation_id=? AND status IN ('running','cancel_requested')",
                (conversation_id,),
            ).fetchone()
            if active:
                raise ConflictError("Conversation is running", turn_id=active[0])
            if attachment_ids:
                rows = conn.execute(
                    f"SELECT attachment_id,message_id FROM attachments WHERE conversation_id=? AND attachment_id IN ({','.join('?' for _ in attachment_ids)})",
                    (conversation_id, *attachment_ids),
                ).fetchall()
                if len(rows) != len(attachment_ids) or any(row["message_id"] for row in rows):
                    raise NotFoundError("Attachment missing, foreign, or already sent")
            turn_id, message_id, now = _id(), _id(), _now()
            conn.execute(
                """INSERT INTO turns(turn_id,conversation_id,trigger_message_id,status,started_at,client_key)
                   VALUES(?,?,?,'running',?,?)""",
                (turn_id, conversation_id, message_id, now, client_key),
            )
            conn.execute(
                """INSERT INTO messages(message_id,conversation_id,turn_id,role,body,status,created_at,updated_at)
                   VALUES(?,?,?,'user',?,'completed',?,?)""",
                (message_id, conversation_id, turn_id, body, now, now),
            )
            if attachment_ids:
                conn.executemany(
                    "UPDATE attachments SET message_id=? WHERE conversation_id=? AND attachment_id=?",
                    [(message_id, conversation_id, item) for item in attachment_ids],
                )
            title_text = " ".join(body.split())
            if not title_text and attachment_ids:
                title_text = conn.execute(
                    "SELECT filename FROM attachments WHERE conversation_id=? AND attachment_id=?",
                    (conversation_id, attachment_ids[0]),
                ).fetchone()[0]
            title = title_text[:32] + ("…" if len(title_text) > 32 else "")
            conn.execute(
                """UPDATE conversations SET updated_at=?,
                   title=CASE WHEN title='新对话' AND ?<>'' THEN ? ELSE title END
                   WHERE conversation_id=?""",
                (now, title, title, conversation_id),
            )
            self._event(conn, conversation_id, turn_id, "turn.started", {"turn_id": turn_id, "started_at": now})
            return {"turn_id": turn_id, "trigger_message_id": message_id}

    def begin_assistant_message(self, conversation_id: str, turn_id: str) -> str:
        with self._transaction() as conn:
            self._require_running_turn(conn, conversation_id, turn_id)
            existing = conn.execute(
                "SELECT message_id FROM messages WHERE conversation_id=? AND turn_id=? AND role='assistant'",
                (conversation_id, turn_id),
            ).fetchone()
            if existing:
                return existing[0]
            message_id, now = _id(), _now()
            conn.execute(
                """INSERT INTO messages(message_id,conversation_id,turn_id,role,status,created_at,updated_at)
                   VALUES(?,?,?,'assistant','streaming',?,?)""",
                (message_id, conversation_id, turn_id, now, now),
            )
            self._event(conn, conversation_id, turn_id, "message.started", {"message_id": message_id})
            return message_id

    @staticmethod
    def _require_running_turn(conn: sqlite3.Connection, conversation_id: str, turn_id: str) -> None:
        row = conn.execute(
            "SELECT status FROM turns WHERE conversation_id=? AND turn_id=?", (conversation_id, turn_id)
        ).fetchone()
        if row is None:
            raise NotFoundError("Turn not found")
        if row[0] != "running":
            raise ConflictError(f"Turn is {row[0]}", turn_id=turn_id)

    def append_message_delta(self, conversation_id: str, turn_id: str, message_id: str, delta: str) -> int:
        if not delta:
            raise ValueError("Empty delta")
        with self._transaction() as conn:
            self._require_running_turn(conn, conversation_id, turn_id)
            cursor = conn.execute(
                """UPDATE messages SET body=body||?,updated_at=? WHERE message_id=? AND conversation_id=?
                   AND turn_id=? AND role='assistant' AND status='streaming'""",
                (delta, _now(), message_id, conversation_id, turn_id),
            )
            if cursor.rowcount != 1:
                raise NotFoundError("Streaming assistant message not found")
            return self._event(conn, conversation_id, turn_id, "message.delta", {"message_id": message_id, "delta": delta})

    def complete_assistant_message(
        self, conversation_id: str, turn_id: str, message_id: str,
        artifact_ids: list[str] | None = None,
    ) -> int:
        with self._transaction() as conn:
            self._require_running_turn(conn, conversation_id, turn_id)
            row = conn.execute(
                "SELECT body FROM messages WHERE conversation_id=? AND turn_id=? AND message_id=? AND status='streaming'",
                (conversation_id, turn_id, message_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("Streaming assistant message not found")
            conn.execute("UPDATE messages SET status='completed',updated_at=? WHERE message_id=?", (_now(), message_id))
            published_ids = list(dict.fromkeys(
                json.loads(event[0])["artifact_id"] for event in conn.execute(
                    """SELECT payload_json FROM events WHERE conversation_id=? AND turn_id=?
                       AND type IN ('artifact.ready','artifact.updated','artifact.renamed') ORDER BY event_id""",
                    (conversation_id, turn_id),
                )
            ))
            if artifact_ids:
                owned = conn.execute(
                    f"SELECT artifact_id FROM artifacts WHERE conversation_id=? AND artifact_id IN ({','.join('?' for _ in artifact_ids)})",
                    (conversation_id, *artifact_ids),
                ).fetchall()
                if len(owned) != len(set(artifact_ids)):
                    raise NotFoundError("Completion references a foreign or missing artifact")
                published_ids = list(dict.fromkeys([*published_ids, *artifact_ids]))
            return self._event(conn, conversation_id, turn_id, "message.completed", {
                "message_id": message_id, "body": row[0], "artifact_ids": published_ids,
            })

    def begin_finalization(self, conversation_id: str, turn_id: str) -> list[dict]:
        """Validate this turn's committed work and close its publish capability."""
        with self._transaction() as conn:
            self._require_running_turn(conn, conversation_id, turn_id)
            turn = conn.execute(
                "SELECT scope_kind,scope_json,outline_id,outline_revision,evaluation_id FROM turns WHERE conversation_id=? AND turn_id=?",
                (conversation_id, turn_id),
            ).fetchone()
            scope = json.loads(turn["scope_json"])
            if scope.get("finalizing"):
                raise ConflictError("Turn is already finalizing", turn_id=turn_id)
            events = [json.loads(row[0]) for row in conn.execute(
                """SELECT payload_json FROM events WHERE conversation_id=? AND turn_id=?
                   AND type IN ('artifact.ready','artifact.updated','artifact.renamed') ORDER BY event_id""",
                (conversation_id, turn_id),
            )]
            if turn["scope_kind"] in ("package_generate", "package_continue"):
                remaining = conn.execute(
                    """SELECT COUNT(*) FROM deliverables WHERE conversation_id=? AND outline_id=?
                       AND outline_revision=? AND status='pending'""",
                    (conversation_id, turn["outline_id"], turn["outline_revision"]),
                ).fetchone()[0]
                if remaining:
                    raise ConflictError("Package still has pending deliverables", turn_id=turn_id)
            elif turn["scope_kind"] == "focused_create":
                delivered = {Path(event["filename"]).stem for event in events if event.get("filename")}
                if not set(scope.get("target_names", [])).issubset(delivered):
                    raise ConflictError("Focused creation did not publish all requested files", turn_id=turn_id)
            elif turn["scope_kind"] in ("focused_edit", "focused_rename"):
                revised = {event["artifact_id"] for event in events}
                if not set(scope.get("target_artifact_ids", [])).issubset(revised):
                    raise ConflictError("Focused edit did not revise all target files", turn_id=turn_id)
            else:
                raise ConflictError("Turn has no files to finalize", turn_id=turn_id)
            issues = self._validation_issues(conn, conversation_id, turn_id, turn, events)
            if issues:
                raise ConflictError("；".join(issues), turn_id=turn_id)
            scope["finalizing"] = True
            conn.execute(
                "UPDATE turns SET scope_json=? WHERE turn_id=?",
                (json.dumps(scope, ensure_ascii=False), turn_id),
            )
            if turn["scope_kind"] in ("package_generate", "package_continue"):
                return [dict(row) for row in conn.execute(
                    """SELECT a.artifact_id,a.filename,a.evaluation_id,a.kind,a.content_revision
                       FROM deliverables d JOIN artifacts a ON a.artifact_id=d.artifact_id
                       WHERE d.conversation_id=? AND d.outline_id=? AND d.outline_revision=?
                       ORDER BY d.rowid""",
                    (conversation_id, turn["outline_id"], turn["outline_revision"]),
                )]
            return events

    def validation_issues(self, conversation_id: str, turn_id: str) -> list[str]:
        """Deterministic scope, file-list and explicit local-reference checks."""
        with closing(self._connect()) as conn:
            self._require_running_turn(conn, conversation_id, turn_id)
            turn = conn.execute(
                "SELECT * FROM turns WHERE conversation_id=? AND turn_id=?",
                (conversation_id, turn_id),
            ).fetchone()
            events = [json.loads(row[0]) for row in conn.execute(
                """SELECT payload_json FROM events WHERE conversation_id=? AND turn_id=?
                   AND type IN ('artifact.ready','artifact.updated','artifact.renamed') ORDER BY event_id""",
                (conversation_id, turn_id),
            )]
            return self._validation_issues(conn, conversation_id, turn_id, turn, events)

    @staticmethod
    def _validation_issues(conn, conversation_id: str, turn_id: str, turn, events: list[dict]) -> list[str]:
        scope = json.loads(turn["scope_json"])
        kind = turn["scope_kind"]
        if kind not in ("package_generate", "package_continue", "focused_create", "focused_edit", "focused_rename"):
            return ["本轮没有可验收的文件任务"]
        issues = []
        if kind.startswith("package_"):
            deliveries = conn.execute(
                """SELECT d.name,d.kind,d.status,d.artifact_id,a.filename,a.kind AS artifact_kind,
                          a.source_path,a.export_path,a.evaluation_id AS artifact_evaluation
                   FROM deliverables d LEFT JOIN artifacts a ON a.artifact_id=d.artifact_id
                   WHERE d.conversation_id=? AND d.evaluation_id=? AND d.outline_id=?
                     AND d.outline_revision=? ORDER BY d.rowid""",
                (conversation_id, turn["evaluation_id"], turn["outline_id"], turn["outline_revision"]),
            ).fetchall()
            if not deliveries:
                issues.append("已确认大纲没有交付清单")
            outline = conn.execute(
                "SELECT body FROM outlines WHERE conversation_id=? AND outline_id=? AND revision=?",
                (conversation_id, turn["outline_id"], turn["outline_revision"]),
            ).fetchone()
            if outline is None or [name for name, _ in parse_outline_deliverables(outline["body"])] != [item["name"] for item in deliveries]:
                issues.append("已确认大纲与交付清单不一致")
            for item in deliveries:
                if item["status"] != "ready" or not item["artifact_id"]:
                    issues.append(f"尚未生成：{item['name']}")
                elif (not WorkspaceStore._artifact_name_matches(conn, conversation_id, item["artifact_id"], item["name"], item["filename"]) or item["kind"] != item["artifact_kind"]
                      or item["artifact_evaluation"] != turn["evaluation_id"]):
                    issues.append(f"交付项与文件不匹配：{item['name']}")
            filenames = {item["name"] for item in deliveries}
            filenames.update(item["filename"] for item in deliveries if item["filename"])
            artifacts = [item for item in deliveries if item["artifact_id"]]
        else:
            artifacts = conn.execute(
                """SELECT * FROM artifacts WHERE conversation_id=? AND evaluation_id=?
                   ORDER BY ready_at,artifact_id""",
                (conversation_id, turn["evaluation_id"]),
            ).fetchall()
            filenames = {item["filename"] for item in artifacts}
            if kind == "focused_create":
                delivered = {Path(event["filename"]).stem for event in events if event.get("filename")}
                for name in scope.get("target_names", []):
                    if name not in delivered:
                        issues.append(f"尚未生成：{name}")
            else:
                revised = {event["artifact_id"] for event in events}
                for artifact_id in scope.get("target_artifact_ids", []):
                    if artifact_id not in revised:
                        issues.append(f"尚未修改目标文件：{artifact_id}")
        filenames.update(row[0] for row in conn.execute(
            """SELECT filename FROM artifact_aliases WHERE conversation_id=? AND evaluation_id=?
               AND artifact_id IN (SELECT artifact_id FROM artifacts WHERE conversation_id=? AND evaluation_id=?)""",
            (conversation_id, turn["evaluation_id"], conversation_id, turn["evaluation_id"]),
        ))
        changed_ids = {event["artifact_id"] for event in events}
        for item in artifacts:
            if kind.startswith("focused_") and item["artifact_id"] not in changed_ids:
                continue
            filename = item["filename"]
            if not item["source_path"] or not item["export_path"]:
                issues.append(f"文件路径缺失：{filename}")
                continue
            source = Path(item["source_path"])
            export = Path(item["export_path"])
            if not source.is_file() or not export.is_file() or not source.stat().st_size or not export.stat().st_size:
                issues.append(f"文件缺失或为空：{filename}")
                continue
            if item["kind"] in ("md", "docx"):
                content = source.read_text(encoding="utf-8")
                for target in _broken_file_links(content, filenames):
                    issues.append(f"{filename} 引用了未交付的文件：{target}")
        return issues

    def request_cancel(self, conversation_id: str, turn_id: str) -> str:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT status FROM turns WHERE conversation_id=? AND turn_id=?", (conversation_id, turn_id)
            ).fetchone()
            if row is None:
                raise NotFoundError("Turn not found")
            if row[0] == "running":
                conn.execute("UPDATE turns SET status='cancel_requested' WHERE turn_id=?", (turn_id,))
                self._event(conn, conversation_id, turn_id, "turn.cancel_requested", {"turn_id": turn_id})
                return "cancel_requested"
            return row[0]

    def finish_turn(self, conversation_id: str, turn_id: str, status: str) -> int:
        if status not in TERMINAL_STATES:
            raise ValueError("Expected terminal state")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT status,started_at,scope_kind,scope_json,evaluation_id,outline_id,outline_revision FROM turns WHERE conversation_id=? AND turn_id=?",
                (conversation_id, turn_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("Turn not found")
            if row["status"] in TERMINAL_STATES:
                return 0
            if status == "completed" and row["status"] != "running":
                raise ConflictError("Cancelled turn cannot complete", turn_id=turn_id)
            if status == "cancelled" and row["status"] != "cancel_requested":
                raise ConflictError("Cancellation was not requested", turn_id=turn_id)
            package_status = None
            if row["scope_kind"] == "focused_edit" and status != "completed":
                scope = json.loads(row["scope_json"])
                updated = list(dict.fromkeys(
                    json.loads(event[0])["artifact_id"] for event in conn.execute(
                        """SELECT payload_json FROM events WHERE conversation_id=? AND turn_id=?
                           AND type='artifact.updated' ORDER BY event_id""",
                        (conversation_id, turn_id),
                    )
                ))
                targets = scope.get("target_artifact_ids", [])
                scope["partial_edit"] = {
                    "updated_artifact_ids": updated,
                    "remaining_artifact_ids": [item for item in targets if item not in updated],
                }
                conn.execute(
                    "UPDATE turns SET scope_json=? WHERE turn_id=?",
                    (json.dumps(scope, ensure_ascii=False), turn_id),
                )
            if row["scope_kind"] in ("package_generate", "package_continue"):
                remaining = conn.execute(
                    """SELECT COUNT(*) FROM deliverables WHERE conversation_id=? AND outline_id=?
                       AND outline_revision=? AND status='pending'""",
                    (conversation_id, row["outline_id"], row["outline_revision"]),
                ).fetchone()[0]
                if status == "completed" and remaining:
                    raise ConflictError("Package still has pending deliverables", turn_id=turn_id)
                package_status = "completed" if status == "completed" else "incomplete"
                conn.execute(
                    "UPDATE evaluations SET package_status=?,updated_at=? WHERE evaluation_id=? AND conversation_id=?",
                    (package_status, _now(), row["evaluation_id"], conversation_id),
                )
            ended = _now()
            duration = max(0, int((datetime.fromisoformat(ended) - datetime.fromisoformat(row["started_at"])).total_seconds() * 1000))
            conn.execute(
                "UPDATE turns SET status=?,ended_at=?,duration_ms=? WHERE turn_id=?",
                (status, ended, duration, turn_id),
            )
            conn.execute(
                "UPDATE messages SET status='failed',updated_at=? WHERE turn_id=? AND status='streaming'",
                (ended, turn_id),
            )
            return self._event(conn, conversation_id, turn_id, f"turn.{status}", {
                "turn_id": turn_id, "started_at": row["started_at"],
                "ended_at": ended, "duration_ms": duration, "package_status": package_status,
            })

    def recover_interrupted_turns(self) -> int:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT conversation_id,turn_id,started_at,evaluation_id,scope_kind FROM turns WHERE status IN ('running','cancel_requested')"
            ).fetchall()
            for row in rows:
                ended = _now()
                duration = max(0, int((datetime.fromisoformat(ended) - datetime.fromisoformat(row["started_at"])).total_seconds() * 1000))
                conn.execute(
                    "UPDATE turns SET status='interrupted',ended_at=?,duration_ms=? WHERE turn_id=?",
                    (ended, duration, row["turn_id"]),
                )
                conn.execute(
                    "UPDATE messages SET status='failed',updated_at=? WHERE turn_id=? AND status='streaming'",
                    (ended, row["turn_id"]),
                )
                package_status = None
                if row["scope_kind"] in ("package_generate", "package_continue"):
                    package_status = "incomplete"
                    conn.execute(
                        "UPDATE evaluations SET package_status='incomplete',updated_at=? WHERE evaluation_id=?",
                        (ended, row["evaluation_id"]),
                    )
                self._event(conn, row["conversation_id"], row["turn_id"], "turn.interrupted", {
                    "turn_id": row["turn_id"], "started_at": row["started_at"],
                    "ended_at": ended, "duration_ms": duration, "package_status": package_status,
                })
            return len(rows)

    def list_events(self, conversation_id: str, *, after: int = 0) -> list[dict]:
        with closing(self._connect()) as conn:
            self._require_conversation(conn, conversation_id)
            return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in conn.execute(
                "SELECT * FROM events WHERE conversation_id=? AND event_id>? ORDER BY event_id",
                (conversation_id, after),
            )]

    def record_context_event(self, conversation_id: str, turn_id: str, kind: str, payload: dict) -> None:
        """Persist context metadata only; never summary text or model inputs."""
        if kind not in {
            "context.usage", "context.compression.started", "context.compression.completed",
            "context.compression.failed", "context.compression.cancelled",
        }:
            raise ValueError("Unknown context event")
        with self._transaction() as conn:
            turn = conn.execute(
                "SELECT status FROM turns WHERE conversation_id=? AND turn_id=?",
                (conversation_id, turn_id),
            ).fetchone()
            # A delayed callback must not append business events after a terminal event.
            if turn is None or turn["status"] not in ("running", "cancel_requested"):
                return
            self._event(conn, conversation_id, turn_id, kind, payload)

    def get_turn(self, conversation_id: str, turn_id: str) -> dict:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM turns WHERE conversation_id=? AND turn_id=?",
                (conversation_id, turn_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("Turn not found")
            return dict(row)

    def get_artifact(self, conversation_id: str, artifact_id: str) -> dict:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE conversation_id=? AND artifact_id=?",
                (conversation_id, artifact_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("Artifact not found")
            return dict(row)

    def list_artifacts(self, conversation_id: str, evaluation_id: str) -> list[dict]:
        with closing(self._connect()) as conn:
            self._require_conversation(conn, conversation_id)
            product = conn.execute(
                "SELECT 1 FROM evaluations WHERE conversation_id=? AND evaluation_id=?",
                (conversation_id, evaluation_id),
            ).fetchone()
            if product is None:
                raise NotFoundError("Evaluation not found in conversation")
            return [dict(row) for row in conn.execute(
                "SELECT * FROM artifacts WHERE conversation_id=? AND evaluation_id=? ORDER BY ready_at,artifact_id",
                (conversation_id, evaluation_id),
            )]

    def snapshot(self, conversation_id: str) -> dict:
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            self._require_conversation(conn, conversation_id)
            conversation = dict(conn.execute(
                "SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)
            ).fetchone())
            result = {"conversation": conversation}
            for table, order in (
                ("evaluations", "created_at"), ("messages", "created_at"),
                ("attachments", "created_at"), ("turns", "started_at"),
                ("evaluation_notes", "created_at"),
                ("outlines", "created_at"), ("deliverables", "rowid"),
                ("artifacts", "ready_at"),
                ("artifact_aliases", "rowid"),
            ):
                result[table] = [dict(row) for row in conn.execute(
                    f"SELECT * FROM {table} WHERE conversation_id=? ORDER BY {order},rowid",
                    (conversation_id,),
                )]
            progress = conn.execute(
                "SELECT turn_id,payload_json FROM events WHERE conversation_id=? AND type='tool.progress' ORDER BY event_id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            result["progress"] = {"turn_id": progress["turn_id"], **json.loads(progress["payload_json"])} if progress else None
            result["last_event_id"] = conn.execute(
                "SELECT COALESCE(MAX(event_id),0) FROM events WHERE conversation_id=?", (conversation_id,)
            ).fetchone()[0]
            result["context"] = {"usage": None, "compression": None}
            for key, condition in (("usage", "type='context.usage'"),
                                   ("compression", "type LIKE 'context.compression.%'")):
                event = conn.execute(
                    f"SELECT type,payload_json,turn_id,created_at FROM events WHERE conversation_id=? AND {condition} ORDER BY event_id DESC LIMIT 1",
                    (conversation_id,),
                ).fetchone()
                if event:
                    result["context"][key] = {
                        **json.loads(event["payload_json"]), "turn_id": event["turn_id"],
                        "updated_at": event["created_at"],
                    }
                    if key == "compression":
                        status = event["type"].rsplit(".", 1)[-1]
                        # Shutdown/crash can preempt the summarizer's callback.
                        turn = next(item for item in result["turns"] if item["turn_id"] == event["turn_id"])
                        if status == "started" and turn["status"] in TERMINAL_STATES:
                            status = turn["status"] if turn["status"] != "completed" else "interrupted"
                        result["context"][key]["status"] = status
            result["server_now"] = _now()
            conn.commit()
            return result
        finally:
            conn.close()
