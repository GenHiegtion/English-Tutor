import json

import pytest

from english_tutor.evals.cases import EvalCase, find_case, load_cases, with_personas
from english_tutor.evals.judges import (
    CaseResult,
    CheckOutcome,
    JudgeScore,
    MistakeOut,
    SegmentOut,
    build_judge_prompt,
    check_case,
    load_judge_scores,
)
from english_tutor.evals.run_eval import (
    JUDGE_PROMPT,
    JUDGE_SCORES,
    RESULTS,
    load_run,
    regressions,
    render_report,
    run_cases,
    write_run,
)
from english_tutor.testing import FakeProvider, reply

MISTAKE = (
    "record_mistake",
    {"type": "grammar", "original": "go", "corrected": "went", "explanation_vi": "quá khứ"},
)
ASK = ("ask_question", {"question": "What did you see?", "expected_structure": "I saw ..."})


def case(**kw):
    base = {"id": "c1", "mode": "free_chat", "learner": "Yesterday I go to the cinema"}
    return EvalCase.model_validate({**base, **kw})


def result(**kw):
    base = {"id": "c1", "mode": "free_chat", "cefr": "A2", "persona": "lan", "learner": "x"}
    return CaseResult.model_validate({**base, **kw})


# ---- cases ----------------------------------------------------------------------------
def test_shipped_cases_load_and_cover_both_modes():
    cases = load_cases()
    assert len(cases) >= 40
    assert {c.mode for c in cases} == {"scenario", "free_chat"}
    assert all(c.judge for c in cases)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"mode": "scenario"}, "scenario is required"),
        ({"scenario": "cafe-order"}, "scenario is required"),
        ({"judge": ["vibes"]}, "unknown judge items"),
        ({"expect": {"no_correction": True, "must_correct": ["went"]}}, "conflicts"),
    ],
)
def test_invalid_cases_are_rejected(override, message):
    with pytest.raises(ValueError, match=message):
        case(**override)


def test_loader_rejects_unknown_scenario_and_duplicates(tmp_path):
    (tmp_path / "a.yaml").write_text(
        "cases:\n  - {id: x, mode: scenario, scenario: nope, learner: hi}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="unknown scenario"):
        load_cases(tmp_path)
    (tmp_path / "a.yaml").write_text(
        "reviewed_by: Ms Mai\ncases:\n  - {id: x, mode: free_chat, learner: hi}\n",
        encoding="utf-8",
    )
    assert load_cases(tmp_path)[0].reviewed_by == "Ms Mai"
    (tmp_path / "b.yaml").write_text(
        "cases:\n  - {id: x, mode: free_chat, learner: hi}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_cases(tmp_path)


# ---- rule checks ----------------------------------------------------------------------
def test_rule_checks_pass_for_a_good_reply():
    c = case(
        expect={
            "must_correct": ["went"],
            "max_corrections": 1,
            "ends_with_question": True,
            "terminal": ["ask_question"],
            "max_sentences": 3,
            "forbid": ["Do you understand"],
        }
    )
    r = result(
        segments=[
            SegmentOut(lang="vi", text="Bạn kể hay, chỉ cần nói went thay cho go."),
            SegmentOut(lang="en", text="What did you see?"),
        ],
        mistakes=[MistakeOut(type="grammar", original="go", corrected="went")],
        terminal="ask_question",
        terminal_args={"question": "What did you see?"},
    )
    checks = check_case(c, r)
    assert all(o.passed for o in checks.values()), checks


def test_rule_checks_catch_failures():
    c = case(
        expect={
            "no_correction": True,
            "en_only": True,
            "vi_ratio": [0.0, 0.2],
            "must_not_correct": ["coffee"],
            "no_flags": ["cefr_overshoot"],
            "must_include_any": ["market"],
        }
    )
    r = result(
        segments=[SegmentOut(lang="vi", text="Bạn viết coffee sai rồi nhé.")],
        mistakes=[MistakeOut(type="spelling", original="cofee", corrected="coffee")],
        flags=["cefr_overshoot", "dropped_chars"],
    )
    checks = check_case(c, r)
    failed = {n for n, o in checks.items() if not o.passed}
    assert failed == {
        "clean_output",
        "no_correction",
        "en_only",
        "vi_ratio",
        "must_not_correct[coffee]",
        "no_flag[cefr_overshoot]",
        "must_include_any",
    }


def test_ask_question_must_be_said_in_the_reply():
    c = case()
    said = result(
        segments=[SegmentOut(lang="en", text="What did you see?")],
        terminal="ask_question",
        terminal_args={"question": "What did you see?"},
    )
    assert check_case(c, said)["question_said"].passed
    hidden = said.model_copy(update={"segments": [SegmentOut(lang="en", text="Two dollars.")]})
    assert not check_case(c, hidden)["question_said"].passed


def test_failed_turn_short_circuits():
    checks = check_case(case(expect={"must_correct": ["went"]}), result(failed="providers_down"))
    assert list(checks) == ["turn_completed"] and not checks["turn_completed"].passed


def test_must_correct_accepts_alternatives_and_ignores_punctuation():
    c = case(expect={"must_correct": ["would like|I'd like"]})
    r = result(segments=[SegmentOut(lang="en", text='Say "I\'d like a coffee."')])
    assert check_case(c, r)["must_correct[would like]"].passed


# ---- end to end with a fake model -----------------------------------------------------
async def test_run_cases_seeds_history_and_records_everything(tmp_path):
    c = case(
        id="sc1",
        mode="scenario",
        scenario="cafe-order",
        history=[{"learner": "Hi", "teacher": "<en>Hello! What would you like?</en>"}],
        learner="I go here yesterday",
        expect={"must_correct": ["went"], "terminal": ["ask_question"]},
        judge=["correction_quality"],
    )
    text = "<vi>Bạn nói hay, chỉ cần nói went thay cho go.</vi><en>What did you see?</en>"
    provider = FakeProvider([reply(text, [MISTAKE, ASK])])
    [r] = await run_cases([c], provider)

    sent = provider.requests[0].messages
    rendered = json.dumps([m.model_dump() for m in sent], ensure_ascii=False)
    assert "[Học viên viết]: Hi" in rendered and "Hello! What would you like?" in rendered
    assert [s.text for s in r.segments][-1] == "What did you see?"
    assert r.mistakes[0].corrected == "went" and r.terminal == "ask_question"
    assert r.cost_usd == pytest.approx(0.0001) and r.model == "fake/model"
    assert r.prompt_version.startswith("teacher.text.scenario#") and not r.reviewed
    assert r.passed

    write_run(tmp_path, [c], [r])
    prompt = (tmp_path / JUDGE_PROMPT).read_text(encoding="utf-8")
    assert "## sc1" in prompt and "correction_quality" in prompt
    assert str((tmp_path / JUDGE_SCORES).resolve()) in prompt
    assert (tmp_path / RESULTS).exists()


# ---- judge scores and report ----------------------------------------------------------
def test_judge_scores_are_validated(tmp_path):
    cases = {"c1": case(judge=["correction_quality"])}
    path = tmp_path / "scores.jsonl"
    path.write_text('{"id": "c1", "scores": {"correction_quality": 4}, "issues": ""}\n')
    assert load_judge_scores(path, cases)["c1"].scores == {"correction_quality": 4}
    for bad, message in [
        ('{"id": "zz", "scores": {}}', "unknown case"),
        ('{"id": "c1", "scores": {"task_boundaries": 4}}', "unrequested"),
        ('{"id": "c1", "scores": {"correction_quality": 7}}', "outside 1-5"),
        ("not json", "1"),
    ]:
        path.write_text(bad + "\n")
        with pytest.raises(ValueError, match=message):
            load_judge_scores(path, cases)


def test_report_and_regressions():
    good = result(segments=[SegmentOut(lang="en", text="Hi!")])
    good.checks = {"clean_output": CheckOutcome(passed=True)}
    bad = good.model_copy(deep=True)
    bad.checks = {"clean_output": CheckOutcome(passed=False, detail="flags: format_leak")}
    old_score = {"c1": JudgeScore(id="c1", scores={"conversation_flow": 5})}
    new_score = {"c1": JudgeScore(id="c1", scores={"conversation_flow": 3}, issues="ignores it")}

    lines = regressions([bad], new_score, [good], old_score)
    assert any("passed 1/1, now 0/1 (clean_output)" in line for line in lines)
    assert any("conversation_flow 5→3" in line for line in lines)

    report = render_report("run", [bad], new_score, lines)
    assert "Cases passing every check: 0/1" in report
    assert "| conversation_flow | 3.00 |" in report and "ignores it" in report
    assert "Regressions vs baseline" in report


def test_load_run_reads_results_and_optional_scores(tmp_path):
    r = result(id="fc-past-tense", segments=[SegmentOut(lang="en", text="Hi")])
    (tmp_path / RESULTS).write_text(r.model_dump_json() + "\n", encoding="utf-8")
    results, scores = load_run(tmp_path)
    assert results[0].id == "fc-past-tense" and scores == {}
    (tmp_path / JUDGE_SCORES).write_text(
        '{"id": "fc-past-tense", "scores": {"correction_quality": 5}}\n', encoding="utf-8"
    )
    assert load_run(tmp_path)[1]["fc-past-tense"].scores["correction_quality"] == 5


async def test_repeat_runs_every_case_and_judges_the_first_run_only(tmp_path):
    c = case(id="fc1", judge=["conversation_flow"])
    text = "<en>Nice!</en>"
    provider = FakeProvider([reply(text, []), reply("junk<en>Nice!</en>", []), reply(text, [])])
    results = await run_cases([c], provider, repeat=3, concurrency=1)

    assert sorted(r.run for r in results) == [0, 1, 2]
    noisy = next(r for r in results if r.dropped_samples)
    assert noisy.dropped_samples == ["junk"] and not noisy.passed
    assert "outside tags: ['junk']" in noisy.checks["clean_output"].detail

    write_run(tmp_path, [c], results)
    assert (tmp_path / JUDGE_PROMPT).read_text(encoding="utf-8").count("## fc1") == 1
    report = render_report("run", results, {})
    assert "Runs passing every check: 2/3" in report
    assert "Cases passing in every run: 0/1" in report
    assert "`fc1`: 2/3" in report and f"### fc1#{noisy.run + 1} (FAIL)" in report


def test_regressions_compare_pass_rates_across_repeat_counts():
    ok = result(segments=[SegmentOut(lang="en", text="Hi!")])
    ok.checks = {"clean_output": CheckOutcome(passed=True)}
    bad = ok.model_copy(deep=True)
    bad.checks = {"clean_output": CheckOutcome(passed=False, detail="flags: format_leak")}
    runs = [ok.model_copy(update={"run": 0}), bad.model_copy(update={"run": 1})]
    runs.append(ok.model_copy(update={"run": 2}))

    assert regressions(runs, {}, [ok], {}) == []  # 1/1 -> 2/3 is noise, not a regression
    worse = [bad.model_copy(update={"run": i}) for i in range(2)] + [runs[0]]
    assert regressions(worse, {}, [ok], {}) == ["- `c1`: passed 1/1, now 1/3 (clean_output)"]


def test_with_personas_runs_each_case_per_teacher_and_maps_ids_back(tmp_path):
    base = case(id="fc1", judge=["conversation_flow"])
    variants = with_personas([base], ["thu", "duc"])
    assert [(c.id, c.persona) for c in variants] == [("fc1@thu", "thu"), ("fc1@duc", "duc")]
    with pytest.raises(ValueError, match="ghost"):
        with_personas([base], ["ghost"])

    cases = {"fc1": base}
    assert find_case(cases, "fc1@duc").persona == "duc"
    assert find_case(cases, "fc1") is base
    assert find_case(cases, "nope@duc") is None and find_case(cases, "fc1@") is None


def test_load_run_rechecks_persona_variants(tmp_path):
    r = result(id="fc-past-tense@duc", segments=[SegmentOut(lang="en", text="Hi")])
    (tmp_path / RESULTS).write_text(r.model_dump_json() + "\n", encoding="utf-8")
    (tmp_path / JUDGE_SCORES).write_text(
        '{"id": "fc-past-tense@duc", "scores": {"correction_quality": 4}}\n', encoding="utf-8"
    )
    results, scores = load_run(tmp_path)
    assert results[0].checks and scores["fc-past-tense@duc"].scores["correction_quality"] == 4


def test_judge_prompt_gives_each_teacher_its_own_pronouns(tmp_path):
    kid = case(id="k1", age_band="under_18", judge=["natural_vietnamese"], notes="pronouns cô/em")
    variants = with_personas([kid], ["khoa", "duc"])
    pairs = [(c, result(id=c.id, persona=c.persona)) for c in variants]
    prompt = build_judge_prompt(pairs, tmp_path / JUDGE_SCORES)
    assert 'the teacher says "anh", the learner is "em"' in prompt
    assert 'the teacher says "ông", the learner is "cháu"' in prompt
