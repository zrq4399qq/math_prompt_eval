"""Offline plumbing test — no API key, no network.

Substitutes a deterministic fake for the model so that the scoring, comparison,
ablation, and ladder-metric code paths all execute. It proves the wiring works;
it says nothing about grading quality.

    python tests/test_offline.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mpe import compare, format_report, grade, llm, schema  # noqa: E402
from mpe.corpus import Doc  # noqa: E402
from mpe.degrade import make_variant  # noqa: E402
from mpe.grader import Grade, AxisScore  # noqa: E402
from mpe.ladder import LadderResult, format_ladder  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="mpe_test_"))


def fake(system: str, user: str, json_schema: dict) -> dict:
    """Score an axis 2 if its id appears in the prompt text, else 0.

    Also serves the ablation, proposal, apply, and fidelity call shapes, which
    are told apart by their response schemas.
    """
    props = json_schema.get("properties", {})
    body_of = lambda: user.split("<prompt>")[-1].split("</prompt>")[0].strip()

    if "variant" in props:
        return {"variant": body_of(), "removed": "nothing (fake ablator)"}

    if "edits" in props:
        # Propose one edit per axis named in the target list.
        targets = user.split("Raise these axes: ")[-1].splitlines()[0].split(", ")
        return {
            "edits": [
                {
                    "id": f"e{i}",
                    "axis": aid,
                    "target_score": 2,
                    "kind": "add",
                    "anchor": "",
                    "text": aid,  # inserting the id makes the fake grader score it
                    "rationale": "fake",
                    # every other edit self-reports a mathematical claim
                    "introduces_mathematical_claim": i % 2 == 1,
                    "claims_to_verify": ["fake claim"] if i % 2 == 1 else [],
                }
                for i, aid in enumerate(t for t in targets if t)
            ],
            "cannot_fix": [],
        }

    if "revised" in props:
        # Header lines look like: [e3] axis=stop_condition kind=add
        headers = [ln for ln in user.splitlines() if ln.startswith("[e")]
        ids = [ln.split("]")[0][1:] for ln in headers]
        added = [ln.split("axis=")[1].split()[0] for ln in headers]
        # Inserting the axis id is what makes the fake grader score that axis.
        return {"revised": body_of() + " " + " ".join(added), "applied": ids, "notes": ""}

    if "preserved" in props:
        return {"preserved": True, "issues": [], "new_mathematical_assertions": []}

    axis_ids = props["axis_scores"]["items"]["properties"]["axis"]["enum"]
    body = user.split("<prompt>")[-1]
    return {
        "axis_scores": [
            {
                "axis": aid,
                "score": 2 if aid in body else 0,
                "evidence": aid if aid in body else "",
                "note": "fake grader",
            }
            for aid in axis_ids
        ],
        "top_gaps": ["fake gap"],
    }


def check(label, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        raise AssertionError(label)


def main() -> int:
    llm.FAKE_RESPONDER = fake
    ids = [a["id"] for a in schema.DEFAULT_AXES]

    print("rubric")
    rubric = schema.render_rubric(schema.DEFAULT_AXES)
    check("every axis rendered", all(i in rubric for i in ids))
    check("12 axes in the frozen schema", len(schema.DEFAULT_AXES) == 12)
    check("subscales are the declared two",
          {a["subscale"] for a in schema.DEFAULT_AXES} == {schema.SPEC, schema.PROC})
    check("axis ids unique", len(set(ids)) == len(ids))

    print("grading")
    full = grade(" ".join(ids), name="full")
    check("all-present prompt scores max", full.total == full.max_total == 24)
    empty = grade("a prompt mentioning none of the features", name="empty")
    check("no-feature prompt scores zero", empty.total == 0)
    half_ids = ids[:6]
    half = grade(" ".join(half_ids), name="half")
    check("half-present scores half", half.total == 12)
    check("pct computed", abs(half.pct - 50.0) < 1e-9)
    check("subscales sum to total",
          half.subscale(schema.SPEC) + half.subscale(schema.PROC) == half.total)
    check("axis order is canonical", [a.axis for a in half.axis_scores] == ids)

    print("missing-axis handling")
    llm.FAKE_RESPONDER = lambda s, u, sch: {
        "axis_scores": [{"axis": ids[0], "score": 1, "evidence": "", "note": ""}],
        "top_gaps": [],
    }
    try:
        grade("x")
        check("raises when the model omits an axis", False)
    except ValueError:
        check("raises when the model omits an axis", True)
    llm.FAKE_RESPONDER = fake

    print("comparison")
    corpus = [grade(" ".join(ids[:n]), name=f"c{n}") for n in (4, 8, 12)]
    cmp_ = compare(half, corpus)
    check("percentile in range", 0.0 <= cmp_.percentile <= 100.0)
    check("per-axis rows cover the schema", len(cmp_.per_axis) == len(ids))
    check("gaps sorted worst-first",
          all(cmp_.per_axis[i]["gap"] <= cmp_.per_axis[i + 1]["gap"]
              for i in range(len(cmp_.per_axis) - 1)))
    report = format_report(half, cmp_)
    check("report renders", "PER AXIS" in report and "SCORE" in report)

    print("ablation + cache")
    doc = Doc(name="d", text=" ".join(ids), path=Path("d.md"))
    v1 = make_variant(doc, schema.DEFAULT_AXES[0], "full", cache_dir=TMP / "v")
    check("variant carries provenance", v1.axis == ids[0] and v1.severity == "full")
    llm.FAKE_RESPONDER = None  # cache must serve this without any model call
    v2 = make_variant(doc, schema.DEFAULT_AXES[0], "full", cache_dir=TMP / "v")
    check("second call served from cache", v2.text == v1.text)
    llm.FAKE_RESPONDER = fake
    try:
        make_variant(doc, schema.DEFAULT_AXES[0], "sideways", cache_dir=TMP / "v")
        check("rejects unknown severity", False)
    except ValueError:
        check("rejects unknown severity", True)

    print("ladder metrics")
    axes2 = schema.DEFAULT_AXES

    def g(name, scores):
        return Grade(
            name=name,
            axis_scores=[AxisScore(a["id"], scores.get(a["id"], 2), "", "") for a in axes2],
            top_gaps=[],
            axes=axes2,
        )

    rows = [
        # clean detection, correctly attributed
        {"source": "d", "axis": ids[0], "severity": "partial", "baseline_score": 2,
         "variant_score": 1, "target_drop": 1, "collateral": 0,
         "target_is_largest_drop": True, "removed": ""},
        {"source": "d", "axis": ids[0], "severity": "full", "baseline_score": 2,
         "variant_score": 0, "target_drop": 2, "collateral": 0,
         "target_is_largest_drop": True, "removed": ""},
        # invisible axis: ablation changed nothing
        {"source": "d", "axis": ids[1], "severity": "partial", "baseline_score": 2,
         "variant_score": 2, "target_drop": 0, "collateral": 0,
         "target_is_largest_drop": False, "removed": ""},
        {"source": "d", "axis": ids[1], "severity": "full", "baseline_score": 2,
         "variant_score": 2, "target_drop": 0, "collateral": 0,
         "target_is_largest_drop": False, "removed": ""},
    ]
    res = LadderResult(baseline={"d": g("d", {})}, variants={}, rows=rows)
    check("sensitivity 1 of 2 full ablations", abs(res.sensitivity() - 0.5) < 1e-9)
    check("monotonicity holds for both pairs", abs(res.monotonicity() - 1.0) < 1e-9)
    check("attribution 100% among detected", abs(res.attribution() - 1.0) < 1e-9)
    per = res.per_axis()
    check("undetected axis sorts first", per[0]["axis"] == ids[1])
    check("ladder report renders", "sensitivity" in format_ladder(res))

    print("improvement")
    from mpe.improve import (  # noqa: E402
        MATHEMATICAL,
        STRUCTURAL,
        axis_safety,
        format_improvement,
        format_proposal,
        improve,
        propose,
    )

    check("every frozen axis has a safety class",
          all(axis_safety(a["id"]) in (STRUCTURAL, MATHEMATICAL) for a in schema.DEFAULT_AXES))
    check("unknown axes default to needing review", axis_safety("invented_axis") == MATHEMATICAL)

    weak = " ".join(ids[:3])  # 3 axes present, 9 missing
    prop = propose(weak)
    check("proposes only for axes below max", len(prop.edits) == 9)
    check("no edit targets a maxed axis",
          all(e.axis not in set(ids[:3]) for e in prop.edits))
    check("safe and review partition the edits",
          len(prop.safe()) + len(prop.needing_review()) == len(prop.edits))
    check("mathematical axes always need review",
          all(e.needs_review for e in prop.edits if axis_safety(e.axis) == MATHEMATICAL))
    check("self-reported claims always need review",
          all(e.needs_review for e in prop.edits if e.introduces_mathematical_claim))
    check("proposal report renders", "PROPOSED EDITS" in format_proposal(prop))

    run_safe = improve(weak, accept="safe")
    check("safe run applies only no-math edits",
          all(not e.needs_review for e in run_safe.revision.edits))
    check("score did not fall", run_safe.after.total >= run_safe.before.total)
    check("no provisional gain when only safe edits applied", run_safe.provisional_gain == 0)
    check("deltas cover every axis", len(run_safe.deltas()) == len(ids))

    run_all = improve(weak, accept="all")
    check("accept=all applies more than accept=safe",
          len(run_all.revision.edits) > len(run_safe.revision.edits))
    check("accept=all flags provisional gain", run_all.provisional_gain > 0)
    check("improvement report shows the caveat",
          "unverified mathematics" in format_improvement(run_all))
    check("fidelity checked", run_all.fidelity is not None)

    run_none = improve(weak, accept="none")
    check("accept=none leaves the prompt alone", run_none.revision.text == weak)
    check("accept=none skips re-grading", run_none.after is None)

    try:
        improve(weak, accept="sideways")
        check("rejects unknown accept mode", False)
    except ValueError:
        check("rejects unknown accept mode", True)

    full_prompt = " ".join(ids)
    check("nothing to propose on a maxed prompt", len(propose(full_prompt).edits) == 0)

    print("\nall offline checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
