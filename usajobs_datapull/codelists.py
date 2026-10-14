"""USAJOBS code lists -> data/reference/codelists/<date>/ (not implemented yet).

Files are named <list>_<timestamp>.json. Endpoints that 404 are skipped.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config


def latest_dir(root: Path | None = None) -> Path | None:
    """The newest dated folder under data/reference/codelists/, if any."""
    root = root or config.CODELISTS
    dirs = sorted(d for d in root.glob("*") if d.is_dir()) if root.exists() else []
    return dirs[-1] if dirs else None


def load(name: str, directory: Path | None) -> dict[str, str]:
    """Return {CODE: label} for one code list, e.g. load("hiringpaths", dir).

    Codes are upper-cased, because postings use lower-case codes
    ("fed-competitive") where the code list uses upper-case ("FED-COMPETITIVE").
    Missing directory or file -> {} (labels fall back to the raw code).
    """
    if directory is None:
        return {}
    files = sorted(directory.glob(f"{name}_*.json"))
    if not files:
        return {}
    with open(files[-1], encoding="utf-8") as fh:
        data = json.load(fh)
    values = data["CodeList"][0]["ValidValue"]
    return {str(v["Code"]).upper(): v["Value"] for v in values}


def run() -> None:
    config.ensure_dirs()
    raise NotImplementedError("codelists fetch not implemented yet")
