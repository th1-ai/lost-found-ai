#!/usr/bin/env python3
"""tools/demo.py - the whole loop on the bundled fixtures, zero credentials.

    make demo
    python3 tools/demo.py

Forces `llm.provider=mock`, `mode=shadow` and `lost_found.intake.source=
fixtures` regardless of config/*.yaml, so this always works on a fresh clone
with a blank .env (ARCHITECTURE.md section 1). Intake reads
fixtures/hotel/found_items.csv directly (10 items); the 6 sample guest emails
in fixtures/inbound/ become claims via the mock provider, matched against
fixtures/expected/extract_claim/*.json. Runs against its own database
(data/demo/demo.db) so running it twice always shows the same result, and
never touches data/agent.db (that is `make run`'s file).

Prints one line every check reads for the pass/fail signal:

    DEMO OK — 16 items processed, 3 drafted, 0 sent (shadow)
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.adapters import get_email  # noqa: E402
from core.config import ConfigError, load_settings, sub_data_dir  # noqa: E402
from core.log import summary_line  # noqa: E402
from core.store import Store  # noqa: E402
from engine import (ensure_schema, intake_found_items, process_new_claim_email,  # noqa: E402
                    run_matching_pass, sweep_stale_claims)


def main() -> int:
    # demo=True forces mock provider, shadow mode and the mock adapter for
    # every system, whatever the hotel has configured - so `make demo` can
    # never read a real mailbox, PMS or sheet, even after config/hotel.yaml
    # has been customised (core.config.load_settings).
    try:
        settings = load_settings(demo=True)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    # lost_found.intake.source is this agent's own knob, on top of that: the
    # one flag `make demo` sets for itself so intake reads the bundled CSV
    # instead of data/imports/ - see tools/engine.py:load_found_items_rows.
    settings.agent.setdefault("lost_found", {}).setdefault("intake", {})["source"] = "fixtures"

    demo_db = sub_data_dir("demo") / "demo.db"
    if demo_db.exists():
        demo_db.unlink()  # every `make demo` is a clean, repeatable run
    store = Store(settings, path=demo_db)
    ensure_schema(store)

    print("Lost & Found AI demo - fixtures/hotel/found_items.csv + "
         "fixtures/inbound/*.json\n")

    intake = intake_found_items(settings, store)
    print(f"Found-items log: {intake['logged']} item(s) logged from the floor sheet "
         f"({intake['high_value']} high-value - flagged for the duty manager the "
         "moment they were logged, before any claim exists).")

    email = get_email(settings)
    messages = email.fetch_unread(limit=50)
    if not messages:
        print("no fixtures found in fixtures/inbound/ - nothing to demo", file=sys.stderr)
        return 1

    claims_new, needs_human_email = 0, 0
    for msg in messages:
        _claim, needs_human = process_new_claim_email(settings, store, msg, provider="mock")
        if needs_human:
            needs_human_email += 1
        else:
            claims_new += 1
    print(f"Guest email: {len(messages)} message(s) read, {claims_new} turned into a "
         f"claim by the one LLM call this agent makes ({needs_human_email} were not "
         "actually a lost-item claim).\n")

    pass_stats = run_matching_pass(settings, store)
    for step in pass_stats["steps"]:
        print(f"  - {step['title']}")
        print(f"    {step['detail']}")
    print()
    print(pass_stats["headline"] + ".")
    print(f"{pass_stats['queued']} return email(s) queued for a human to approve, "
         f"{pass_stats['escalated']} of those are high-value and need the duty "
         "manager's sign-off before they can be approved (docs/safety.md).")

    expired = sweep_stale_claims(settings, store)
    if expired:
        print(f"{len(expired)} claim(s) already past the 14-day sweep in this sample data.")

    print("\nNothing was sent and nothing shipped: mode is shadow, and demo never "
         "approves anything on its own.")
    print("Next: `make review` to see the drafts, or read workflows/10-match-claims.md.\n")

    stats = {"processed": intake["read"] + len(messages), "drafted": pass_stats["queued"],
             "sent": 0}
    print(f"DEMO OK — {summary_line(stats, settings.mode)}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
