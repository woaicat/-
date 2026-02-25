"""Dev server entrypoint that avoids reloading on artefact writes."""

from __future__ import annotations

from pathlib import Path

import uvicorn


def _resolve_dir(name: str) -> str:
    return str((Path(__file__).resolve().parent.parent / name).resolve())


def main() -> None:
    reload_dirs = [
        _resolve_dir("app"),
        _resolve_dir("frontend"),
        _resolve_dir("tests"),
    ]
    reload_excludes = [
        _resolve_dir("outputs") + "/*",
        _resolve_dir("uploads") + "/*",
        _resolve_dir("sample_outputs") + "/*",
    ]

    config = uvicorn.Config(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        reload_dirs=reload_dirs,
        reload_excludes=reload_excludes,
    )
    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
