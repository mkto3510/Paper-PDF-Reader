"""Locate the per-installation data directory."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile


def data_dir() -> Path:
    root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    override = os.environ.get("PAPER_PDF_READER_DATA_DIR")
    if override:
        folder = Path(override)
        folder.mkdir(parents=True, exist_ok=True)
        return folder
    for folder in (root / "data", Path(os.getenv("LOCALAPPDATA", Path.home())) / "PaperPdfReader"):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=folder):
                pass
            return folder
        except OSError as error:
            last_error = error
    raise last_error


def write_json(path: Path, data: dict) -> None:
    with NamedTemporaryFile(dir=path.parent, delete=False, mode="w", encoding="utf-8") as temp:
        draft = Path(temp.name)
        try:
            json.dump(data, temp, ensure_ascii=False, indent=2)
            temp.close()
            os.replace(draft, path)
        finally:
            draft.unlink(missing_ok=True)
