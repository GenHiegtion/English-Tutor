import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, update

from english_tutor.adapters.memory import load_seed_personas
from english_tutor.adapters.sql import SqlPersonaStore, create_engine

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "seed_personas.py"
NOW = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)


def load_script():
    spec = importlib.util.spec_from_file_location("seed_personas", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seed_personas = load_script()
TABLE = seed_personas.TEACHER_PERSONAS


@pytest.fixture
async def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(TABLE.metadata.create_all)
    yield engine
    await engine.dispose()


async def run_seed(engine, apply):
    async with engine.begin() as conn:
        return await seed_personas.seed(conn, load_seed_personas(), NOW, apply=apply)


async def all_rows(engine):
    async with engine.connect() as conn:
        return {r.id: r for r in (await conn.execute(select(TABLE))).all()}


async def test_dry_run_writes_nothing(engine):
    plan = await run_seed(engine, apply=False)
    assert [line.split()[0] for line in plan] == ["insert"] * len(load_seed_personas())
    assert await all_rows(engine) == {}


async def test_apply_inserts_the_teachers_the_service_can_read(engine):
    await run_seed(engine, apply=True)
    rows = await all_rows(engine)
    assert set(rows) == {p.id for p in load_seed_personas()}
    assert {(r.version, r.status, r.reviewed_by) for r in rows.values()} == {
        (1, "published", "developers")
    }
    assert all(r.description_vi for r in rows.values())  # for the app's teacher picker
    # The service never reads description_vi (it is not part of the prompt): it works whether
    # or not the app's migration 0032 has run.
    personas = await SqlPersonaStore(engine, ttl_s=0).list_personas()
    seeded = {p.id: p.model_copy(update={"description_vi": ""}) for p in load_seed_personas()}
    assert {p.id: p for p in personas} == seeded


async def test_rerun_keeps_unchanged_and_updates_changed_rows(engine):
    await run_seed(engine, apply=True)
    async with engine.begin() as conn:
        await conn.execute(update(TABLE).where(TABLE.c.id == "lan").values(personality="Old."))
        await conn.execute(
            TABLE.insert().values(
                id="emma",
                version=3,
                name="Emma",
                gender="female",
                age_band="adult",
                personality="x",
                switch_greeting="Hi",
                address={},
                fillers={},
                status="draft",
                updated_at=NOW,
            )
        )

    plan = await run_seed(engine, apply=True)

    assert "update lan to version 2 (personality changed)" in plan
    assert "keep   minh (version 1, unchanged)" in plan
    assert "leave  emma (not in personas.yaml, not touched)" in plan
    rows = await all_rows(engine)
    assert rows["lan"].version == 2 and rows["lan"].personality != "Old."
    assert rows["emma"].version == 3
