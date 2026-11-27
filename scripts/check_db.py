"""Read-only check that DATABASE_URL is ready for the English Tutor service.

Checks the connection, the role's privileges, the columns English Tutor reads and writes, and the
catalogs (published teachers, active scenarios). Sends only SELECTs and never prints the URL.

Run:  uv run python scripts/check_db.py
Exit code 1 when a check fails (the service would not work), 0 otherwise (warnings included).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from english_tutor.adapters.sql import (
    METADATA,
    SqlPersonaStore,
    SqlScenarioStore,
    create_engine,
)

ENV_URL = "DATABASE_URL"
READ_TABLES = ("users", "teacher_personas", "scenarios")
WRITE_TABLES = ("sessions", "messages", "ai_usage_logs")
WRITE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE")
EXTRA_PRIVILEGES = ("DELETE", "TRUNCATE")  # the service never needs these
SERIAL_TABLES = ("messages", "ai_usage_logs")  # integer ids drawn from a sequence
EXPECTED_PERSONAS = 4


@dataclass
class Report:
    """Collected check results; failures mean the service cannot run on this database."""

    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def ok(self, message: str) -> None:
        """Record a passed check."""
        print(f"  ok    {message}")

    def warn(self, message: str) -> None:
        """Record a problem the service survives."""
        self.warnings.append(message)
        print(f"  warn  {message}")

    def fail(self, message: str) -> None:
        """Record a problem that breaks the service."""
        self.failures.append(message)
        print(f"  FAIL  {message}")


async def _scalar(conn: AsyncConnection, sql: str, **params: object) -> object:
    return (await conn.execute(text(sql), params)).scalar()


async def check_role(conn: AsyncConnection, report: Report) -> None:
    """Report which role is connected and warn when it is more powerful than needed."""
    row = (
        await conn.execute(
            text(
                "SELECT current_user, current_database(), r.rolsuper, r.rolcreaterole "
                "FROM pg_roles r WHERE r.rolname = current_user"
            )
        )
    ).one()
    report.ok(f"connected as role '{row[0]}' to database '{row[1]}'")
    if row[2] or row[3]:
        report.warn("role is superuser or can create roles: use a limited role for the service")
    owned = await _scalar(
        conn,
        "SELECT count(*) FROM pg_tables WHERE schemaname = current_schema() "
        "AND tableowner = current_user AND tablename = ANY(:names)",
        names=list(READ_TABLES + WRITE_TABLES),
    )
    if owned:
        report.warn(
            f"role owns {owned} of the app tables (owner role): it may alter or drop them; "
            "create a limited role for the service"
        )


async def check_columns(conn: AsyncConnection, report: Report) -> bool:
    """Every table and column English Tutor mirrors must exist; False when a table is missing."""
    all_present = True
    for table in METADATA.sorted_tables:
        rows = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :name"
            ),
            {"name": table.name},
        )
        found = {r[0] for r in rows}
        if not found:
            report.fail(f"table '{table.name}' does not exist (is migration 0030 applied?)")
            all_present = False
            continue
        missing = sorted(c.name for c in table.columns if c.name not in found)
        if missing:
            report.fail(f"table '{table.name}' lacks columns: {', '.join(missing)}")
        else:
            report.ok(
                f"table '{table.name}' has the {len(table.columns)} columns English Tutor uses"
            )
    return all_present


async def _has(conn: AsyncConnection, table: str, privilege: str) -> bool:
    return bool(
        await _scalar(
            conn,
            "SELECT has_table_privilege(current_user, :table, :privilege)",
            table=table,
            privilege=privilege,
        )
    )


async def check_privileges(conn: AsyncConnection, report: Report) -> None:
    """The role must read the catalogs and write the history tables, and nothing more."""
    for table in READ_TABLES:
        if not await _has(conn, table, "SELECT"):
            report.fail(f"no SELECT on '{table}'")
        elif await _has(conn, table, "INSERT") or await _has(conn, table, "UPDATE"):
            report.warn(f"role can write '{table}', the service only reads it")
        else:
            report.ok(f"read-only access to '{table}'")
    for table in WRITE_TABLES:
        lacking = [p for p in WRITE_PRIVILEGES if not await _has(conn, table, p)]
        if lacking:
            report.fail(f"missing {', '.join(lacking)} on '{table}'")
            continue
        extra = [p for p in EXTRA_PRIVILEGES if await _has(conn, table, p)]
        if extra:
            report.warn(f"role also has {', '.join(extra)} on '{table}', not needed")
        else:
            report.ok(f"SELECT, INSERT, UPDATE on '{table}'")
    for table in SERIAL_TABLES:
        sequence = await _scalar(conn, "SELECT pg_get_serial_sequence(:t, 'id')", t=table)
        if sequence is None:
            report.warn(f"'{table}.id' has no sequence; inserts rely on another default")
            continue
        usable = await _scalar(
            conn, "SELECT has_sequence_privilege(current_user, :s, 'USAGE')", s=sequence
        )
        if usable:
            report.ok(f"USAGE on sequence {sequence}")
        else:
            report.fail(f"no USAGE on sequence {sequence}: inserts into '{table}' will fail")


async def check_catalogs(engine: AsyncEngine, conn: AsyncConnection, report: Report) -> None:
    """Teachers and scenarios the service would offer, parsed as the service parses them."""
    rows = (
        await conn.execute(text("SELECT status, count(*) FROM teacher_personas GROUP BY status"))
    ).all()
    by_status = {status: count for status, count in rows}
    personas = await SqlPersonaStore(engine, ttl_s=0).list_personas()
    if not personas:
        report.fail(
            "no usable published teacher in 'teacher_personas' "
            f"(rows by status: {by_status or 0}); POST /sessions answers 404 persona_not_found"
        )
    elif len(personas) < EXPECTED_PERSONAS:
        names = ", ".join(sorted(p.id for p in personas))
        report.warn(f"only {len(personas)} published teachers ({names}), expected 4")
    else:
        report.ok(f"published teachers: {', '.join(sorted(p.id for p in personas))}")
    published = by_status.get("published", 0)
    if published > len(personas):
        report.warn(f"{published - len(personas)} published teacher rows are invalid (see log)")

    scenarios = await SqlScenarioStore(engine, ttl_s=0).list_scenarios()
    if scenarios:
        report.ok(f"{len(scenarios)} active scenarios")
    else:
        report.warn("no active scenario: only free_chat sessions can be opened")


async def check_history(conn: AsyncConnection, report: Report) -> None:
    """How many sessions exist and how many of them were written by English Tutor."""
    total = await _scalar(conn, "SELECT count(*) FROM sessions")
    ours = await _scalar(conn, "SELECT count(*) FROM sessions WHERE snapshot IS NOT NULL")
    report.ok(f"sessions: {total} in total, {ours} written by English Tutor (with a snapshot)")


async def run(url: str) -> Report:
    """Run every check against one database."""
    report = Report()
    engine = create_engine(url)
    try:
        async with engine.connect() as conn:
            await check_role(conn, report)
            if await check_columns(conn, report):
                await check_privileges(conn, report)
                await check_catalogs(engine, conn, report)
                await check_history(conn, report)
    finally:
        await engine.dispose()
    return report


def main() -> int:
    """Load .env, run the checks and print a summary."""
    logging.basicConfig(level=logging.WARNING, format="  log   %(message)s")
    load_dotenv()
    url = os.environ.get(ENV_URL, "").strip()
    if not url:
        print(f"{ENV_URL} is not set")
        return 1
    print("Checking DATABASE_URL (read-only)")
    try:
        report = asyncio.run(run(url))
    except Exception as exc:  # report the failure instead of a traceback that may echo the URL
        reason = str(exc).splitlines()[0] if str(exc) else ""
        print(f"  FAIL  cannot check the database: {type(exc).__name__}: {reason}")
        return 1
    print(f"\n{len(report.failures)} failed, {len(report.warnings)} warnings")
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
