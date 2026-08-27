"""Validating the grader on the degradation ladder.

Three things are measured, and they answer different questions:

  sensitivity   Does ablating an axis lower that axis's score at all?
                An axis that never moves is one the grader cannot detect.
  monotonicity  Does full removal score at or below partial weakening?
                Out-of-order pairs mean the grader is reading noise.
  attribution   Was the targeted axis the one that dropped most?
                Low attribution with high sensitivity means the axes overlap.

Collateral damage is reported too, but read it with care: the ablation is done
by a model, so a changed neighbouring axis may be the ablator's fault rather
than the grader's.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import degrade, grader, schema
from .corpus import CACHE_DIR, Doc


@dataclass
class LadderResult:
    baseline: Dict[str, grader.Grade]
    variants: Dict[str, grader.Grade]
    rows: List[Dict[str, Any]] = field(default_factory=list)

    # -- aggregate metrics -------------------------------------------------
    def sensitivity(self) -> float:
        full = [r for r in self.rows if r["severity"] == "full"]
        if not full:
            return float("nan")
        return sum(1 for r in full if r["target_drop"] > 0) / len(full)

    def monotonicity(self) -> float:
        pairs = {}
        for r in self.rows:
            pairs.setdefault((r["source"], r["axis"]), {})[r["severity"]] = r
        ok = tot = 0
        for _, sev in pairs.items():
            if "partial" in sev and "full" in sev:
                tot += 1
                base = sev["partial"]["baseline_score"]
                if base >= sev["partial"]["variant_score"] >= sev["full"]["variant_score"]:
                    ok += 1
        return ok / tot if tot else float("nan")

    def attribution(self) -> float:
        full = [r for r in self.rows if r["severity"] == "full" and r["target_drop"] > 0]
        if not full:
            return float("nan")
        return sum(1 for r in full if r["target_is_largest_drop"]) / len(full)

    def per_axis(self) -> List[Dict[str, Any]]:
        out = []
        by_axis: Dict[str, List[Dict[str, Any]]] = {}
        for r in self.rows:
            if r["severity"] == "full":
                by_axis.setdefault(r["axis"], []).append(r)
        for axis, rows in by_axis.items():
            drops = [r["target_drop"] for r in rows]
            out.append(
                {
                    "axis": axis,
                    "n": len(rows),
                    "detected": sum(1 for d in drops if d > 0),
                    "mean_drop": round(statistics.mean(drops), 2) if drops else 0.0,
                    "mean_collateral": round(
                        statistics.mean([r["collateral"] for r in rows]), 2
                    ),
                }
            )
        out.sort(key=lambda r: (r["detected"] / r["n"] if r["n"] else 0, r["mean_drop"]))
        return out


def run_ladder(
    docs: List[Doc],
    axes: Optional[List[Dict[str, str]]] = None,
    severities=degrade.SEVERITIES,
    *,
    grade_effort: str = "high",
    ablate_effort: str = "medium",
    cache_path: Optional[Path] = None,
    refresh: bool = False,
) -> LadderResult:
    """Ablate, grade, and score the grader. Results are cached — this is the
    expensive call: len(docs) * (1 + len(axes) * len(severities)) gradings."""
    axes = axes or schema.DEFAULT_AXES
    cache_path = Path(cache_path or (CACHE_DIR / "ladder.json"))

    if cache_path.exists() and not refresh:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
        return _from_cache(raw, axes)

    baseline: Dict[str, grader.Grade] = {}
    for doc in docs:
        baseline[doc.name] = grader.grade(
            doc.text, name=doc.name, axes=axes, effort=grade_effort
        )

    variants: Dict[str, grader.Grade] = {}
    rows: List[Dict[str, Any]] = []
    for doc in docs:
        base_scores = baseline[doc.name].score_map()
        for axis in axes:
            for severity in severities:
                var = degrade.make_variant(doc, axis, severity, effort=ablate_effort)
                g = grader.grade(var.text, name=var.name, axes=axes, effort=grade_effort)
                variants[var.name] = g
                var_scores = g.score_map()

                drops = {
                    a["id"]: base_scores.get(a["id"], 0) - var_scores.get(a["id"], 0)
                    for a in axes
                }
                target_drop = drops[axis["id"]]
                max_drop = max(drops.values())
                rows.append(
                    {
                        "source": doc.name,
                        "axis": axis["id"],
                        "severity": severity,
                        "baseline_score": base_scores.get(axis["id"], 0),
                        "variant_score": var_scores.get(axis["id"], 0),
                        "target_drop": target_drop,
                        "collateral": sum(
                            1 for k, v in drops.items() if k != axis["id"] and v != 0
                        ),
                        "target_is_largest_drop": target_drop > 0 and target_drop >= max_drop,
                        "removed": var.removed,
                    }
                )

    result = LadderResult(baseline=baseline, variants=variants, rows=rows)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(_to_cache(result), indent=2), encoding="utf-8")
    return result


def _to_cache(result: LadderResult) -> Dict[str, Any]:
    return {
        "baseline": {k: g.to_dict() for k, g in result.baseline.items()},
        "variants": {k: g.to_dict() for k, g in result.variants.items()},
        "rows": result.rows,
    }


def _from_cache(raw: Dict[str, Any], axes: List[Dict[str, str]]) -> LadderResult:
    def rebuild(d):
        return grader.Grade(
            name=d["name"],
            axis_scores=[grader.AxisScore(**a) for a in d["axis_scores"]],
            top_gaps=d["top_gaps"],
            axes=axes,
        )

    return LadderResult(
        baseline={k: rebuild(v) for k, v in raw["baseline"].items()},
        variants={k: rebuild(v) for k, v in raw["variants"].items()},
        rows=raw["rows"],
    )


def format_ladder(result: LadderResult, axes: Optional[List[Dict[str, str]]] = None) -> str:
    axes = axes or schema.DEFAULT_AXES
    names = {a["id"]: a["name"] for a in axes}
    lines = [
        "DEGRADATION LADDER",
        f"  sensitivity   {result.sensitivity():.0%}   (full ablation lowered the targeted axis)",
        f"  monotonicity  {result.monotonicity():.0%}   (full <= partial <= original)",
        f"  attribution   {result.attribution():.0%}   (targeted axis dropped most)",
        "",
        f"{'axis':<24}{'detected':>10}{'mean drop':>12}{'collateral':>12}",
    ]
    for r in result.per_axis():
        lines.append(
            f"{names.get(r['axis'], r['axis']):<24}"
            f"{r['detected']}/{r['n']:>8}"
            f"{r['mean_drop']:>12}"
            f"{r['mean_collateral']:>12}"
        )
    lines.append("")
    lines.append("Axes at the top of this table carry the least signal: ablating them")
    lines.append("did not change the score, so they are not yet worth trusting.")
    return "\n".join(lines)
