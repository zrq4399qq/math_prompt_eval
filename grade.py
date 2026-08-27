#!/usr/bin/env python
"""Grade a research-math prompt from the command line, and optionally improve it.

    python grade.py my_prompt.md
    python grade.py my_prompt.md --json
    python grade.py my_prompt.md --no-compare       # skip the corpus comparison
    Get-Content my_prompt.md -Raw | python grade.py -

    python grade.py my_prompt.md --improve --out revised.md
    python grade.py my_prompt.md --improve --accept none    # see the edits, apply none
    python grade.py my_prompt.md --improve --accept all --save-run run.json

`python` here must be 3.8+; on this machine that is the Anaconda interpreter.
"""

import sys

# Checked before importing mpe, which uses 3.7+ syntax and would otherwise fail
# with a bare SyntaxError. On this machine `python` is a 3.6 install; the
# interpreter with anthropic in it is the Anaconda one.
if sys.version_info < (3, 8):
    sys.stderr.write(
        "grade.py needs Python 3.8+ (the anthropic SDK does too); "
        "this is %d.%d.\n\n" % sys.version_info[:2]
    )
    sys.stderr.write(
        'Try:  & "$env:USERPROFILE\\anaconda3\\python.exe" grade.py <prompt.md>\n'
        "  or:  py -3 grade.py <prompt.md>\n"
    )
    raise SystemExit(2)

import argparse
import json
from pathlib import Path

from mpe import (
    compare,
    format_improvement,
    format_report,
    grade,
    grade_corpus,
    improve,
    save_run,
)
from mpe.llm import MissingAPIKey


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("prompt", help="path to a prompt file, or - for stdin")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    parser.add_argument(
        "--no-compare", action="store_true", help="skip grading/loading the exemplar corpus"
    )
    parser.add_argument(
        "--effort",
        default="high",
        choices=["low", "medium", "high", "xhigh", "max"],
        help="model effort (default: high)",
    )
    parser.add_argument(
        "--improve",
        action="store_true",
        help="propose rubric-targeted edits, apply them, and re-grade",
    )
    parser.add_argument(
        "--accept",
        default="safe",
        choices=["safe", "all", "none"],
        help="which proposed edits to apply: safe = those asserting no mathematics "
        "(default), all = every edit, none = propose only",
    )
    parser.add_argument(
        "--out",
        help="where to write the revised prompt (default: <input>.revised.md)",
    )
    parser.add_argument(
        "--no-save", action="store_true", help="do not write the revised prompt to disk"
    )
    parser.add_argument("--save-run", help="write the full improvement run as JSON")
    args = parser.parse_args()

    if args.prompt == "-":
        text, name = sys.stdin.read(), "stdin"
    else:
        path = Path(args.prompt)
        if not path.exists():
            print(f"no such file: {path}", file=sys.stderr)
            return 2
        text, name = path.read_text(encoding="utf-8"), path.stem

    if not text.strip():
        print("prompt is empty", file=sys.stderr)
        return 2

    if args.improve:
        try:
            run = improve(text, accept=args.accept, effort=args.effort)
        except MissingAPIKey as exc:
            print(exc, file=sys.stderr)
            return 1
        print(format_improvement(run))

        if args.save_run:
            save_run(run, args.save_run)
            print(f"\nfull run written to {args.save_run}")

        # The revised text exists only in memory otherwise, so save by default.
        if run.revision.applied and not args.no_save:
            if args.out:
                out = Path(args.out)
            elif args.prompt == "-":
                print(
                    "\nreading from stdin, so pass --out PATH to keep the revision",
                    file=sys.stderr,
                )
                return 0
            else:
                src = Path(args.prompt)
                out = src.with_name(src.stem + ".revised" + src.suffix)
            out.write_text(run.revision.text, encoding="utf-8")
            print(f"\nrevised prompt written to {out}")
        elif not run.revision.applied:
            print("\nno edits applied, so nothing was written")
        return 0

    try:
        result = grade(text, name=name, effort=args.effort)
        comparison = None if args.no_compare else compare(result, grade_corpus(effort=args.effort))
    except MissingAPIKey as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.json:
        payload = result.to_dict()
        if comparison is not None:
            payload["comparison"] = {
                "percentile": comparison.percentile,
                "corpus_median": comparison.corpus_median,
                "per_axis": comparison.per_axis,
            }
        print(json.dumps(payload, indent=2))
    else:
        print(format_report(result, comparison))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
