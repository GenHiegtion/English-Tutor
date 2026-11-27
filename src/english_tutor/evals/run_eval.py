"""Teacher evals with the real model: run cases, check rules, hand off to a judge, report.

Usage::

    uv run python -m english_tutor.evals.run_eval run [--filter TEXT] [--out DIR] [--concurrency N]
    uv run python -m english_tutor.evals.run_eval report RUN_DIR [--baseline RUN_DIR]

``run`` sends every case through ``TeacherService`` with the configured teacher model
(``ENGLISH_TUTOR_TEACHER_MODEL``) and no fallback, so results belong to that model alone. It
writes to the run directory (default ``eval_runs/<timestamp>``):

- ``results.jsonl``: one ``CaseResult`` per case (reply, corrections, flags, cost, rule checks);
- ``judge_prompt.md``: instructions and cases for the LLM judge, which writes
  ``judge_scores.jsonl`` next to it;
- ``report.md``: metrics, failed cases and, with ``--baseline``, regressions.

``report`` rebuilds ``report.md`` after the judge has written its scores.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from english_tutor.adapters.memory import (
    MemoryHistoryStore,
    MemoryLearnerStore,
    MemoryPersonaStore,
    load_seed_scenarios,
)
from english_tutor.evals.cases import EvalCase, load_cases
from english_tutor.evals.judges import (
    CaseResult,
    JudgeScore,
    MistakeOut,
    SegmentOut,
    build_judge_prompt,
    check_case,
    load_judge_scores,
)
from english_tutor.models import LearnerProfile, StoredMessage, TurnRecord
from english_tutor.providers.base import LLMProvider, Message, TextPart
from english_tutor.teacher.context import LearnerInput, TurnContext
from english_tutor.teacher.events import (
    MistakeNoted,
    OutputSegment,
    TerminalAction,
    TurnEnd,
    TurnFailed,
)
from english_tutor.teacher.runner import LEARNER_PREFIX, TeacherService

RUNS_DIR = Path("eval_runs")
RESULTS = "results.jsonl"
JUDGE_PROMPT = "judge_prompt.md"
JUDGE_SCORES = "judge_scores.jsonl"
REPORT = "report.md"
JUDGE_PASS = 4  # a judged item passes at 4/5 or more (PLAN threshold)


# ------------------------------------------------------------------------------- run
async def run_case(
    service: TeacherService, learners: MemoryLearnerStore, case: EvalCase
) -> CaseResult:
    """One case in a fresh session: open, scenario opener, seeded history, then the case turn."""
    scenarios = {s.id: s for s in load_seed_scenarios()}
    scenario = scenarios[case.scenario] if case.scenario else None
    user_id = f"eval-{case.id}"
    session_id = f"{user_id}-{uuid.uuid4().hex[:6]}"
    learners.put(
        LearnerProfile(
            user_id=user_id, cefr=case.cefr, age_band=case.age_band, interests=case.interests
        )
    )
    snapshot = await service.open_session(
        session_id=session_id,
        user_id=user_id,
        channel=case.channel,
        mode=case.mode,
        persona_id=case.persona,
        scenario=scenario,
    )
    result = CaseResult(
        id=case.id,
        mode=case.mode,
        cefr=case.cefr,
        persona=case.persona,
        learner=case.learner,
        model=service.primary.model_for("teacher"),
        prompt_version=snapshot.prompt_version,
        reviewed=case.reviewed_by != "PLACEHOLDER",
    )

    def ctx(turn_id: str, text: str | None) -> TurnContext:
        return TurnContext(
            session_id=session_id,
            user_id=user_id,
            turn_id=turn_id,
            channel=case.channel,
            mode=case.mode,
            persona_id=case.persona,
            scenario=scenario,
            learner_input=LearnerInput(text=text, source="typed") if text is not None else None,
            received_at=0.0,
        )

    if scenario is not None:
        async for _ in service.run_turn(ctx("opener", None)):
            pass
    for i, turn in enumerate(case.history):
        user = Message(
            role="user", content=[TextPart(text=f"{LEARNER_PREFIX[case.channel]} {turn.learner}")]
        )
        assistant = Message(role="assistant", content=[TextPart(text=turn.teacher)])
        await service.history.append_turn(
            session_id,
            TurnRecord(
                turn_id=f"history-{i}",
                messages=[
                    StoredMessage(turn_id=f"history-{i}", message=user),
                    StoredMessage(
                        turn_id=f"history-{i}", message=assistant, persona_id=case.persona
                    ),
                ],
            ),
        )

    turn_id = "case"
    async for event in service.run_turn(ctx(turn_id, case.learner)):
        if isinstance(event, OutputSegment):
            service.mark_delivered(turn_id, event.seq)
            result.segments.append(SegmentOut(lang=event.lang, text=event.text))
        elif isinstance(event, MistakeNoted):
            result.mistakes.append(
                MistakeOut(
                    type=event.type,
                    original=event.original,
                    corrected=event.corrected,
                    explanation_vi=event.explanation_vi,
                )
            )
        elif isinstance(event, TerminalAction):
            result.terminal, result.terminal_args = event.tool, event.args
        elif isinstance(event, TurnEnd):
            result.flags, result.ttft_ms = event.guard_flags, event.ttft_ms
        elif isinstance(event, TurnFailed):
            result.failed = event.reason
    history = service.history
    record = history.turns.get(turn_id) if isinstance(history, MemoryHistoryStore) else None
    if record is not None:
        result.cost_usd = sum(row.cost_usd or 0.0 for row in record.usage)
    result.checks = check_case(case, result)
    return result


async def run_cases(
    cases: list[EvalCase], primary: LLMProvider, *, concurrency: int = 4
) -> list[CaseResult]:
    """Run cases concurrently; every case gets its own history store (turn ids repeat)."""
    semaphore = asyncio.Semaphore(concurrency)
    personas = MemoryPersonaStore.from_seed()

    async def one(case: EvalCase) -> CaseResult:
        learners = MemoryLearnerStore()
        service = TeacherService(
            primary=primary, history=MemoryHistoryStore(), personas=personas, learners=learners
        )
        async with semaphore:
            return await run_case(service, learners, case)

    return list(await asyncio.gather(*(one(c) for c in cases)))


def write_run(run_dir: Path, cases: list[EvalCase], results: list[CaseResult]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / RESULTS).open("w", encoding="utf-8") as fh:
        for result in results:
            fh.write(result.model_dump_json() + "\n")
    by_id = {c.id: c for c in cases}
    pairs = [(by_id[r.id], r) for r in results]
    scores_path = (run_dir / JUDGE_SCORES).resolve()
    (run_dir / JUDGE_PROMPT).write_text(build_judge_prompt(pairs, scores_path), encoding="utf-8")


def load_run(run_dir: Path) -> tuple[list[CaseResult], dict[str, JudgeScore]]:
    """Results with rule checks re-applied from the current cases, plus judge scores if any.

    Re-checking keeps an old baseline comparable after a rule or an ``expect`` block changes.
    """
    cases = {c.id: c for c in load_cases()}
    lines = (run_dir / RESULTS).read_text(encoding="utf-8").splitlines()
    results = [CaseResult.model_validate_json(line) for line in lines if line.strip()]
    for result in results:
        if result.id in cases:
            result.checks = check_case(cases[result.id], result)
    scores_path = run_dir / JUDGE_SCORES
    scores: dict[str, JudgeScore] = {}
    if scores_path.exists():
        scores = load_judge_scores(scores_path, cases)
    return results, scores


# ---------------------------------------------------------------------------- report
def _pct(part: int, whole: int) -> str:
    return f"{part}/{whole} ({100 * part / whole:.0f}%)" if whole else "n/a"


def _rate(results: list[CaseResult], prefix: str) -> tuple[int, int]:
    outcomes = [o for r in results for name, o in r.checks.items() if name.startswith(prefix)]
    return sum(o.passed for o in outcomes), len(outcomes)


def regressions(
    current: list[CaseResult],
    scores: dict[str, JudgeScore],
    baseline: list[CaseResult],
    baseline_scores: dict[str, JudgeScore],
) -> list[str]:
    """Cases that passed in the baseline and fail now, or lost a judge point or more."""
    before = {r.id: r for r in baseline}
    lines = []
    for result in current:
        old = before.get(result.id)
        if old is None:
            continue
        if old.passed and not result.passed:
            failed = [n for n, o in result.checks.items() if not o.passed]
            lines.append(f"- `{result.id}`: rule checks now fail ({', '.join(failed)})")
        new_s, old_s = scores.get(result.id), baseline_scores.get(result.id)
        if new_s and old_s:
            drops = [
                f"{item} {old_s.scores[item]}→{value}"
                for item, value in new_s.scores.items()
                if item in old_s.scores and old_s.scores[item] - value >= 1
            ]
            if drops:
                lines.append(f"- `{result.id}`: judge {', '.join(drops)}")
    return lines


def render_report(
    name: str,
    results: list[CaseResult],
    scores: dict[str, JudgeScore],
    regression_lines: list[str] | None = None,
) -> str:
    total = len(results)
    passed = sum(r.passed for r in results)
    ttfts = [r.ttft_ms for r in results if r.ttft_ms is not None]
    unreviewed = sum(not r.reviewed for r in results)
    models = sorted({r.model for r in results})
    versions = sorted({r.prompt_version for r in results})
    out = [
        f"# Teacher eval: {name}",
        "",
        f"- Model: {', '.join(models)}",
        f"- Prompts: {', '.join(versions)}",
        f"- Cases: {total} ({unreviewed} not reviewed yet)",
        f"- Cost: ${sum(r.cost_usd for r in results):.4f}",
    ]
    if ttfts:
        p90 = statistics.quantiles(ttfts, n=10)[-1] if len(ttfts) >= 2 else ttfts[0]
        out.append(f"- TTFT: median {statistics.median(ttfts):.0f} ms, p90 {p90:.0f} ms")
    out += ["", "## Rule checks", "", f"**Cases passing every check: {_pct(passed, total)}**", ""]
    for mode in sorted({r.mode for r in results}):
        subset = [r for r in results if r.mode == mode]
        out.append(f"- {mode}: {_pct(sum(r.passed for r in subset), len(subset))}")
    turns = [r for r in results if not r.failed]

    def flag_free(flag: str) -> int:
        return sum(flag not in r.flags for r in turns)

    metrics = [
        ("Clean output (tags, format, tools, pronouns)", _rate(results, "clean_output"), "≥ 98%"),
        ("Pronouns correct", (flag_free("address_mismatch"), len(turns)), "≥ 99%"),
        ("ask_question is what the learner saw", _rate(results, "question_said"), "≥ 95%"),
        ("Required corrections made", _rate(results, "must_correct"), "≥ 90%"),
        ("No correction of correct sentences", _rate(results, "no_correction"), "≥ 95%"),
        ("English within CEFR level", (flag_free("cefr_overshoot"), len(turns)), "≥ 95%"),
        ("Length within limit (no too_long)", (flag_free("too_long"), len(turns)), ""),
    ]
    out += ["", "| Metric | Result | Target |", "| --- | --- | --- |"]
    out += [f"| {label} | {_pct(*rate)} | {target} |" for label, rate, target in metrics]
    if scores:
        out += ["", "## LLM judge", "", "| Item | Mean | ≥ 4 | n |", "| --- | --- | --- | --- |"]
        items: dict[str, list[int]] = {}
        for score in scores.values():
            for item, value in score.scores.items():
                items.setdefault(item, []).append(value)
        every = [v for vs in items.values() for v in vs]
        for item, values in [*sorted(items.items()), ("**all**", every)]:
            good = _pct(sum(v >= JUDGE_PASS for v in values), len(values))
            out.append(f"| {item} | {statistics.mean(values):.2f} | {good} | {len(values)} |")
    else:
        out += ["", "## LLM judge", "", f"No `{JUDGE_SCORES}` yet: see `{JUDGE_PROMPT}`."]
    failing = [r for r in results if not r.passed]
    if failing:
        out += ["", "## Failed rule checks", ""]
        for r in failing:
            details = "; ".join(f"{n}: {o.detail}" for n, o in r.checks.items() if not o.passed)
            out.append(f"- `{r.id}`: {details}")
    weak = [s for s in scores.values() if any(v < JUDGE_PASS for v in s.scores.values())]
    if weak:
        out += ["", "## Judge findings (items below 4)", ""]
        for s in weak:
            low = ", ".join(f"{k} {v}" for k, v in s.scores.items() if v < JUDGE_PASS)
            out.append(f"- `{s.id}` ({low}): {s.issues}")
    if regression_lines is not None:
        out += ["", "## Regressions vs baseline", ""]
        out += regression_lines or ["None."]
    out += ["", "## Replies", ""]
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        out.append(f"### {r.id} ({mark})")
        out.append(f"Learner ({r.cefr}): {r.learner}")
        out.append("")
        out += [f"- [{s.lang}] {s.text}" for s in r.segments] or ["- (no reply)"]
        out += [f"- {{mistake {m.type}: {m.original} -> {m.corrected}}}" for m in r.mistakes]
        out.append(f"- {{{r.terminal}}} flags: {', '.join(r.flags) or 'none'}")
        out.append("")
    return "\n".join(out)


def write_report(run_dir: Path, baseline_dir: Path | None) -> str:
    results, scores = load_run(run_dir)
    lines = None
    if baseline_dir is not None:
        base_results, base_scores = load_run(baseline_dir)
        lines = regressions(results, scores, base_results, base_scores)
    report = render_report(run_dir.name, results, scores, lines)
    (run_dir / REPORT).write_text(report, encoding="utf-8")
    return report


# ------------------------------------------------------------------------------- CLI
def _summary(report: str) -> str:
    """The report up to the first per-case section, for the terminal."""
    return report.split("\n## Failed rule checks")[0].split("\n## Replies")[0]


async def _run(args: argparse.Namespace) -> int:
    from english_tutor.providers.openrouter_provider import OpenRouterProvider

    load_dotenv()
    cases = [c for c in load_cases() if args.filter in c.id]
    if not cases:
        print(f"no case id contains {args.filter!r}", file=sys.stderr)
        return 2
    run_dir = args.out or RUNS_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"running {len(cases)} cases -> {run_dir}", file=sys.stderr)
    results = await run_cases(cases, OpenRouterProvider(), concurrency=args.concurrency)
    write_run(run_dir, cases, results)
    print(_summary(write_report(run_dir, args.baseline)))
    print(f"\nJudge next: {run_dir / JUDGE_PROMPT} -> {run_dir / JUDGE_SCORES}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="run_eval", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the cases against the real teacher model")
    run.add_argument("--filter", default="", help="only cases whose id contains this text")
    run.add_argument("--out", type=Path, default=None, help="run directory")
    run.add_argument("--concurrency", type=int, default=4)
    run.add_argument("--baseline", type=Path, default=None, help="earlier run to compare with")
    report = sub.add_parser("report", help="rebuild report.md (after the judge scored)")
    report.add_argument("run_dir", type=Path)
    report.add_argument("--baseline", type=Path, default=None)
    args = parser.parse_args()
    if args.command == "run":
        sys.exit(asyncio.run(_run(args)))
    print(_summary(write_report(args.run_dir, args.baseline)))


if __name__ == "__main__":
    main()
