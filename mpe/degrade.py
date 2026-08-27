"""Controlled degradation of exemplar prompts.

The point is to manufacture labelled pairs without any new human data: take a
prompt that scores well, remove the material for exactly one axis, and the
variant *should* score lower on that axis and nowhere else. If the grader
doesn't see the drop, that axis carries no signal.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from . import llm, schema
from .corpus import CACHE_DIR, Doc

SEVERITIES = ("partial", "full")

_SEVERITY_INSTRUCTION = {
    "partial": (
        "Weaken this axis to a generic gesture: keep a vague sentence in its place "
        "but delete every specific that made it concrete."
    ),
    "full": "Remove every trace of this axis from the prompt.",
}

_SYSTEM = """You are producing an ablated variant of a prompt for a controlled experiment.

You will be given a prompt and one feature to remove from it. Produce the same \
prompt with that feature degraded, and nothing else changed.

Hard rules:
- Delete or weaken only. Never add new content, never rewrite surviving sentences, \
never improve anything, never fix typos.
- Leave all other features exactly as they are, byte for byte where possible.
- Keep the same overall shape: the variant must still read as a serious attempt at \
the same problem, just missing this one thing.
- Do not mention the ablation. Return the variant prompt text only."""

_USER_TEMPLATE = """Feature to degrade: {name} ({axis_id})
What it is: {question}
Fully-present form: {anchor_2}

{instruction}

<prompt>
{prompt}
</prompt>"""

_SCHEMA = {
    "type": "object",
    "properties": {
        "variant": {"type": "string", "description": "The full ablated prompt text."},
        "removed": {
            "type": "string",
            "description": "One sentence naming what was taken out.",
        },
    },
    "required": ["variant", "removed"],
    "additionalProperties": False,
}


@dataclass
class Variant:
    source: str
    axis: str
    severity: str
    text: str
    removed: str

    @property
    def name(self) -> str:
        return f"{self.source}::{self.axis}::{self.severity}"


def _cache_key(source: str, axis: str, severity: str, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"{source}__{axis}__{severity}__{digest}"


def make_variant(
    doc: Doc,
    axis: Dict[str, str],
    severity: str,
    *,
    effort: str = "medium",
    cache_dir: Optional[Path] = None,
) -> Variant:
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {SEVERITIES}, got {severity!r}")

    cache_dir = Path(cache_dir or (CACHE_DIR / "variants"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / (_cache_key(doc.name, axis["id"], severity, doc.text) + ".json")
    if cache_file.exists():
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
        return Variant(**raw)

    user = _USER_TEMPLATE.format(
        name=axis["name"],
        axis_id=axis["id"],
        question=axis["question"],
        anchor_2=axis["anchor_2"],
        instruction=_SEVERITY_INSTRUCTION[severity],
        prompt=doc.text,
    )
    obj, _ = llm.complete_json(_SYSTEM, user, _SCHEMA, effort=effort)
    variant = Variant(
        source=doc.name,
        axis=axis["id"],
        severity=severity,
        text=obj["variant"],
        removed=obj["removed"],
    )
    cache_file.write_text(json.dumps(variant.__dict__, indent=2), encoding="utf-8")
    return variant


def make_ladder(
    docs: List[Doc],
    axes: Optional[List[Dict[str, str]]] = None,
    severities=SEVERITIES,
    *,
    effort: str = "medium",
) -> List[Variant]:
    """Every (doc, axis, severity) ablation. Cost scales as the product — subset first."""
    axes = axes or schema.DEFAULT_AXES
    variants = []
    for doc in docs:
        for axis in axes:
            for severity in severities:
                variants.append(make_variant(doc, axis, severity, effort=effort))
    return variants
