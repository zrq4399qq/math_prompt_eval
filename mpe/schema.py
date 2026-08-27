"""The feature schema: the axes a research-math prompt is graded on.

DEFAULT_AXES was hand-frozen from a reading of the six ShouqiaoW/erdos
exemplars. `induce_axes()` re-derives a schema from whatever corpus is on
disk, so the frozen list can be checked against a larger or different corpus
later. Everything downstream reads whichever schema is passed to it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import llm

SPEC = "specification"  # properties of the mathematical problem statement
PROC = "procedure"  # properties of the requested search/verification process

DEFAULT_AXES: List[Dict[str, str]] = [
    {
        "id": "formal_setup",
        "name": "Formal setup",
        "subscale": SPEC,
        "question": "Is every object, notation, and quantifier defined before it is used?",
        "anchor_0": "Objects referred to informally or assumed known; notation undefined.",
        "anchor_1": "Main objects defined, but some notation or quantifier is left implicit.",
        "anchor_2": "Self-contained: a reader with no outside context can parse every symbol.",
    },
    {
        "id": "target_claim",
        "name": "Single precise target",
        "subscale": SPEC,
        "question": "Is there exactly one precisely stated thing being asked for?",
        "anchor_0": "Vague direction ('investigate X', 'say something about Y').",
        "anchor_1": "A clear question, but with ambiguity about scope or which case is meant.",
        "anchor_2": "One unambiguous claim or question, stated formally.",
    },
    {
        "id": "success_criteria",
        "name": "Success criteria in all directions",
        "subscale": SPEC,
        "question": "Is it spelled out what a complete answer looks like, for every possible outcome?",
        "anchor_0": "No statement of what would count as done.",
        "anchor_1": "Success stated for the expected outcome only.",
        "anchor_2": "Each possible resolution (e.g. affirmative and negative) written out formally.",
    },
    {
        "id": "ruled_out_shortcuts",
        "name": "Ruled-out shortcuts",
        "subscale": SPEC,
        "question": "Does it enumerate specific partial results that would NOT count as a solution?",
        "anchor_0": "No exclusions given.",
        "anchor_1": "Generic exclusions ('no partial results', 'be rigorous').",
        "anchor_2": "A specific list of insufficient reductions, special cases, or weaker statements.",
    },
    {
        "id": "pitfall_warnings",
        "name": "Named pitfalls",
        "subscale": SPEC,
        "question": "Are concrete failure modes named that the argument must avoid?",
        "anchor_0": "None.",
        "anchor_1": "General cautions without naming the specific error.",
        "anchor_2": "Named traps (illegal limit interchange, false independence, edge cases, ...).",
    },
    {
        "id": "resource_bounds",
        "name": "Permitted resources",
        "subscale": SPEC,
        "question": "Is it stated which prior results, literature, or tools may be used and how?",
        "anchor_0": "Silent on what may be assumed.",
        "anchor_1": "Loose gesture at allowed background.",
        "anchor_2": "Named permitted areas/theorems plus conditions on their use (hypotheses, uniformity).",
    },
    {
        "id": "reformulations",
        "name": "Useful reformulations",
        "subscale": SPEC,
        "question": "Does it supply equivalent framings, constructions, or scaffolding to work from?",
        "anchor_0": "Bare statement, no scaffolding.",
        "anchor_1": "One aside or hint.",
        "anchor_2": "Explicit reformulations or intermediate objects, with their limits noted.",
    },
    {
        "id": "no_answer_leak",
        "name": "Outcome left open",
        "subscale": SPEC,
        "question": "Does the prompt avoid presupposing or leaking the answer?",
        "anchor_0": "Assumes or states the answer, or is phrased to elicit agreement.",
        "anchor_1": "Neutral but tilted (asks to 'prove' one direction only).",
        "anchor_2": "Explicitly keeps every outcome open and forbids assuming one.",
    },
    {
        "id": "verification_demand",
        "name": "Adversarial verification",
        "subscale": PROC,
        "question": "Is the answer required to survive an explicit audit before being returned?",
        "anchor_0": "No verification requested.",
        "anchor_1": "'Check your work' with no method.",
        "anchor_2": "A specified adversarial check with a concrete checklist of what to attack.",
    },
    {
        "id": "search_policy",
        "name": "Search policy",
        "subscale": PROC,
        "question": "Is there guidance on how to explore (breadth, diversity, independence, reallocation)?",
        "anchor_0": "None.",
        "anchor_1": "Vague ('try several approaches').",
        "anchor_2": "Explicit portfolio management: diversity, independence, when to abandon or reopen.",
    },
    {
        "id": "stop_condition",
        "name": "Stop condition",
        "subscale": PROC,
        "question": "Is it stated when to return, and what must not be returned?",
        "anchor_0": "Nothing about stopping.",
        "anchor_1": "'Return when done.'",
        "anchor_2": "Explicit return condition plus named non-answers to refuse to return.",
    },
    {
        "id": "contamination_control",
        "name": "Contamination control",
        "subscale": PROC,
        "question": "Are lookup and literature search bounded so the answer is not simply retrieved?",
        "anchor_0": "Silent.",
        "anchor_1": "Mentions sources without limiting them.",
        "anchor_2": "Explicit boundary between allowed background lookup and forbidden answer lookup.",
    },
]

MAX_SCORE_PER_AXIS = 2


def axes_by_subscale(axes: List[Dict[str, str]]) -> Dict[str, List[Dict[str, str]]]:
    out: Dict[str, List[Dict[str, str]]] = {}
    for axis in axes:
        out.setdefault(axis["subscale"], []).append(axis)
    return out


def render_rubric(axes: List[Dict[str, str]]) -> str:
    lines = []
    for axis in axes:
        lines.append(f"### {axis['id']} — {axis['name']}  [{axis['subscale']}]")
        lines.append(axis["question"])
        lines.append(f"  0 = {axis['anchor_0']}")
        lines.append(f"  1 = {axis['anchor_1']}")
        lines.append(f"  2 = {axis['anchor_2']}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Re-deriving the schema from a corpus
# --------------------------------------------------------------------------

_INDUCE_SYSTEM = """You are analysing a corpus of prompts that research mathematicians \
wrote to get an AI system to attack open problems. Some of these attempts produced \
proofs that held up.

Your task is to induce a grading rubric: the structural features these prompts share, \
stated as scoreable axes. You are looking for what the author *did*, not for what the \
mathematics is about. Two prompts on unrelated problems should score similarly if they \
are built the same way.

Rules:
- Report only features that appear in at least half the corpus. A feature present in one \
prompt is that author's habit, not a rubric axis.
- Each axis must be checkable by pointing at a span of text.
- Separate axes about the mathematical problem statement (subscale "specification") from \
axes about the requested search or verification process (subscale "procedure").
- Anchors must describe observable text, not quality judgements."""

_INDUCE_SCHEMA = {
    "type": "object",
    "properties": {
        "axes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "subscale": {"type": "string", "enum": [SPEC, PROC]},
                    "question": {"type": "string"},
                    "anchor_0": {"type": "string"},
                    "anchor_1": {"type": "string"},
                    "anchor_2": {"type": "string"},
                    "present_in": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Corpus document names exhibiting this feature.",
                    },
                },
                "required": [
                    "id",
                    "name",
                    "subscale",
                    "question",
                    "anchor_0",
                    "anchor_1",
                    "anchor_2",
                    "present_in",
                ],
                "additionalProperties": False,
            },
        },
        "rejected": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Candidate features seen in too few documents to keep.",
        },
    },
    "required": ["axes", "rejected"],
    "additionalProperties": False,
}


def induce_axes(corpus, *, effort: str = "high") -> Dict[str, Any]:
    """Derive a candidate schema from a corpus. Returns the raw model object."""
    parts = []
    for doc in corpus:
        parts.append(f"<prompt name=\"{doc.name}\">\n{doc.text}\n</prompt>")
    user = (
        f"Here are {len(corpus)} prompts. Induce the rubric.\n\n" + "\n\n".join(parts)
    )
    obj, _ = llm.complete_json(_INDUCE_SYSTEM, user, _INDUCE_SCHEMA, effort=effort)
    return obj


def save_axes(axes: List[Dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(axes, indent=2), encoding="utf-8")


def load_axes(path: Optional[Path]) -> List[Dict[str, str]]:
    if path is None or not Path(path).exists():
        return DEFAULT_AXES
    return json.loads(Path(path).read_text(encoding="utf-8"))
