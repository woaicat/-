"""Thread-local draft files with per-turn write restrictions."""

from pathlib import PurePosixPath

from deepagents.backends import StateBackend
from deepagents.backends.protocol import EditResult, WriteResult
from langgraph.config import get_config


class ScopedStateBackend(StateBackend):
    """Keep files in graph state while limiting model writes to this turn.

    The framework writes its evicted history under /conversation_history.
    FilesystemMiddleware denies model writes there; only the internal summary
    middleware reaches this backend path.
    """

    @staticmethod
    def _may_write(file_path: str) -> bool:
        path = PurePosixPath(file_path)
        if not file_path.startswith("/") or ".." in file_path.split("/"):
            return False
        if str(path).startswith("/conversation_history/"):
            return True
        try:
            turn_id = get_config().get("configurable", {}).get("turn_id")
        except RuntimeError:
            return False
        if not isinstance(turn_id, str) or not turn_id or "/" in turn_id or turn_id in (".", ".."):
            return False
        return str(path).startswith(f"/drafts/{turn_id}/")

    def write(self, file_path: str, content: str) -> WriteResult:
        if not self._may_write(file_path):
            return WriteResult(error="Permission denied: drafts belong to the current turn")
        return super().write(file_path, content)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        if not self._may_write(file_path):
            return EditResult(error="Permission denied: drafts belong to the current turn")
        return super().edit(file_path, old_string, new_string, replace_all)
