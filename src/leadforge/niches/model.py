"""Niche configuration: everything that changes when the target industry changes."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class Categories(BaseModel):
    include: list[str]
    optional: list[str] = []
    exclude: list[str] = []


class KeywordPack(BaseModel):
    language: str
    core: list[str]
    synonyms: list[str] = []
    materials: list[str] = []
    exclusions: list[str] = []


class Qualification(BaseModel):
    relevant_if: str
    irrelevant_if: str


class DescriptionRules(BaseModel):
    must_mention: list[str]
    max_words: int = 60
    hook_max_words: int = 25


class Enrichment(BaseModel):
    max_pages: int = 5
    domain_budget_s: int = 20
    page_keywords: list[str] = []


class Sources(BaseModel):
    prefer: list[str] = []
    disable: list[str] = []


class SheetColumn(BaseModel):
    header: str
    field: str
    owner: Literal["research", "sales", "shared_append", "computed"]


class NicheConfig(BaseModel):
    niche: str
    display_name: str
    categories: Categories
    industry_enum: str = Field(description="Lists tab column holding the allowed industry types")
    material_enum: str
    keyword_packs: dict[str, KeywordPack]
    qualification: Qualification
    description: DescriptionRules
    enrichment: Enrichment = Enrichment()
    sources: Sources = Sources()
    sheet_columns: list[SheetColumn] = []


def load_niche(root: Path, name: str) -> NicheConfig:
    path = root / "config" / "niches" / f"{name}.yaml"
    return NicheConfig(**yaml.safe_load(path.read_text(encoding="utf-8")))


def check_against_db(cfg: NicheConfig, conn: sqlite3.Connection) -> list[str]:
    """Problems where the niche refers to values the controlled lists do not contain."""
    problems: list[str] = []

    def enum(name: str) -> set[str]:
        return {r[0] for r in conn.execute("SELECT value FROM enum_value WHERE list_name=?", (name,))}

    industries = enum(cfg.industry_enum)
    if not industries:
        problems.append(f"Lists has no column {cfg.industry_enum!r}")
    for c in [*cfg.categories.include, *cfg.categories.optional]:
        if industries and c not in industries:
            problems.append(f"category {c!r} is not in Lists → {cfg.industry_enum}")
    if not enum(cfg.material_enum):
        problems.append(f"Lists has no column {cfg.material_enum!r}")
    overlap = set(cfg.categories.include) & set(cfg.categories.exclude)
    if overlap:
        problems.append(f"categories both included and excluded: {sorted(overlap)}")
    return problems
