"""The contract between leadforge and the AI agent: instructions, input shape, output schema."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

PROMPT_VERSION = "2026-10-12.1"


class AIResult(BaseModel):
    """One object per input business, in the agent's results file."""
    id: str
    relevant: Literal["yes", "no", "unsure"]
    industry_type: str | None = None
    material_focus: list[str] = Field(default_factory=list)
    description: str | None = None
    personalisation_hook: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"]
    concerns: list[str] = Field(default_factory=list)

    @field_validator("description", "personalisation_hook")
    @classmethod
    def strip(cls, v: str | None) -> str | None:
        v = (v or "").strip()
        return v or None


def instructions(niche_display: str, relevant_if: str, irrelevant_if: str, must_mention: list[str],
                 max_words: int, hook_max_words: int, industry_values: list[str], material_values: list[str]) -> str:
    return f"""You are qualifying businesses for: {niche_display}.

For EVERY business in `records`, return one result object (see `output_schema`), matched by `id`.
Write all results for this batch as a JSON array to the file named in `results_file`.

Rules
1. Use ONLY the text in that business's `evidence`. Do not use outside knowledge or guess.
2. relevant = "yes" if: {relevant_if}
   relevant = "no" if: {irrelevant_if}
   relevant = "unsure" when the evidence is too thin to tell.
3. industry_type: exactly one value from `allowed.industry_type`, or null.
   material_focus: zero or more values from `allowed.material_focus`.
4. description: {max_words} words at most, plain factual English. Cover, where the evidence supports it:
   {", ".join(must_mention)}. After each claim put the source tag in square brackets, e.g. [S2].
   No marketing language ("leading", "one-stop shop", "premier", "best"). No size, volume, revenue,
   customer names or years unless the evidence states them.
5. personalisation_hook: one specific, verifiable detail ({hook_max_words} words at most) a sales email could
   mention, with its source tag. null if nothing specific exists.
6. evidence_ids: the source tags you relied on.
7. confidence: high = clear evidence for relevance and description; medium = some gaps; low = thin or conflicting.
8. concerns: short notes on contradictions, a possibly wrong website, closed business, or weak evidence.
9. If a field is unknown, use null or an empty list. Never invent facts.

Allowed industry_type values: {industry_values}
Allowed material_focus values: {material_values}"""


OUTPUT_SCHEMA = AIResult.model_json_schema()
