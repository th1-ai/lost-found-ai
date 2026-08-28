#!/usr/bin/env python3
"""tools/report.py - what Lost & Found AI did, and what it cost.

    make report
    python3 tools/report.py
    python3 tools/report.py --since 2026-08-01
    python3 tools/report.py --days 7

Reads data/agent.db - the real database `make run` writes to, never the demo
database. Prints found-items volume, claim outcomes (matched / vetoed or
still open / expired), how many high-value escalations went to the duty
manager, the review queue's edit rate (the loop that teaches the agent your
voice - see core/review.py), and LLM spend for the one model call this agent
makes (`extract_claim`). See docs/benefits.md for how these map to the
roster's ROI claim.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import ConfigError, load_settings  # noqa: E402
from core.review import queue_summary  # noqa: E402
from core.store import Store  # noqa: E402
from engine import ensure_schema  # noqa: E402


def _claim_counts(store: Store) -> dict[str, int]:
    rows = store.db.execute(
        "SELECT status, COUNT(*) AS n FROM lf_claims GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def _item_counts(store: Store) -> dict[str, int]:
    rows = store.db.execute(
        "SELECT status, COUNT(*) AS n FROM lf_items GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def _high_value_open(store: Store) -> int:
    row = store.db.execute(
        "SELECT COUNT(*) AS n FROM lf_claims WHERE high_value=1 AND status "
        "NOT IN ('shipped', 'expired')").fetchone()
    return row["n"] if row else 0


def _event_action_counts(store: Store, prefix: str = "status:") -> dict[str, int]:
    rows = store.db.execute(
        "SELECT action, COUNT(*) AS n FROM events WHERE action LIKE ? GROUP BY action",
        (f"{prefix}%",)).fetchall()
    return {r["action"][len(prefix):]: r["n"] for r in rows}


def _avg_days_to_match(store: Store) -> float | None:
    """Average days from a claim's creation to its return email being queued."""
    rows = store.db.execute(
        "SELECT c.created_at AS claimed, i.created_at AS queued FROM lf_claims c "
        "JOIN items i ON i.source='lost_found' AND i.external_id=c.id "
        "WHERE c.matched_item_id IS NOT NULL").fetchall()
    deltas = []
    for row in rows:
        try:
            a = datetime.fromisoformat(row["claimed"])
            b = datetime.fromisoformat(row["queued"])
        except (TypeError, ValueError):
            continue
        deltas.append((b - a).total_seconds() / 86400)
    return (sum(deltas) / len(deltas)) if deltas else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", default=None, help="ISO date/time - only LLM spend is filtered")
    parser.add_argument("--days", type=int, default=None,
                        help="shorthand for --since <today minus N days>")
    args = parser.parse_args(argv)

    since = args.since
    if args.days is not None:
        since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat(timespec="seconds")

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    store = Store(settings)
    ensure_schema(store)
    try:
        items = _item_counts(store)
        claims = _claim_counts(store)
        events = _event_action_counts(store)
        summary = queue_summary(store)
        avg_days = _avg_days_to_match(store)
        usage = store.usage_totals(since=since)

        print(f"Lost & Found AI - report ({settings.mode})\n")

        total_items = sum(items.values())
        print(f"Found items:   {total_items} logged  "
             f"({items.get('logged', 0)} in the safe, {items.get('matched', 0)} matched "
             f"awaiting shipment, {items.get('returned', 0)} returned)")

        total_claims = sum(claims.values())
        matched = claims.get("matched", 0) + claims.get("shipped", 0)
        print(f"Claims:        {total_claims} received  "
             f"({matched} matched, {claims.get('open', 0)} still open, "
             f"{claims.get('shipped', 0)} shipped, {claims.get('expired', 0)} expired "
             "on the 14-day sweep)")
        if total_claims:
            print(f"               {round(100 * matched / total_claims)}% of claims matched "
                 "to a logged item without staff chasing anything")

        hv_open = _high_value_open(store)
        print(f"High-value:    {claims.get('shipped', 0)} shipped with a duty-manager "
             f"sign-off, {hv_open} currently awaiting one")

        if avg_days is not None:
            print(f"Speed:         {avg_days:.1f} day(s) average from claim to a drafted "
                 "return email")

        print(f"\nReview queue:  {summary['waiting_on_human']} waiting for a person, "
             f"{summary['in_send_queue']} approved and queued to send, "
             f"{summary['sent']} sent")
        approved, edited = events.get("approved", 0), events.get("edited", 0)
        decided = approved + edited
        if decided:
            print(f"Edit rate:     {edited}/{decided} approved drafts were edited first "
                 f"({round(100 * edited / decided)}%) - see docs/benefits.md")

        label = f" since {since}" if since else ""
        print(f"\nLLM spend{label}:  {usage.get('calls', 0)} call(s), "
             f"{usage.get('input_tokens', 0)} in / {usage.get('output_tokens', 0)} out "
             f"tokens, ${usage.get('cost_usd', 0.0):.4f} (provider: {settings.llm.provider})")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
