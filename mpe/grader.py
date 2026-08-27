"""Per-axis grading of a research-math prompt, and comparison against the corpus."""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import llm, schema
from .corpus import CACHE_DIR, Doc, load_exemplars

_SYSTEM_TEMPLATE = """You are grading a *prompt*, not an answer.

The prompt was written by a researcher to get an AI system to attack an open \
mathematics problem. You are assessing how well the prompt specifies the work — \
whether a capable solver could tell what is being asked, what would count as done, \
and what would not.

You are NOT assessing whether the mathematics is interesting, whether the problem is \
solvable, or whether you agree with the approach. A prompt about a trivial problem can \
score full marks; a prompt about a deep problem can score zero.

Grade each axis 0, 1, or 2 against the anchors below. For every axis, quote a short \
verbatim span from the prompt as evidence. If an axis scores 0, set evidence to the \
empty string and say in the note what is missing.

Be strict about the difference between 1 and 2. A 2 requires the specific, observable \
form described in the anchor — a generic gesture in the right direction is a 1.

## Rubric

{rubric}
"""

_USER_TEMPLATE = """Grade this prompt.

<prompt>
{prompt}
</prompt>"""


def _grading_schema(axes: List[Dict[str, str]]) -> Dict[str, Any]:
    axis_ids = [a["id"] for a in axes]
    return {
        "type": "object",
        "properties": {
            "axis_scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "axis": {"type": "string", "enum": axis_ids},
                        "score": {"type": "integer", "enum": [0, 1, 2]},
                        "evidence": {
                            "type": "string",
                            "description": "Verbatim span from the prompt, or empty if score is 0.",
                        },
                        "note": {
                            "type": "string",
                            "description": "One sentence: why this score and not the one above.",
                        },
                    },
                    "required": ["axis", "score", "evidence", "note"],
                    "additionalProperties": False,
                },
            },
            "top_gaps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The two or three changes that would most improve this prompt.",
            },
        },
        "required": ["axis_scores", "top_gaps"],
        "additionalProperties": False,
    }


@dataclass
class AxisScore:
    axis: str
    score: int
    evidence: str
    note: str


@dataclass
class Grade:
    name: str
    axis_scores: List[AxisScore]
    top_gaps: List[str]
    axes: List[Dict[str, str]] = field(default_factory=list)

    def score_map(self) -> Dict[str, int]:
        return {a.axis: a.score for a in self.axis_scores}

    def subscale(self, which: str) -> int:
        ids = {a["id"] for a in self.axes if a["subscale"] == which}
        return sum(a.score for a in self.axis_scores if a.axis in ids)

    def subscale_max(self, which: str) -> int:
        n = sum(1 for a in self.axes if a["subscale"] == which)
        return n * schema.MAX_SCORE_PER_AXIS

    @property
    def total(self) -> int:
        return sum(a.score for a in self.axis_scores)

    @property
    def max_total(self) -> int:
        return len(self.axis_scores) * schema.MAX_SCORE_PER_AXIS

    @property
    def pct(self) -> float:
        return 100.0 * self.total / self.max_total if self.max_total else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "axis_scores": [asdict(a) for a in self.axis_scores],
            "top_gaps": self.top_gaps,
            "total": self.total,
            "max_total": self.max_total,
            "pct": self.pct,
        }


def grade(
    prompt: str,
    *,
    name: str = "prompt",
    axes: Optional[List[Dict[str, str]]] = None,
    effort: str = "high",
) -> Grade:
    """Grade one prompt against the rubric."""
    axes = axes or schema.DEFAULT_AXES
    system = _SYSTEM_TEMPLATE.format(rubric=schema.render_rubric(axes))
    user = _USER_TEMPLATE.format(prompt=prompt)
    obj, _ = llm.complete_json(system, user, _grading_schema(axes), effort=effort)

    scored = {row["axis"]: row for row in obj["axis_scores"]}
    axis_scores = []
    for axis in axes:  # canonical order, and fail loudly on a missing axis
        row = scored.get(axis["id"])
        if row is None:
            raise ValueError(f"model omitted axis {axis['id']!r} from its grading")
        axis_scores.append(
            AxisScore(
                axis=row["axis"],
                score=int(row["score"]),
                evidence=row["evidence"],
                note=row["note"],
            )
        )
    return Grade(
        name=name, axis_scores=axis_scores, top_gaps=obj.get("top_gaps", []), axes=axes
    )


def grade_corpus(
    *,
    axes: Optional[List[Dict[str, str]]] = None,
    effort: str = "high",
    cache_path: Optional[Path] = None,
    refresh: bool = False,
) -> List[Grade]:
    """Grade every exemplar, caching results (each run costs real tokens)."""
    axes = axes or schema.DEFAULT_AXES
    cache_path = Path(cache_path or (CACHE_DIR / "exemplar_grades.json"))

    if cache_path.exists() and not refresh:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
        return [
            Grade(
                name=g["name"],
                axis_scores=[AxisScore(**a) for a in g["axis_scores"]],
                top_gaps=g["top_gaps"],
                axes=axes,
            )
            for g in raw
        ]

    grades = [
        grade(doc.text, name=doc.name, axes=axes, effort=effort)
        for doc in load_exemplars()
    ]
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps([g.to_dict() for g in grades], indent=2), encoding="utf-8"
    )
    return grades


# --------------------------------------------------------------------------
# Comparison against the corpus — the "same axis as the good examples" view
# --------------------------------------------------------------------------


@dataclass
class Comparison:
    total: int
    max_total: int
    corpus_totals: List[int]
    percentile: float
    per_axis: List[Dict[str, Any]]

    @property
    def corpus_median(self) -> float:
        return statistics.median(self.corpus_totals) if self.corpus_totals else 0.0


def compare(target: Grade, corpus_grades: List[Grade]) -> Comparison:
    """Locate a graded prompt within the corpus distribution, axis by axis."""
    totals = [g.total for g in corpus_grades]
    at_or_below = sum(1 for t in totals if t <= target.total)
    percentile = 100.0 * at_or_below / len(totals) if totals else float("nan")

    target_scores = target.score_map()
    per_axis = []
    for axis in target.axes:
        aid = axis["id"]
        corpus_vals = [g.score_map().get(aid, 0) for g in corpus_grades]
        mean = statistics.mean(corpus_vals) if corpus_vals else 0.0
        per_axis.append(
            {
                "axis": aid,
                "name": axis["name"],
                "subscale": axis["subscale"],
                "score": target_scores.get(aid, 0),
                "corpus_mean": round(mean, 2),
                "gap": round(target_scores.get(aid, 0) - mean, 2),
            }
        )
    per_axis.sort(key=lambda r: r["gap"])
    return Comparison(
        total=target.total,
        max_total=target.max_total,
        corpus_totals=totals,
        percentile=percentile,
        per_axis=per_axis,
    )


def format_report(target: Grade, comparison: Optional[Comparison] = None) -> str:
    """Human-readable report for the terminal or a notebook cell."""
    by_id = {a["id"]: a for a in target.axes}
    lines = [
        f"PROMPT: {target.name}",
        f"SCORE:  {target.total}/{target.max_total}  ({target.pct:.0f}%)",
    ]
    for sub in (schema.SPEC, schema.PROC):
        if any(a["subscale"] == sub for a in target.axes):
            lines.append(
                f"  {sub:<15} {target.subscale(sub)}/{target.subscale_max(sub)}"
            )
    if comparison is not None:
        lines.append(
            f"  vs corpus       median {comparison.corpus_median:.1f}/"
            f"{comparison.max_total}, this prompt at the {comparison.percentile:.0f}th percentile"
        )
    lines.append("")
    lines.append("PER AXIS")
    for a in target.axis_scores:
        axis = by_id.get(a.axis, {"name": a.axis})
        bar = "#" * a.score + "." * (schema.MAX_SCORE_PER_AXIS - a.score)
        lines.append(f"  [{bar}] {a.score}  {axis['name']}")
        lines.append(f"          {a.note}")
        if a.evidence:
            ev = a.evidence.replace("\n", " ")
            if len(ev) > 110:
                ev = ev[:107] + "..."
            lines.append(f'          > "{ev}"')
    if comparison is not None:
        weakest = [r for r in comparison.per_axis if r["gap"] < 0][:4]
        if weakest:
            lines.append("")
            lines.append("BIGGEST GAPS VS THE EXEMPLARS")
            for r in weakest:
                lines.append(
                    f"  {r['name']:<32} you {r['score']}  corpus mean {r['corpus_mean']}"
                )
    if target.top_gaps:
        lines.append("")
        lines.append("SUGGESTED FIXES")
        for gap in target.top_gaps:
            lines.append(f"  - {gap}")
    return "\n".join(lines)
