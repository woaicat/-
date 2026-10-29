"""Product actions; all identities and write scopes come from runtime."""

import json
from typing import Literal

from deepagents.backends.utils import file_data_to_string
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel

from evaluation_agent.artifacts import ArtifactPublisher
from evaluation_agent.scoped_backend import ScopedStateBackend
from evaluation_agent.store import ConflictError, NotFoundError, WorkspaceStore


class RenameInput(BaseModel):
    artifact_id: str
    filename: str


def _ids(runtime: ToolRuntime) -> tuple[str, str]:
    config = runtime.config.get("configurable", {})
    conversation_id, turn_id = config.get("conversation_id"), config.get("turn_id")
    if not isinstance(conversation_id, str) or not isinstance(turn_id, str):
        raise ValueError("Missing server-provided conversation or turn context")
    return conversation_id, turn_id


def _response(action):
    try:
        return json.dumps({"ok": True, "result": action()}, ensure_ascii=False)
    except (ValueError, ConflictError, NotFoundError) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


def create_product_tools(
    store: WorkspaceStore,
    publisher: ArtifactPublisher,
    backend: ScopedStateBackend,
) -> list[BaseTool]:
    @tool
    def select_evaluation(
        scope_kind: Literal["discussion", "package_outline", "package_continue", "focused_create", "focused_edit", "focused_rename"],
        runtime: ToolRuntime,
        evaluation_id: str | None = None,
        product_name: str | None = None,
        target_names: list[str] | None = None,
        target_artifact_ids: list[str] | None = None,
        attachment_ids: list[str] | None = None,
    ) -> str:
        """Choose a product and one exact scope before writing or publishing.

        For a new full package or substantial replanning, first read relevant
        materials and assess the object/scenario, inputs/outputs and scope,
        evaluation purpose/priority risks, and correctness basis.
        Ask the user about missing information that changes the evaluation design
        BEFORE choosing package_outline. Ordinary clarification needs no tool;
        use discussion only if a clear product/material binding is needed while
        asking. A turn's scope can only be chosen once: discussion cannot be
        upgraded to package_outline in the same turn. Do not select discussion
        merely to read an attachment that can already be read from /materials/.
        Use package_outline when information suffices, or the user explicitly asks
        for an exploratory draft with labeled assumptions and open questions.
        Missing samples/gold answers do not block planning if a correctness basis
        or human-review process is known. Do not invent business facts to fill gaps.
        Use package_continue for a confirmed incomplete package; focused_create for a few explicitly
        named new files; focused_edit for edits to existing artifact IDs, including
        an edit that changes several existing files at once. For focused_edit,
        provide all target_artifact_ids in this first call from the current
        artifact index, never deliverable IDs. The scope cannot be expanded
        after selection. Use evaluation_id for an existing product or
        product_name for a new one. These are the only valid scope_kind values.
        For filename-only changes use focused_rename with all target_artifact_ids,
        then rename_artifacts once; never rewrite file contents to simulate renaming.
        """
        return _response(lambda: store.select_evaluation_scope(
            *_ids(runtime), scope_kind=scope_kind, evaluation_id=evaluation_id,
            product_name=product_name, target_names=target_names,
            target_artifact_ids=target_artifact_ids, attachment_ids=attachment_ids,
        ))

    @tool
    def submit_outline(body: str, runtime: ToolRuntime) -> str:
        """Submit an editable package outline with goals, metrics, samples and execution.

        Only after assessing the four requirement dimensions and resolving critical
        gaps through chat in earlier turns, unless the user explicitly requests an
        exploratory draft. Do not use this tool as a substitute for clarification.
        If information was available in the request/history/materials, proceed
        without redundant questions. Distinguish facts, assumptions and open items;
        suggested thresholds are not confirmed acceptance criteria.
        Include a deliverables section headed '## 产物清单' and list filenames,
        for example '- 评测方案.md'. Only .md, .csv and .docx are supported.
        """
        return _response(lambda: store.submit_outline(*_ids(runtime), body))

    @tool
    def publish_artifact(
        draft_path: str,
        filename: str,
        runtime: ToolRuntime,
        deliverable_id: str | None = None,
        artifact_id: str | None = None,
    ) -> str:
        """Publish one authorized current-turn draft as soon as that file is ready.

        For a package, publish each completed file before drafting the next one.
        This makes each file visible and previewable immediately during the run.
        """
        def publish():
            conversation_id, turn_id = _ids(runtime)
            if not draft_path.startswith(f"/drafts/{turn_id}/") or ".." in draft_path.split("/"):
                raise ValueError("Draft path is outside this turn")
            result = backend.read(draft_path, offset=0, limit=1_000_000)
            if result.error or result.file_data is None:
                raise ValueError(result.error or "Draft not found")
            content = file_data_to_string(result.file_data)
            return publisher.publish(
                conversation_id, turn_id, draft_path=draft_path,
                filename=filename, content=content,
                deliverable_id=deliverable_id, artifact_id=artifact_id,
            )

        return _response(publish)

    @tool
    def rename_artifacts(renames: list[RenameInput], runtime: ToolRuntime) -> str:
        """Atomically rename the entire focused_rename scope in one batch.

        Each pair contains artifact_id and destination filename. Keep extensions.
        IDs, content bytes and content revisions remain unchanged; old names
        remain aliases for in-app file links. Do not read/rewrite full documents
        for filename-only changes. Cannot be used to edit or create file content.
        """
        return _response(lambda: store.rename_artifacts(
            *_ids(runtime), [item.model_dump() for item in renames],
        ))

    return [select_evaluation, submit_outline, publish_artifact, rename_artifacts]
