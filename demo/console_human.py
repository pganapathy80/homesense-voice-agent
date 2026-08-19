"""The 'human agent screen' for the demo.

Tails data/handoffs.jsonl and data/tickets.jsonl and pretty-prints new rows as
the voice agent writes them. Run it in a second terminal beside the agent to
watch tickets and warm transfers land in real time.

    uv run console_human.py
    uv run console_human.py --from-start   # replay everything already on disk
"""

from __future__ import annotations

import argparse
import textwrap
import time
from pathlib import Path
from typing import Any

import backend

WIDTH = 78

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
GREEN = "\033[32m"


def _rule(char: str = "=", color: str = "") -> str:
    return f"{color}{char * WIDTH}{RESET}"


def print_handoff(row: dict[str, Any]) -> None:
    print()
    print(_rule("=", CYAN))
    print(f"{BOLD}{CYAN}{'INCOMING WARM TRANSFER'.center(WIDTH)}{RESET}")
    print(_rule("=", CYAN))
    print(f"  {BOLD}Handoff{RESET}   : {row.get('handoff_id')}")
    print(f"  {BOLD}Customer{RESET}  : {row.get('customer_name') or 'Unidentified'}")
    print(f"  {BOLD}Account{RESET}   : {row.get('account_number') or '-'}")
    print(f"  {BOLD}Ticket{RESET}    : {row.get('ticket_id') or '-'}")
    print(f"  {BOLD}Reason{RESET}    : {row.get('reason')}")
    print(f"  {BOLD}Mode{RESET}      : {row.get('mode')}")
    print(f"  {DIM}Received  : {row.get('created_at')}{RESET}")
    print(_rule("-", CYAN))
    print(f"  {BOLD}CONTEXT SUMMARY{RESET}")
    for line in textwrap.wrap(row.get("summary", ""), width=WIDTH - 4):
        print(f"  {line}")

    transcript = row.get("transcript") or []
    if transcript:
        print(_rule("-", CYAN))
        print(f"  {BOLD}TRANSCRIPT{RESET} {DIM}(last 6 turns){RESET}")
        for turn in transcript[-6:]:
            who = "Caller" if turn.get("role") == "user" else "Agent "
            for i, line in enumerate(textwrap.wrap(turn.get("text", ""), width=WIDTH - 12)):
                prefix = f"  {who} : " if i == 0 else "           "
                print(f"{prefix}{line}")
    print(_rule("=", CYAN))
    print()


def print_ticket(row: dict[str, Any]) -> None:
    print()
    print(_rule("-", YELLOW))
    print(f"{BOLD}{YELLOW}  NEW TICKET  {row.get('ticket_id')}{RESET}")
    print(_rule("-", YELLOW))
    print(f"  {BOLD}Customer{RESET} : {row.get('customer_name')}  ({row.get('account_number')})")
    print(f"  {BOLD}Device{RESET}   : {row.get('device_id') or '-'}")
    print(f"  {BOLD}Priority{RESET} : {row.get('priority')}    Status: {row.get('status')}")
    print(f"  {DIM}Opened   : {row.get('created_at')}{RESET}")
    print(f"  {BOLD}Issue{RESET}    :")
    for line in textwrap.wrap(row.get("issue_summary", ""), width=WIDTH - 6):
        print(f"    {line}")
    print(_rule("-", YELLOW))
    print()


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with open(path, encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def main() -> None:
    parser = argparse.ArgumentParser(description="Human-agent screen for the HomeSense demo.")
    parser.add_argument(
        "--from-start",
        action="store_true",
        help="Print rows already on disk before watching for new ones.",
    )
    parser.add_argument(
        "--interval", type=float, default=0.5, help="Poll interval in seconds (default 0.5)."
    )
    args = parser.parse_args()

    watched: list[tuple[Path, Any, str]] = [
        (backend.HANDOFFS_FILE, print_handoff, "handoffs"),
        (backend.TICKETS_FILE, print_ticket, "tickets"),
    ]

    offsets = {path: 0 if args.from_start else _count_lines(path) for path, _, _ in watched}

    print(_rule("=", GREEN))
    print(f"{BOLD}{GREEN}{'HOMESENSE - HUMAN SPECIALIST CONSOLE'.center(WIDTH)}{RESET}")
    print(_rule("=", GREEN))
    print(f"{DIM}  Watching: {backend.HANDOFFS_FILE}{RESET}")
    print(f"{DIM}            {backend.TICKETS_FILE}{RESET}")
    print(f"{DIM}  Waiting for the voice agent... (Ctrl-C to quit){RESET}\n")

    try:
        while True:
            for path, printer, _label in watched:
                rows = backend.read_jsonl(path)
                if len(rows) > offsets[path]:
                    for row in rows[offsets[path] :]:
                        printer(row)
                    offsets[path] = len(rows)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\n{DIM}  Console closed.{RESET}")


if __name__ == "__main__":
    main()
