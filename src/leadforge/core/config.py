"""Load config/app.yaml and resolve paths against the project root."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel


class Paths(BaseModel):
    database: Path
    exports: Path
    cache: Path


class Workbook(BaseModel):
    legacy_xlsx: Path


class AppConfig(BaseModel):
    root: Path
    paths: Paths
    workbook: Workbook
    concurrency: dict[str, Any] = {}
    enrichment: dict[str, Any] = {}
    ai: dict[str, Any] = {}

    def resolve(self, p: Path) -> Path:
        return p if p.is_absolute() else self.root / p


def find_root(start: Path | None = None) -> Path:
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / "config" / "app.yaml").exists():
            return d
    raise FileNotFoundError("config/app.yaml not found in this directory or any parent")


def load_app_config(root: Path | None = None) -> AppConfig:
    root = root or find_root()
    data = yaml.safe_load((root / "config" / "app.yaml").read_text(encoding="utf-8"))
    return AppConfig(root=root, **data)
