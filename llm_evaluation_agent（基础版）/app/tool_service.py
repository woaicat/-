"""Utility service exposing file-oriented tools to the agent."""

from __future__ import annotations

import csv
from collections import OrderedDict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .file_manager import (
    create_request_directory,
    sanitize_filename,
    sanitize_path_fragment,
    write_markdown as persist_markdown,
)
from .models import GeneratedFile


class ToolService:
    """Encapsulates filesystem tooling for artefact generation."""

    def __init__(self, request_id: str, product_name: str):
        self.request_id = request_id
        self.product_name = product_name
        self.output_dir = create_request_directory(request_id, product_name)
        self._files: "OrderedDict[str, GeneratedFile]" = OrderedDict()
        self._invocations: List[Dict[str, str]] = []

    # ------------------------------------------------------------------
    # Directory & file helpers
    # ------------------------------------------------------------------
    def create_directory(self, relative_path: str) -> Path:
        """Create a subdirectory under the request output folder."""

        safe_fragment = sanitize_path_fragment(relative_path, fallback="artifacts")
        target = self.output_dir / safe_fragment
        target.mkdir(parents=True, exist_ok=True)
        self._log_invocation("create_directory", safe_fragment)
        return target

    def write_markdown(
        self,
        filename: str,
        content: str,
        description: str | None = None,
        directory: Path | None = None,
    ) -> GeneratedFile:
        """Persist markdown content and register it as an artefact."""

        target_dir = directory or self.output_dir
        markdown_target = self._prepare_markdown_path(target_dir, filename)
        self._remove_existing_artifact(markdown_target.name, markdown_target)
        path = persist_markdown(target_dir, filename, content)
        self._log_invocation("write_markdown", path.name)
        return self._register_file(path, description or "Word 文档")

    def write_text(
        self,
        filename: str,
        content: str,
        description: str | None = None,
        directory: Path | None = None,
    ) -> GeneratedFile:
        """Persist plain text content."""

        return self._write_text_file(
            filename=filename,
            content=content,
            description=description or "文本文件",
            directory=directory,
        )

    def write_csv(
        self,
        filename: str,
        rows: Sequence[Sequence[str]],
        description: str | None = None,
        directory: Path | None = None,
    ) -> GeneratedFile:
        """Persist a CSV file."""

        path = self._prepare_file_path(filename, directory)
        with path.open("w", encoding="utf-8", newline="") as csv_file:
            writer = csv.writer(csv_file)
            for row in rows:
                writer.writerow(row)
        self._log_invocation("write_csv", path.name)
        return self._register_file(path, description or "CSV 数据文件")

    # ------------------------------------------------------------------
    def list_generated_files(self) -> List[GeneratedFile]:
        """Return all artefacts registered so far."""

        return list(self._files.values())

    # ------------------------------------------------------------------
    def _write_text_file(
        self,
        filename: str,
        content: str,
        description: str,
        directory: Path | None,
    ) -> GeneratedFile:
        path = self._prepare_file_path(filename, directory)
        path.write_text(content, encoding="utf-8")
        self._log_invocation("write_text", path.name)
        return self._register_file(path, description)

    def _register_file(self, path: Path, description: str) -> GeneratedFile:
        artefact = GeneratedFile(
            filename=path.name,
            path=path,
            download_url=self._make_download_url(path.name),
            preview_url=self._make_preview_url(path.name),
            description=description,
        )
        existing = self._files.pop(artefact.filename, None)
        if existing:
            try:
                existing.path.unlink(missing_ok=True)
            except OSError:
                pass
        self._files[artefact.filename] = artefact
        return artefact

    def _prepare_file_path(self, filename: str, directory: Path | None) -> Path:
        target_dir = directory or self.output_dir
        path = target_dir / sanitize_filename(filename)
        self._remove_existing_artifact(path.name, path)
        return path

    def _prepare_markdown_path(self, directory: Path, filename: str) -> Path:
        safe_name = sanitize_filename(filename)
        docx_name = Path(safe_name).with_suffix(".docx").name
        return directory / docx_name

    def _remove_existing_artifact(self, filename: str, target_path: Path) -> None:
        existing = self._files.pop(filename, None)
        if existing:
            try:
                existing.path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            target_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _make_download_url(self, filename: str) -> str:
        return f"/download/{self.request_id}/{filename}"

    def _make_preview_url(self, filename: str) -> str:
        return f"/preview/{self.request_id}/{filename}"

    def _log_invocation(self, name: str, target: str, details: Optional[str] = None) -> None:
        entry: Dict[str, str] = {"name": name, "target": target}
        if details:
            entry["details"] = details
        self._invocations.append(entry)

    def flush_invocations(self) -> List[Dict[str, str]]:
        invocations = list(self._invocations)
        self._invocations.clear()
        return invocations
