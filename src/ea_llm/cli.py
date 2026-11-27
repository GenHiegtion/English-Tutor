"""Terminal chat with the teacher: ``uv run ea-llm chat``."""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from ea_llm.adapters.memory import MemoryLearnerStore, load_seed_scenarios
from ea_llm.bootstrap import build_service, configure_logging
from ea_llm.config import ConfigError
from ea_llm.models import LearnerProfile
from ea_llm.teacher.context import LearnerInput, TurnContext
from ea_llm.teacher.events import (
    BoardUpdate,
    MistakeNoted,
    OutputSegment,
    TeacherEvent,
    TerminalAction,
    TurnEnd,
    TurnFailed,
)

BLUE, GREEN, DIM, YELLOW, RED, RESET = (
    "\033[94m",
    "\033[92m",
    "\033[2m",
    "\033[93m",
    "\033[91m",
    "\033[0m",
)
HELP = "Commands: /persona <id>  /state  /help  /quit"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ea-llm", description="Talk to the EA-LLM teacher.")
    sub = parser.add_subparsers(dest="command", required=True)
    chat = sub.add_parser("chat", help="chat in the terminal (text channel)")
    chat.add_argument("--mode", default="scenario", choices=["scenario", "free_chat"])
    chat.add_argument("--persona", default="lan", help="teacher id: lan, minh, hung, chi")
    chat.add_argument("--cefr", default="A2", choices=["A1", "A2", "B1", "B2", "C1", "C2"])
    chat.add_argument("--age", default="adult", choices=["under_18", "adult", "senior", "unknown"])
    chat.add_argument("--name", default=None, help="learner display name")
    chat.add_argument(
        "--scenario", default="cafe-order", help="scenario id, scenario mode only (see --list)"
    )
    chat.add_argument(
        "--interests", default="", help="comma-separated learner interests (free_chat topics)"
    )
    chat.add_argument("--offline", action="store_true", help="no network: canned replies")
    chat.add_argument("--list", action="store_true", help="list teachers and scenarios, then exit")
    serve = sub.add_parser("serve", help="run the dev HTTP server with API docs at /docs")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--offline", action="store_true", help="no network: canned replies")
    return parser


async def _chat(args: argparse.Namespace) -> int:
    scenarios = {s.id: s for s in load_seed_scenarios()}
    learners = MemoryLearnerStore()
    service = build_service(offline=args.offline, learners=learners)
    if args.list:
        for persona in await service.personas.list_personas():
            print(f"teacher  {persona.id:<6} {persona.name}")
        for sc in scenarios.values():
            print(f"scenario {sc.id:<16} [{sc.level}] {sc.title}")
        return 0
    mode = args.mode
    scenario = None
    if mode == "scenario":
        scenario = scenarios.get(args.scenario)
        if scenario is None:
            print(f"Unknown scenario '{args.scenario}'. Use --list.", file=sys.stderr)
            return 2

    persona_id = args.persona
    interests = [i.strip() for i in args.interests.split(",") if i.strip()]
    learners.put(
        LearnerProfile(
            user_id="cli",
            display_name=args.name,
            cefr=args.cefr,
            age_band=args.age,
            interests=interests,
        )
    )
    session_id = f"cli-{uuid.uuid4().hex[:8]}"
    try:
        snapshot = await service.open_session(
            session_id=session_id,
            user_id="cli",
            channel="text",
            mode=mode,
            persona_id=persona_id,
            scenario=scenario,
        )
    except ValueError as exc:
        print(f"{exc}. Use --list.", file=sys.stderr)
        return 2
    print(
        f"{DIM}{HELP}\nteacher={persona_id} address={snapshot.address.teacher}/"
        f"{snapshot.address.learner} prompt={snapshot.prompt_version}{RESET}"
    )

    async def run(text: str | None) -> None:
        ctx = TurnContext(
            session_id=session_id,
            user_id="cli",
            turn_id=uuid.uuid4().hex[:10],
            channel="text",
            mode=mode,
            persona_id=persona_id,
            scenario=scenario,
            learner_input=LearnerInput(text=text, source="typed") if text else None,
            received_at=0.0,
        )
        async for event in service.run_turn(ctx):
            _show(event)

    if mode == "scenario":
        await run(None)  # scenario opener, no LLM call
    else:
        print(f"{DIM}Free chat: say anything to start.{RESET}")
    loop = asyncio.get_running_loop()
    while True:
        try:
            line = (await loop.run_in_executor(None, input, f"\n{GREEN}you> {RESET}")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("/quit", "/exit"):
            return 0
        if line == "/help":
            print(HELP)
        elif line == "/state":
            snap = await service.history.load_session(session_id)
            print(snap.model_dump_json(indent=2) if snap else "no session")
        elif line.startswith("/persona"):
            new_id = line.split(maxsplit=1)[1] if " " in line else ""
            if await service.personas.get_persona(new_id):
                persona_id = new_id
                print(f"{DIM}next turn the teacher will be '{new_id}'{RESET}")
            else:
                print(f"{RED}unknown teacher '{new_id}'{RESET}")
        else:
            await run(line)


def _show(event: TeacherEvent) -> None:
    if isinstance(event, OutputSegment):
        color = BLUE if event.lang == "en" else RESET
        print(f"{color}[{event.lang}] {event.text}{RESET}")
    elif isinstance(event, BoardUpdate):
        print(f"{YELLOW}[board] {event.text} {event.note_vi or ''}{RESET}")
    elif isinstance(event, MistakeNoted):
        print(
            f"{YELLOW}[mistake:{event.type}] {event.original} -> {event.corrected}: "
            f"{event.explanation_vi}{RESET}"
        )
    elif isinstance(event, TerminalAction):
        print(f"{DIM}[action] {event.tool} {event.args or ''}{RESET}")
    elif isinstance(event, TurnEnd):
        flags = f" flags={event.guard_flags}" if event.guard_flags else ""
        fallback = " (fallback)" if event.fallback_used else ""
        tokens = (
            f"in={event.input_tokens} out={event.output_tokens} "
            f"cache_read={event.cache_read_tokens} cache_write={event.cache_write_tokens}"
        )
        print(
            f"{DIM}[turn] ttft={event.ttft_ms}ms {tokens} provider={event.provider}{fallback}"
            f"{flags}{RESET}"
        )
    elif isinstance(event, TurnFailed):
        print(f"{RED}[failed] {event.reason}{RESET}")


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from ea_llm.api.app import dev_app

    uvicorn.run(dev_app(offline=args.offline), host=args.host, port=args.port)
    return 0


def main() -> None:
    args = _parser().parse_args()
    configure_logging()
    try:
        code = _serve(args) if args.command == "serve" else asyncio.run(_chat(args))
    except ConfigError as exc:  # missing API key, malformed model id, ...
        print(f"{RED}error: {exc}{RESET}", file=sys.stderr)
        code = 2
    sys.exit(code)
