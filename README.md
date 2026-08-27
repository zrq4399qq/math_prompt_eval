# math_prompt_eval

A baseline grader for **research-level mathematics prompts** — the prompt a researcher
writes to get an AI system to attack an open problem. It scores the input, not the proof.

The premise: most generated proofs won't solve the problem, so solve-rate is a sparse and
badly-calibrated label. Scoring the prompt on its own structural properties gives a dense
signal that needs no correct answer.

## Where the rubric comes from

Six prompts in [`ShouqiaoW/erdos`](https://github.com/ShouqiaoW/erdos) (problems 390, 486,
536, 788, 1002, 1038) — real researcher-authored attempts at Erdős problems, 7–12 KB each,
with proofs checked and several formalised in Lean. `data/exemplars/` holds copies.

`mpe.schema.DEFAULT_AXES` is a hand-frozen reading of what those prompts share. Twelve
axes on two subscales, each scored 0 / 1 / 2:

| subscale | axes |
|---|---|
| **specification** (16 pts) | formal setup · single precise target · success criteria in all directions · ruled-out shortcuts · named pitfalls · permitted resources · useful reformulations · outcome left open |
| **procedure** (8 pts) | adversarial verification · search policy · stop condition · contamination control |

The two subscales are kept apart because they answer to different settings — a user
prompting a single-shot chat cannot act on multiagent search instructions at all, and
should not be penalised on that subscale for a setting mismatch.

`ruled_out_shortcuts` is the most distinctive axis. Erdős 486 enumerates ~14 insufficient
reductions by name; that is a grading rubric written into the prompt by its author, and it
is the thing most likely to carry real signal.

## Install

> **`python` on this machine is a 3.6 install with no `anthropic` in it.** Use the
> Anaconda interpreter (3.9.21), which is where the SDK was installed. `grade.py` checks
> the version and says so rather than dying with a `SyntaxError`.

```powershell
$py = "$env:USERPROFILE\anaconda3\python.exe"
& $py -m pip install anthropic
setx ANTHROPIC_API_KEY "sk-ant-..."     # then restart the shell
```

## Use

Notebook (`math_prompt_eval.ipynb`) — paste a prompt into section 1 and run. Check the
kernel is the Anaconda 3.9 one, not 3.6.

CLI:

```powershell
$py = "$env:USERPROFILE\anaconda3\python.exe"
& $py grade.py my_prompt.md
& $py grade.py my_prompt.md --json
& $py grade.py my_prompt.md --no-compare --effort medium
Get-Content my_prompt.md -Raw | & $py grade.py -
```

Library:

```python
from mpe import grade, grade_corpus, compare, format_report

g = grade(open("my_prompt.md", encoding="utf-8").read())
print(format_report(g, compare(g, grade_corpus())))
```

## Improving a prompt

`mpe.improve` proposes edits targeting the axes that scored below 2, applies the ones you
accept, re-grades, and checks the revision did not change the mathematics.

```powershell
& $py grade.py my_prompt.md --improve --accept none      # see the edits, apply none
& $py grade.py my_prompt.md --improve                    # apply structural edits
& $py grade.py my_prompt.md --improve --out revised.md   # ... to a path you choose
& $py grade.py my_prompt.md --improve --accept all --save-run run.json
```

When edits are applied the revision is written to **`<input>.revised.md`** next to the
input — so `my_prompt.md` produces `my_prompt.revised.md`. Override with `--out`,
suppress with `--no-save`. The original is never modified. `--save-run` additionally
dumps the whole run (both gradings, every proposed edit, fidelity report, revised text)
as JSON.

```python
from mpe import improve, format_improvement

run = improve(prompt, accept="safe")          # or "all" / "none" / "ids"
print(format_improvement(run))
run.revision.text                             # the revised prompt
```

**The guard.** A rewriter told to raise a rubric score will invent content to raise it,
and on these axes that means inventing mathematics — insufficient reductions that aren't
insufficient, named pitfalls that aren't traps, reformulations that are wrong. That
raises the score while making the prompt worse, which is precisely what the grader exists
to catch. So axes are split by whether raising them requires asserting mathematics:

| class | axes | handling |
|---|---|---|
| structural | target claim · outcome left open · verification demand · search policy · stop condition · contamination control | applied by default |
| mathematical | formal setup · success criteria · ruled-out shortcuts · named pitfalls · permitted resources · reformulations | **held for author review** |

The split is by axis (`improve.AXIS_SAFETY`), not by the model's self-report — the
self-report only ever adds review, never removes it, and unknown axes default to
mathematical. Any score gain traceable to a held edit is reported as **provisional** and
does not count until you have checked the claims. `accept="none"` proposes without
applying; `accept="ids"` takes exactly the edits you name.

Three further checks run automatically: the proposer is told to refuse edits it cannot
ground in the prompt (they come back in `cannot_fix` instead), application is a separate
pass over only the accepted edits so nothing is silently reworded, and `check_fidelity`
diffs the result for altered definitions, dropped conditions, and new assertions.

## Validating it — run this before trusting a score

`mpe.ladder` is the cheap validation: take an exemplar, ablate one axis at a time
(partial = weakened to a generic gesture, full = removed), re-grade, and check the score
moves the way it should.

```python
from mpe import load_exemplars, DEFAULT_AXES
from mpe.ladder import run_ladder, format_ladder

print(format_ladder(run_ladder(load_exemplars()[:1], axes=DEFAULT_AXES[:4])))
```

- **sensitivity** — did full ablation lower the targeted axis at all? An axis that never
  moves is one the grader cannot detect, and it should be dropped from the total.
- **monotonicity** — is full ≤ partial ≤ original? Out-of-order pairs mean noise.
- **attribution** — did the targeted axis drop most? Low attribution with high sensitivity
  means the axes overlap and should be merged.

This needs no new human data: 6 exemplars × 12 axes × 2 severities is 144 labelled pairs
for free. It is also the honest way to find out which axes are real.

## Layout

```
mpe/
  llm.py        one call shape: JSON out, streamed, cached system prompt
  schema.py     DEFAULT_AXES + induce_axes() to re-derive from a corpus
  corpus.py     exemplar loading
  grader.py     grade() / grade_corpus() / compare() / format_report()
  improve.py    propose() / apply_edits() / check_fidelity() / improve()
  degrade.py    single-axis ablation, disk-cached
  ladder.py     run the ladder, compute sensitivity / monotonicity / attribution
grade.py        CLI
tests/
  test_offline.py   plumbing test with a fake model — no key, no network
data/
  exemplars/    the six prompts
  cache/        grades, variants, ladder results (delete to recompute)
```

Model calls go to `claude-opus-5` with adaptive thinking, streamed. Results are cached
under `data/cache/` because every re-run costs tokens.

## Known limits

**Single author.** All six exemplars are by one person. "Distance from the corpus" is
partly "distance from this author's house style" — the grader will reward multiagent
boilerplate and long negative lists because *this* author writes that way, not because
they are known to work. Fix by adding exemplars from other authors (Tao's published
transcripts, the repo's forks) and re-running `induce_axes()`; features surviving the
cross-author intersection are the trustworthy ones.

**No outcome validation.** A high score means "built like the exemplars", not "produces
better mathematics". The estimand that would close the loop is value-added over the bare
problem statement: same problem, your prompt vs. a rewrite, k generations each, scored on
graded partial progress. That is the next thing to build.

**Judge leniency.** Published proof-grading work measures 38–75% leniency rates for LLM
judges. A prompt grader inherits the same tendency to reward authoritative-looking
structure. The ladder is the check on it — an axis that scores 2 on an ablated prompt is
the grader rewarding form over content.

**Grader stability.** Each `grade()` is a single sample. For anything load-bearing, grade
k times and look at the variance before reading a one-point difference as real.
