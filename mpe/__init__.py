"""mpe — a baseline grader for research-level mathematics prompts.

Grades the *prompt*, not the proof: how well a researcher's prompt specifies the
work, scored against axes induced from prompts that produced verified results.

    from mpe import grade, grade_corpus, compare, format_report

    g = grade(open("my_prompt.md").read())
    print(format_report(g, compare(g, grade_corpus())))
"""

from .corpus import Doc, load_exemplars, read_prompt
from .grader import (
    AxisScore,
    Comparison,
    Grade,
    compare,
    format_report,
    grade,
    grade_corpus,
)
from .improve import (
    Edit,
    ImprovementRun,
    Proposal,
    Revision,
    apply_edits,
    check_fidelity,
    format_improvement,
    format_proposal,
    improve,
    propose,
    save_run,
)
from .schema import DEFAULT_AXES, PROC, SPEC, induce_axes, render_rubric

__all__ = [
    "AxisScore",
    "Comparison",
    "DEFAULT_AXES",
    "Doc",
    "Edit",
    "Grade",
    "ImprovementRun",
    "PROC",
    "Proposal",
    "Revision",
    "SPEC",
    "apply_edits",
    "check_fidelity",
    "compare",
    "format_improvement",
    "format_proposal",
    "format_report",
    "grade",
    "grade_corpus",
    "improve",
    "induce_axes",
    "load_exemplars",
    "propose",
    "read_prompt",
    "render_rubric",
    "save_run",
]

__version__ = "0.1.0"
