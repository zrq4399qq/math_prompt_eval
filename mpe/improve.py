"""Propose rubric-targeted edits to a prompt, then apply and re-grade them.

The hazard this module is built around: a rewriter told to raise a rubric score
will happily invent content to raise it. On these axes that means inventing
mathematics — plausible-sounding "insufficient reductions" that are not actually
insufficient, named pitfalls that are not real traps, reformulations that are
wrong. That would raise the score while making the prompt worse, which is the
exact failure the grader exists to detect.

So edits are never applied silently. They come back as discrete, reviewable
blocks, each tagged with whether it asserts something mathematical. Axes that
cannot be improved without domain knowledge are flagged by construction, not by
the model's self-report alone.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from . import llm, schema
from .grader import Grade, grade

# Which axes can be raised by restating what the author already knows
# (STRUCTURAL), and which require asserting new mathematics (MATHEMATICAL).
# An edit on a MATHEMATICAL axis always needs author review, whatever the model
# says about itself.
STRUCTURAL = "structural"
MATHEMATICAL = "mathematical"

AXIS_SAFETY: Dict[str, str] = {
    "formal_setup": MATHEMATICAL,
    "target_claim": STRUCTURAL,
    "success_criteria": MATHEMATICAL,
    "ruled_out_shortcuts": MATHEMATICAL,
    "pitfall_warnings": MATHEMATICAL,
    "resource_bounds": MATHEMATICAL,
    "reformulations": MATHEMATICAL,
    "no_answer_leak": STRUCTURAL,
    "verification_demand": STRUCTURAL,
    "search_policy": STRUCTURAL,
    "stop_condition": STRUCTURAL,
    "contamination_control": STRUCTURAL,
}


def axis_safety(axis_id: str) -> str:
    """Unknown axes (e.g. from induce_axes) are treated as mathematical."""
    return AXIS_SAFETY.get(axis_id, MATHEMATICAL)


# --------------------------------------------------------------------------
# Pass 1 — propose
# --------------------------------------------------------------------------

_PROPOSE_SYSTEM = """You are improving a prompt that a researcher wrote to get an AI \
system to attack an open mathematics problem. A rubric has been applied to it and some \
axes scored below full marks. Propose edits that would raise those axes.

The prompt's mathematics belongs to its author. You are improving how the work is \
specified, not what is being claimed.

Hard rules:

1. Never invent mathematics. Do not assert that some reduction is insufficient, that \
some approach is a known trap, that some reformulation is valid, or that some theorem \
may be used, unless the prompt itself already establishes it. Where raising an axis \
would require a mathematical fact the prompt does not supply, say so in `cannot_fix` \
instead of writing the edit.

2. No generic boilerplate. An edit that could be pasted into a prompt about any other \
problem is worthless: it will raise the score without improving the prompt, which is \
the failure mode this whole exercise exists to catch. Every edit must name objects, \
quantities, or conditions specific to this prompt.

3. Prefer making explicit what the prompt already implies. The best edit takes something \
the author clearly intended but left implicit, and states it in the form the rubric asks \
for. That is the edit that adds real specification without adding new claims.

4. Set `introduces_mathematical_claim` to true if your edit asserts anything the reader \
would have to check for correctness. When unsure, set it to true.

5. Do not touch axes already at 2. Do not restructure or reword the prompt at large.

Write in the prompt's existing voice and notation."""

_PROPOSE_USER = """Rubric:

{rubric}

Current scores:

{scores}

Raise these axes: {targets}

<prompt>
{prompt}
</prompt>"""


def _propose_schema(axes: Sequence[Dict[str, str]]) -> Dict[str, Any]:
    axis_ids = [a["id"] for a in axes]
    return {
        "type": "object",
        "properties": {
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Short handle, e.g. e1."},
                        "axis": {"type": "string", "enum": axis_ids},
                        "target_score": {"type": "integer", "enum": [1, 2]},
                        "kind": {
                            "type": "string",
                            "enum": ["add", "replace", "strengthen"],
                        },
                        "anchor": {
                            "type": "string",
                            "description": "Verbatim span the edit follows or replaces; empty to append at the end.",
                        },
                        "text": {"type": "string", "description": "The proposed text."},
                        "rationale": {
                            "type": "string",
                            "description": "One sentence: why this moves the axis.",
                        },
                        "introduces_mathematical_claim": {"type": "boolean"},
                        "claims_to_verify": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Assertions the author must confirm before accepting.",
                        },
                    },
                    "required": [
                        "id",
                        "axis",
                        "target_score",
                        "kind",
                        "anchor",
                        "text",
                        "rationale",
                        "introduces_mathematical_claim",
                        "claims_to_verify",
                    ],
                    "additionalProperties": False,
                },
            },
            "cannot_fix": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "axis": {"type": "string", "enum": axis_ids},
                        "why": {
                            "type": "string",
                            "description": "What the author would have to supply.",
                        },
                    },
                    "required": ["axis", "why"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["edits", "cannot_fix"],
        "additionalProperties": False,
    }


@dataclass
class Edit:
    id: str
    axis: str
    target_score: int
    kind: str
    anchor: str
    text: str
    rationale: str
    introduces_mathematical_claim: bool
    claims_to_verify: List[str]
    axis_safety: str = STRUCTURAL

    @property
    def needs_review(self) -> bool:
        """Either the axis inherently requires domain knowledge, or the model
        flagged its own edit. Both routes force review."""
        return self.axis_safety == MATHEMATICAL or self.introduces_mathematical_claim


@dataclass
class Proposal:
    edits: List[Edit]
    cannot_fix: List[Dict[str, str]]
    base_grade: Grade

    def safe(self) -> List[Edit]:
        return [e for e in self.edits if not e.needs_review]

    def needing_review(self) -> List[Edit]:
        return [e for e in self.edits if e.needs_review]

    def by_id(self) -> Dict[str, Edit]:
        return {e.id: e for e in self.edits}


def propose(
    prompt: str,
    base_grade: Optional[Grade] = None,
    *,
    axes: Optional[List[Dict[str, str]]] = None,
    max_score: int = schema.MAX_SCORE_PER_AXIS,
    effort: str = "high",
) -> Proposal:
    """Propose edits for every axis scoring below `max_score`."""
    axes = axes or schema.DEFAULT_AXES
    base_grade = base_grade or grade(prompt, axes=axes, effort=effort)

    scores = base_grade.score_map()
    names = {a["id"]: a["name"] for a in axes}
    targets = [aid for aid, s in scores.items() if s < max_score]
    if not targets:
        return Proposal(edits=[], cannot_fix=[], base_grade=base_grade)

    score_lines = "\n".join(
        f"  {names.get(a.axis, a.axis)} ({a.axis}): {a.score}/{schema.MAX_SCORE_PER_AXIS}"
        f" — {a.note}"
        for a in base_grade.axis_scores
    )
    user = _PROPOSE_USER.format(
        rubric=schema.render_rubric([a for a in axes if a["id"] in targets]),
        scores=score_lines,
        targets=", ".join(targets),
        prompt=prompt,
    )
    obj, _ = llm.complete_json(
        _PROPOSE_SYSTEM, user, _propose_schema(axes), effort=effort
    )

    edits = [
        Edit(
            id=e["id"],
            axis=e["axis"],
            target_score=int(e["target_score"]),
            kind=e["kind"],
            anchor=e["anchor"],
            text=e["text"],
            rationale=e["rationale"],
            introduces_mathematical_claim=bool(e["introduces_mathematical_claim"]),
            claims_to_verify=e["claims_to_verify"],
            axis_safety=axis_safety(e["axis"]),
        )
        for e in obj["edits"]
    ]
    return Proposal(edits=edits, cannot_fix=obj["cannot_fix"], base_grade=base_grade)


# --------------------------------------------------------------------------
# Pass 2 — apply
# --------------------------------------------------------------------------

_APPLY_SYSTEM = """You are integrating a fixed list of approved edits into a prompt.

Apply exactly the edits given. Do not add anything else, do not reword or reorder \
surviving text, do not fix typos, do not improve anything you were not asked to.

Place each edit where it belongs in the prompt's structure — next to related material \
rather than appended at the end — and match the surrounding formatting and notation. \
If an edit's `anchor` names a span, put the edit there.

Return the complete revised prompt."""

_APPLY_USER = """Approved edits:

{edits}

<prompt>
{prompt}
</prompt>"""

_APPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "revised": {"type": "string", "description": "The complete revised prompt."},
        "applied": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Edit ids actually integrated.",
        },
        "notes": {
            "type": "string",
            "description": "Anything that could not be placed as instructed.",
        },
    },
    "required": ["revised", "applied", "notes"],
    "additionalProperties": False,
}


@dataclass
class Revision:
    text: str
    applied: List[str]
    notes: str
    edits: List[Edit] = field(default_factory=list)


def apply_edits(
    prompt: str, edits: Sequence[Edit], *, effort: str = "high"
) -> Revision:
    """Integrate the given edits. Pass only the edits you have accepted."""
    if not edits:
        return Revision(text=prompt, applied=[], notes="no edits supplied", edits=[])

    blocks = []
    for e in edits:
        blocks.append(
            f"[{e.id}] axis={e.axis} kind={e.kind}\n"
            f"anchor: {e.anchor or '(append near related material)'}\n"
            f"text:\n{e.text}"
        )
    obj, _ = llm.complete_json(
        _APPLY_SYSTEM,
        _APPLY_USER.format(edits="\n\n".join(blocks), prompt=prompt),
        _APPLY_SCHEMA,
        effort=effort,
    )
    return Revision(
        text=obj["revised"],
        applied=obj["applied"],
        notes=obj["notes"],
        edits=list(edits),
    )


# --------------------------------------------------------------------------
# Fidelity — did the revision change the mathematics?
# --------------------------------------------------------------------------

_FIDELITY_SYSTEM = """Compare two versions of a mathematics prompt. The second is meant \
to be the first plus a set of additions, with nothing else changed.

Report every difference that is not a pure addition: altered definitions, changed \
quantifiers or constants, dropped conditions, reworded claims, silently weakened or \
strengthened statements. Report additions only if they assert something mathematical \
that the original did not.

Judge the text as written. Do not speculate about intent."""

_FIDELITY_SCHEMA = {
    "type": "object",
    "properties": {
        "preserved": {
            "type": "boolean",
            "description": "True if the revision is the original plus additions only.",
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "detail": {"type": "string"},
                },
                "required": ["severity", "detail"],
                "additionalProperties": False,
            },
        },
        "new_mathematical_assertions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Mathematical claims present in the revision but not the original.",
        },
    },
    "required": ["preserved", "issues", "new_mathematical_assertions"],
    "additionalProperties": False,
}


def check_fidelity(original: str, revised: str, *, effort: str = "high") -> Dict[str, Any]:
    user = f"<original>\n{original}\n</original>\n\n<revised>\n{revised}\n</revised>"
    obj, _ = llm.complete_json(_FIDELITY_SYSTEM, user, _FIDELITY_SCHEMA, effort=effort)
    return obj


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


@dataclass
class ImprovementRun:
    original: str
    proposal: Proposal
    revision: Revision
    before: Grade
    after: Optional[Grade]
    fidelity: Optional[Dict[str, Any]]

    def deltas(self) -> List[Dict[str, Any]]:
        if self.after is None:
            return []
        before, after = self.before.score_map(), self.after.score_map()
        applied_axes = {e.axis for e in self.revision.edits if e.id in self.revision.applied}
        review_axes = {
            e.axis
            for e in self.revision.edits
            if e.needs_review and e.id in self.revision.applied
        }
        rows = []
        for axis in self.before.axes:
            aid = axis["id"]
            d = after.get(aid, 0) - before.get(aid, 0)
            rows.append(
                {
                    "axis": aid,
                    "name": axis["name"],
                    "before": before.get(aid, 0),
                    "after": after.get(aid, 0),
                    "delta": d,
                    "edited": aid in applied_axes,
                    "provisional": d > 0 and aid in review_axes,
                }
            )
        return rows

    @property
    def provisional_gain(self) -> int:
        """Points that came from edits the author has not yet verified."""
        return sum(r["delta"] for r in self.deltas() if r["provisional"])


def improve(
    prompt: str,
    *,
    axes: Optional[List[Dict[str, str]]] = None,
    accept: str = "safe",
    accept_ids: Optional[Sequence[str]] = None,
    regrade: bool = True,
    check_fidelity_too: bool = True,
    effort: str = "high",
) -> ImprovementRun:
    """Grade, propose, apply, re-grade.

    accept: "safe"  — only edits that assert no mathematics (default)
            "all"   — every proposed edit, review flags carried into the report
            "none"  — propose only, apply nothing
            "ids"   — exactly the ids in accept_ids
    """
    axes = axes or schema.DEFAULT_AXES
    before = grade(prompt, axes=axes, effort=effort)
    proposal = propose(prompt, before, axes=axes, effort=effort)

    if accept == "safe":
        chosen = proposal.safe()
    elif accept == "all":
        chosen = proposal.edits
    elif accept == "none":
        chosen = []
    elif accept == "ids":
        wanted = set(accept_ids or [])
        chosen = [e for e in proposal.edits if e.id in wanted]
    else:
        raise ValueError(f"accept must be safe/all/none/ids, got {accept!r}")

    revision = apply_edits(prompt, chosen, effort=effort)
    after = (
        grade(revision.text, name="revised", axes=axes, effort=effort)
        if regrade and chosen
        else None
    )
    fidelity = (
        check_fidelity(prompt, revision.text, effort=effort)
        if check_fidelity_too and chosen
        else None
    )
    return ImprovementRun(
        original=prompt,
        proposal=proposal,
        revision=revision,
        before=before,
        after=after,
        fidelity=fidelity,
    )


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def format_proposal(proposal: Proposal, axes: Optional[List[Dict[str, str]]] = None) -> str:
    axes = axes or schema.DEFAULT_AXES
    names = {a["id"]: a["name"] for a in axes}
    lines = [f"PROPOSED EDITS ({len(proposal.edits)})", ""]

    for label, group in (
        ("APPLY WITHOUT REVIEW — assert no mathematics", proposal.safe()),
        ("NEEDS AUTHOR REVIEW — asserts mathematics", proposal.needing_review()),
    ):
        if not group:
            continue
        lines.append(label)
        for e in group:
            lines.append(
                f"  [{e.id}] {names.get(e.axis, e.axis)}  ->{e.target_score}  ({e.kind})"
            )
            lines.append(f"       {e.rationale}")
            for line in e.text.strip().splitlines():
                lines.append(f"       | {line}")
            for claim in e.claims_to_verify:
                lines.append(f"       ? verify: {claim}")
            lines.append("")

    if proposal.cannot_fix:
        lines.append("CANNOT FIX WITHOUT YOU")
        for row in proposal.cannot_fix:
            lines.append(f"  {names.get(row['axis'], row['axis'])}: {row['why']}")
    return "\n".join(lines)


def format_improvement(run: ImprovementRun) -> str:
    lines = [format_proposal(run.proposal, run.before.axes), ""]
    lines.append(
        f"APPLIED: {len(run.revision.applied)} of {len(run.proposal.edits)} edits"
    )
    if run.revision.notes:
        lines.append(f"  notes: {run.revision.notes}")

    if run.after is not None:
        lines.append("")
        lines.append(
            f"SCORE: {run.before.total}/{run.before.max_total}"
            f" -> {run.after.total}/{run.after.max_total}"
        )
        for r in run.deltas():
            if r["delta"] == 0 and not r["edited"]:
                continue
            mark = "*" if r["provisional"] else " "
            drift = "  <- unedited axis moved" if r["delta"] != 0 and not r["edited"] else ""
            lines.append(
                f" {mark} {r['name']:<36}{r['before']} -> {r['after']}"
                f"  ({r['delta']:+d}){drift}"
            )
        if run.provisional_gain:
            lines.append("")
            lines.append(
                f"  * {run.provisional_gain} of the gain rests on unverified mathematics."
            )
            lines.append(
                "    Those points are not real until you have checked the claims above."
            )

    if run.fidelity is not None:
        lines.append("")
        lines.append(
            "FIDELITY: " + ("original preserved" if run.fidelity["preserved"] else "CHANGED")
        )
        for issue in run.fidelity["issues"]:
            lines.append(f"  [{issue['severity']}] {issue['detail']}")
        for claim in run.fidelity["new_mathematical_assertions"]:
            lines.append(f"  new claim: {claim}")
    return "\n".join(lines)


def save_run(run: ImprovementRun, path) -> None:
    from pathlib import Path

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "before": run.before.to_dict(),
                "after": run.after.to_dict() if run.after else None,
                "edits": [asdict(e) for e in run.proposal.edits],
                "cannot_fix": run.proposal.cannot_fix,
                "applied": run.revision.applied,
                "notes": run.revision.notes,
                "fidelity": run.fidelity,
                "revised": run.revision.text,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
