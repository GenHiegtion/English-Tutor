"""Copy the teachers in src/english_tutor/data/personas.yaml into the app's teacher_personas table.

The YAML file is the source of the teachers' content; the table is what the service reads.
New teachers are inserted, changed ones updated (version + 1), identical ones left alone.
Columns the YAML does not have (voice, avatar_key) are never touched.

Run:  uv run python scripts/seed_personas.py            # dry run: print the plan, write nothing
      uv run python scripts/seed_personas.py --apply    # write, in one transaction
The URL comes from $DATABASE_URL (or --url-env NAME). The service's own role should not be able
to write this table, so --apply usually needs the app's owner URL.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, Text, select
from sqlalchemy.ext.asyncio import AsyncConnection

from english_tutor.adapters.memory import load_seed_personas
from english_tutor.adapters.sql import create_engine
from english_tutor.models import Persona

REVIEWED_BY = "developers"
CONTENT_FIELDS = (
    "name",
    "gender",
    "age_band",
    "personality",
    "description_vi",
    "switch_greeting",
    "address",
    "fillers",
    "status",
)

# Every column the seed writes, as created by app migrations 0030 and 0032 (description_vi).
_METADATA = MetaData()
TEACHER_PERSONAS = Table(
    "teacher_personas",
    _METADATA,
    Column("id", String(40), primary_key=True),
    Column("version", Integer, nullable=False),
    Column("name", String(80), nullable=False),
    Column("gender", String(10), nullable=False),
    Column("age_band", String(10), nullable=False),
    Column("personality", Text, nullable=False),
    Column("description_vi", Text),
    Column("switch_greeting", Text, nullable=False),
    Column("address", JSON, nullable=False),
    Column("fillers", JSON, nullable=False),
    Column("status", String(12), nullable=False),
    Column("reviewed_by", String(120)),
    Column("reviewed_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


def content(persona: Persona) -> dict[str, Any]:
    """The table columns that come from the YAML file."""
    return {
        "name": persona.name,
        "gender": persona.gender,
        "age_band": persona.age_band,
        "personality": persona.personality,
        "description_vi": persona.description_vi or None,
        "switch_greeting": persona.switch_greeting,
        "address": {band: a.model_dump() for band, a in persona.address.items()},
        "fillers": persona.fillers,
        "status": persona.status,
    }


async def seed(
    conn: AsyncConnection, personas: list[Persona], now: datetime, *, apply: bool
) -> list[str]:
    """Plan (and with ``apply`` run) the insert/update of each teacher; one line per teacher.

    Without ``apply`` only a SELECT is sent. The caller owns the transaction.
    """
    query = select(TEACHER_PERSONAS)
    if apply:
        query = query.with_for_update()  # two seeds at once must not both insert
    existing = {row.id: row for row in (await conn.execute(query)).all()}
    reviewed = {"reviewed_by": REVIEWED_BY, "reviewed_at": now, "updated_at": now}
    plan: list[str] = []
    for persona in personas:
        values = content(persona)
        row = existing.get(persona.id)
        if row is None:
            if apply:
                await conn.execute(
                    TEACHER_PERSONAS.insert().values(id=persona.id, version=1, **values, **reviewed)
                )
            plan.append(f"insert {persona.id} ({persona.name}, {persona.status})")
            continue
        changed = [f for f in CONTENT_FIELDS if getattr(row, f) != values[f]]
        if not changed:
            plan.append(f"keep   {persona.id} (version {row.version}, unchanged)")
            continue
        if apply:
            await conn.execute(
                TEACHER_PERSONAS.update()
                .where(TEACHER_PERSONAS.c.id == persona.id)
                .values(version=row.version + 1, **values, **reviewed)
            )
        plan.append(
            f"update {persona.id} to version {row.version + 1} ({', '.join(changed)} changed)"
        )
    for persona_id in sorted(set(existing) - {p.id for p in personas}):
        plan.append(f"leave  {persona_id} (not in personas.yaml, not touched)")
    return plan


async def run(url: str, apply: bool) -> list[str]:
    """Seed in one transaction (committed only with ``apply``)."""
    engine = create_engine(url)
    try:
        async with engine.begin() if apply else engine.connect() as conn:
            return await seed(conn, load_seed_personas(), datetime.now(UTC), apply=apply)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, seed, and print the plan."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument("--url-env", default="DATABASE_URL", help="variable holding the DB URL")
    args = parser.parse_args(argv)
    load_dotenv()
    url = os.environ.get(args.url_env, "").strip()
    if not url:
        print(f"{args.url_env} is not set")
        return 1
    try:
        plan = asyncio.run(run(url, args.apply))
    except Exception as exc:  # print one line instead of a traceback that may echo the URL
        reason = str(exc).splitlines()[0] if str(exc) else ""
        print(f"seed failed: {type(exc).__name__}: {reason}")
        return 1
    print("\n".join(plan))
    print("applied" if args.apply else "dry run: nothing written (add --apply to write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
