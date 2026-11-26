"""Read-only export of real Talkwise conversations, as raw material for new eval cases.

What leaves the database (everything else stays, see ``SESSIONS_SQL`` / ``MESSAGES_SQL``):
  - sessions: mode, channel, scenario id, teacher id, and the learner's CEFR level and age band;
  - messages: role, text and the old engine's correction (wrong / right / why), in order;
  - scenarios: the public lesson content (title, level, goals, opener, targets).
Never exported: names, emails, account and auth data, device ids, pictures, feedback, assistant
chats, learner memory, analyses, timestamps, and any session of a learner under 18 or of a
suspended account. Session ids are replaced by salted hashes that change on every export, and
emails, links, phone-like numbers and the learner's own name are masked in every text.

The output (``exports/<time>/``, gitignored) is never loaded by the eval runner: a person or a
coding agent picks lines from ``draft_cases.yaml``, writes the ``expect`` block and moves them
into ``evals/cases/teacher/``.

Run (needs the ``export`` extra and TALKWISE_DATABASE_URL, ideally a read-only Neon role):
    uv run --extra export python -m ea_llm.evals.talkwise_export [--limit 200]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

ENV_URL = "TALKWISE_DATABASE_URL"
OUT_DIR = Path("exports")
HISTORY_TURNS = 2  # earlier learner/teacher pairs kept before each drafted learner line

# Only these columns are selected; the user's name is read for masking and never written out.
SESSIONS_SQL = """
SELECT s.id, s.mode, s.channel, s.scenario_id, s.persona_id,
       u.level AS cefr, u.age_band, u.name AS learner_name
FROM sessions s JOIN users u ON u.id = s.user_id
WHERE s.status = 'done'
  AND u.disabled_at IS NULL
  AND (u.age_band IS NULL OR u.age_band <> 'under_18')
  AND EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id AND m.role = 'user')
ORDER BY s.started_at DESC
LIMIT %(limit)s
"""
MESSAGES_SQL = """
SELECT session_id, role, text, fix FROM messages
WHERE session_id = ANY(%(ids)s)
ORDER BY session_id, idx
"""
SCENARIOS_SQL = """
SELECT id, title, level, goal, goal_vi, opener, targets FROM scenarios
WHERE active ORDER BY level, id
"""

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
_PHONE_RE = re.compile(r"(?<!\w)\+?\d(?:[\s.\-]?\d){7,}(?!\w)")  # 8+ digits: phones, ids


def redact(text: str, name: str | None = None) -> str:
    """Mask emails, links, long numbers and the learner's name.

    The full name is masked in any case; its single words ("Nguyễn Văn Bình" -> "Bình") only
    when capitalised and 3+ letters, so short names like "An" do not eat the English "an".
    """
    text = _EMAIL_RE.sub("[EMAIL]", text)
    text = _URL_RE.sub("[URL]", text)
    text = _PHONE_RE.sub("[NUMBER]", text)
    name = (name or "").strip()
    if len(name) >= 2:
        text = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", "[NAME]", text, flags=re.I)
        for part in name.split():
            if len(part) >= 3 and part[0].isupper():
                text = re.sub(rf"(?<!\w){re.escape(part)}(?!\w)", "[NAME]", text)
    return text


def _pseudonym(salt: str, session_id: Any) -> str:
    return hashlib.sha256(f"{salt}:{session_id}".encode()).hexdigest()[:12]


def build_conversations(
    sessions: list[dict[str, Any]], messages: list[dict[str, Any]], salt: str
) -> list[dict[str, Any]]:
    """One record per session with masked turns; drops the name used for masking."""
    by_session: dict[Any, list[dict[str, Any]]] = {}
    for m in messages:
        by_session.setdefault(m["session_id"], []).append(m)
    out = []
    for s in sessions:
        name = s.get("learner_name")
        turns = []
        for m in by_session.get(s["id"], []):
            turn: dict[str, Any] = {"role": m["role"], "text": redact(m["text"] or "", name)}
            fix = m.get("fix")
            if isinstance(fix, dict) and fix.get("wrong"):
                turn["fix"] = {
                    k: redact(str(fix.get(k) or ""), name) for k in ("wrong", "right", "why")
                }
            turns.append(turn)
        out.append(
            {
                "id": _pseudonym(salt, s["id"]),
                "mode": s.get("mode") or "scenario",
                "channel": s.get("channel") or "text",
                "scenario_id": s.get("scenario_id"),
                "persona_id": s.get("persona_id"),
                "cefr": s.get("cefr") or "B1",
                "age_band": s.get("age_band") or "unknown",
                "turns": turns,
            }
        )
    return out


def build_drafts(conversations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One draft case per learner line, with up to two earlier exchanges as history."""
    drafts = []
    for conv in conversations:
        pairs: list[dict[str, str]] = []  # completed learner -> teacher exchanges
        pending: str | None = None
        for i, turn in enumerate(conv["turns"]):
            if turn["role"] == "user":
                draft: dict[str, Any] = {
                    "id": f"rw-{conv['id']}-{i}",
                    "mode": conv["mode"],
                    "talkwise_scenario": conv["scenario_id"],
                    "cefr": conv["cefr"],
                    "age_band": conv["age_band"],
                    "history": pairs[-HISTORY_TURNS:],
                    "learner": turn["text"],
                }
                if "fix" in turn:
                    draft["old_engine_fix"] = turn["fix"]
                drafts.append(draft)
                pending = turn["text"]
            elif pending is not None:  # the old teacher spoke English only
                pairs.append({"learner": pending, "teacher": f"<en>{turn['text']}</en>"})
                pending = None
    return drafts


def write_export(
    out_dir: Path,
    conversations: list[dict[str, Any]],
    scenarios: list[dict[str, Any]],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=False)
    with (out_dir / "conversations.jsonl").open("w", encoding="utf-8") as f:
        for conv in conversations:
            f.write(json.dumps(conv, ensure_ascii=False) + "\n")
    dump = {"allow_unicode": True, "sort_keys": False, "width": 100}
    (out_dir / "scenarios.yaml").write_text(yaml.safe_dump(scenarios, **dump), encoding="utf-8")
    drafts = {
        "note": "Drafts from real sessions: write an expect block before moving any into cases.",
        "drafts": build_drafts(conversations),
    }
    (out_dir / "draft_cases.yaml").write_text(yaml.safe_dump(drafts, **dump), encoding="utf-8")


def _connect(url: str):  # pragma: no cover - needs a database
    import psycopg
    from psycopg.rows import dict_row

    url = re.sub(r"^postgres(?:ql)?\+\w+://", "postgresql://", url)  # SQLAlchemy driver prefix
    return psycopg.connect(
        url,
        row_factory=dict_row,
        options="-c default_transaction_read_only=on -c statement_timeout=30000",
    )


def main(argv: list[str] | None = None) -> None:  # pragma: no cover - needs a database
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(description="Export Talkwise sessions for eval drafting.")
    parser.add_argument("--limit", type=int, default=200, help="newest finished sessions")
    args = parser.parse_args(argv)
    load_dotenv()
    url = os.environ.get(ENV_URL)
    if not url:
        raise SystemExit(f"{ENV_URL} is not set")
    with _connect(url) as conn:
        sessions = conn.execute(SESSIONS_SQL, {"limit": args.limit}).fetchall()
        ids = [s["id"] for s in sessions]
        messages = conn.execute(MESSAGES_SQL, {"ids": ids}).fetchall() if ids else []
        scenarios = conn.execute(SCENARIOS_SQL).fetchall()
    conversations = build_conversations(sessions, messages, secrets.token_hex(16))
    out_dir = OUT_DIR / datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    write_export(out_dir, conversations, scenarios)
    learner_lines = sum(t["role"] == "user" for c in conversations for t in c["turns"])
    print(f"{out_dir}: {len(conversations)} sessions, {learner_lines} learner lines")


if __name__ == "__main__":  # pragma: no cover
    main()
