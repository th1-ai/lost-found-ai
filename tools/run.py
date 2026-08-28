#!/usr/bin/env python3
"""tools/run.py - Lost & Found AI's main loop.

    python3 tools/run.py --once
    python3 tools/run.py --watch
    python3 tools/run.py --once --dry-run
    python3 tools/run.py --once --limit 5
    python3 tools/run.py --once --provider mock

One pass: (1) read the found-items intake sheet and log anything new,
(2) read unread guest email and turn each new one into a structured claim
(the one LLM call in this agent - see docs/how-it-works.md), (3) run the
deterministic matcher over every open claim and every logged item, queueing a
return email for each confident match, (4) expire any claim whose 14-day
sweep has come due unmatched. Nothing is sent here - workflows/80-review.md
and docs/safety.md cover approving, sending and shipping.

Exit codes: 0 ok, 3 waiting on an `interactive` answer (see the message),
1 a real error.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.adapters import get_email  # noqa: E402
from core.adapters.base import AdapterError  # noqa: E402
from core.config import ConfigError, load_settings  # noqa: E402
from core.llm import LLMError, LLMPendingInteractive  # noqa: E402
from core.log import Run, get_logger, summary_line  # noqa: E402
from core.store import Store  # noqa: E402
from engine import intake_found_items, process_new_claim_email, run_matching_pass, sweep_stale_claims  # noqa: E402

log = get_logger("run")


def one_pass(settings, store, *, limit: int, provider: str | None) -> tuple[int, dict]:
    # `processed` / `drafted` / `sent` are what `core.log.summary_line` reads
    # for the one line every real run prints; the rest are this agent's own,
    # more detailed breakdown (SIMULATION.md finding 5 - the summary line
    # used to always read "0 items processed, 0 drafted, 0 sent" because
    # nothing here ever filled those three specific keys).
    stats = {"processed": 0, "drafted": 0, "sent": 0,
            "logged": 0, "claims_new": 0, "needs_human": 0, "matched": 0,
            "queued": 0, "escalated": 0, "expired": 0}
    snapshot = None
    if settings.dry_run:
        # "computes everything, writes nothing" has to mean nothing: no DB
        # rows, no sequence bumps, so a second --dry-run sees exactly what
        # the first one did and can never hit an IntegrityError from a
        # half-applied "write". core.store's write methods have no dry_run
        # switch of their own (they are meant to always really write, and
        # several - ensure_schema/migrate - use sqlite3's executescript(),
        # which silently commits any open transaction, so a plain manual
        # BEGIN/ROLLBACK around the pass does not hold). Instead: copy the
        # whole database to memory before the pass, and copy it straight
        # back after, whatever happened in between - SQLite's online backup
        # API, unaffected by that executescript quirk because it works on
        # the file, not a transaction.
        snapshot = sqlite3.connect(":memory:")
        store.db.backup(snapshot)
    try:
        with Run("match_claims", settings, store) as run:
            intake = intake_found_items(settings, store)
            stats["logged"] = intake["logged"]

            email = get_email(settings)
            messages = email.fetch_unread(limit=limit)
            seen = store.already_processed("email", [m.id for m in messages])
            for msg in messages:
                if msg.id in seen:
                    continue
                try:
                    claim, needs_human = process_new_claim_email(settings, store, msg,
                                                                  provider=provider)
                except LLMPendingInteractive as exc:
                    stats["processed"] = intake["read"] + len(messages)
                    run.stats = dict(stats)
                    print(str(exc))
                    return 3, stats
                if needs_human:
                    stats["needs_human"] += 1
                elif claim is not None:
                    stats["claims_new"] += 1
                    log.info("claim ingested", claim_id=claim.id, guest=claim.guest_name)

            pass_stats = run_matching_pass(settings, store)
            stats["matched"] = pass_stats["matched"]
            stats["queued"] = pass_stats["queued"]
            stats["escalated"] = pass_stats["escalated"]
            for step in pass_stats["steps"]:
                log.info("step", title=step["title"], detail=step["detail"])

            stats["expired"] = len(sweep_stale_claims(settings, store))
            reaped = store.reap_stuck_sending()
            if reaped:
                log.warn("reaped stuck sends", count=len(reaped))
            # `tools/demo.py` uses this same formula for its own DEMO OK
            # line - every found-item and email read this pass counts as
            # "processed", every return email queued for review counts as
            # "drafted". Nothing is ever `sent` here - only
            # `tools/review.py send` writes `sent`.
            stats["processed"] = intake["read"] + len(messages)
            stats["drafted"] = pass_stats["queued"]
            run.stats = dict(stats)
        return 0, stats
    finally:
        if snapshot is not None:
            snapshot.backup(store.db)
            snapshot.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--once", action="store_true", help="run a single pass (default)")
    mode_group.add_argument("--watch", action="store_true",
                            help="keep running on the configured interval")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute everything, write nothing, even in live mode")
    parser.add_argument("--limit", type=int, default=20, help="max new emails per pass")
    parser.add_argument("--provider", default=None,
                        help="override llm.provider for this run")
    parser.add_argument("--poll-seconds", type=int, default=None,
                        help="override the --watch interval (default: agent.yaml or 1800)")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(provider=args.provider, dry_run=args.dry_run)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    store = Store(settings)
    try:
        if args.watch:
            poll_seconds = args.poll_seconds or int(settings.agent_get("poll_seconds", 1800))
            while True:
                code, stats = one_pass(settings, store, limit=args.limit,
                                       provider=args.provider)
                print(summary_line(stats, settings.mode))
                if code != 0:
                    return code
                time.sleep(poll_seconds)
        code, stats = one_pass(settings, store, limit=args.limit, provider=args.provider)
        print(summary_line(stats, settings.mode))
        return code
    except AdapterError as exc:
        print(f"integration error: {exc}", file=sys.stderr)
        print("Run `make doctor` to see what is missing and how to fix it.", file=sys.stderr)
        return 1
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
