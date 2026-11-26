import json

import yaml

from ea_llm.evals.talkwise_export import (
    SESSIONS_SQL,
    build_conversations,
    build_drafts,
    redact,
    write_export,
)


def test_redact_masks_contacts_numbers_and_the_learner_name():
    text = "Hi, I'm Nguyễn Văn Bình, mail binh.nv@gmail.com, call 0912 345 678, see www.x.vn/a"
    out = redact(text, "Nguyễn Văn Bình")
    assert out == "Hi, I'm [NAME], mail [EMAIL], call [NUMBER], see [URL]"
    assert redact("Bình likes football", "Nguyễn Văn Bình") == "[NAME] likes football"


def test_redact_keeps_years_and_short_name_words():
    assert redact("I was born in 2005 and I have an apple", "Lê An") == (
        "I was born in 2005 and I have an apple"
    )
    assert redact("I am lê an", "Lê An") == "I am [NAME]"


def test_sessions_query_skips_minors_suspended_accounts_and_identity_columns():
    sql = SESSIONS_SQL.lower()
    assert "under_18" in sql and "disabled_at is null" in sql
    for column in ("email", "google_sub", "device_id", "password_hash", "started_at,"):
        assert f"u.{column}" not in sql and f"s.{column}" not in sql


SESSIONS = [
    {
        "id": "a1b2",
        "mode": "scenario",
        "channel": "text",
        "scenario_id": "coffee-shop",
        "persona_id": None,
        "cefr": "A2",
        "age_band": None,
        "learner_name": "Trần Thị Hoa",
    }
]
MESSAGES = [
    {"session_id": "a1b2", "role": "assistant", "text": "Hi! What can I get you?", "fix": None},
    {"session_id": "a1b2", "role": "user", "text": "I want coffee", "fix": None},
    {"session_id": "a1b2", "role": "assistant", "text": "Sure, Hoa! Hot or iced?", "fix": None},
    {
        "session_id": "a1b2",
        "role": "user",
        "text": "Iced, how much it is?",
        "fix": {"wrong": "how much it is", "right": "how much is it", "why": ""},
    },
]


def test_conversations_drop_the_name_and_pseudonymise_the_session():
    [conv] = build_conversations(SESSIONS, MESSAGES, salt="s1")
    assert conv["id"] != "a1b2" and len(conv["id"]) == 12
    assert "learner_name" not in conv and "Hoa" not in json.dumps(conv, ensure_ascii=False)
    assert conv["turns"][2]["text"] == "Sure, [NAME]! Hot or iced?"
    assert conv["turns"][3]["fix"]["right"] == "how much is it"
    assert conv["age_band"] == "unknown"
    assert build_conversations(SESSIONS, MESSAGES, salt="s2")[0]["id"] != conv["id"]


def test_drafts_are_one_per_learner_line_with_history():
    drafts = build_drafts(build_conversations(SESSIONS, MESSAGES, salt="s1"))
    assert [d["learner"] for d in drafts] == ["I want coffee", "Iced, how much it is?"]
    assert drafts[0]["history"] == []
    assert drafts[1]["history"] == [
        {"learner": "I want coffee", "teacher": "<en>Sure, [NAME]! Hot or iced?</en>"}
    ]
    assert drafts[1]["old_engine_fix"]["wrong"] == "how much it is"


def test_write_export_keeps_drafts_apart_from_eval_cases(tmp_path):
    conversations = build_conversations(SESSIONS, MESSAGES, salt="s1")
    write_export(tmp_path / "run", conversations, [{"id": "coffee-shop", "title": "Coffee"}])
    files = sorted(p.name for p in (tmp_path / "run").iterdir())
    assert files == ["conversations.jsonl", "draft_cases.yaml", "scenarios.yaml"]
    raw = yaml.safe_load((tmp_path / "run" / "draft_cases.yaml").read_text(encoding="utf-8"))
    assert "cases" not in raw and len(raw["drafts"]) == 2
