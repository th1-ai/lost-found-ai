#!/usr/bin/env python3
"""tools/review.py - work the review queue: list / show / approve / edit / reject / send / ship.

    python3 tools/review.py list [--status needs_human] [--kind lost_found_return]
    python3 tools/review.py digest                          # one-line summary for a cron digest
    python3 tools/review.py show <id>
    python3 tools/review.py approve <id> [--duty-manager-ack "<name>"] [--note "..."]
    python3 tools/review.py edit <id> --body-file draft.txt [--subject "..."] [--note "..."]
    python3 tools/review.py reject <id> --reason "wrong item"
    python3 tools/review.py send                             # send everything approved/edited
    python3 tools/review.py ship <claim-id> [--duty-manager-ack "<name>"]
    python3 tools/review.py stale                             # go-live step: see below

Only this tool writes `approved` / `edited` / `rejected` (core/review.py). Only
`send` writes `sending` / `sent`. `ship` is the SECOND, separate human click the
spec calls for ("Nothing ships without a human approval click") - it only runs
after the return email is `sent`, and a high-value claim needs the same
`--duty-manager-ack` a second time: sending the email and handing the item to a
courier are two different decisions, and the roster's "cant" promise is about
both. Nothing here bypasses `mode: shadow` - see docs/safety.md.

`mode: shadow` blocks every write - `send` and `ship` alike - even for an item
you have approved or edited: approving in shadow only RECORDS the decision,
it does not queue a real send. Run `stale` once, right before flipping
`mode: shadow` -> `live` (`workflows/90-go-live.md`): it moves every un-sent
review row built up during shadow to `stale`, so nothing old goes out by
surprise the moment sending is switched on.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.adapters import get_email  # noqa: E402
from core.config import ConfigError, load_settings  # noqa: E402
from core.review import (WriteBlocked, approve, edit, list_queue, queue_summary, reject,  # noqa: E402
                         stale_backlog)
from core.store import Store, StoreError  # noqa: E402
from engine import revert_match, ship_claim  # noqa: E402


def _print_item_line(item) -> None:
    payload = item.payload or {}
    # `item.is_sample` is set by core (core/store.py) for anything read
    # through a mock adapter outside `make demo` - see docs/integrations.md
    # "Sample data is labelled". A human working the real queue must never
    # mistake a shipped fixture for a real guest.
    marker = "  [SAMPLE DATA]" if item.is_sample else ""
    if item.kind == "lost_found_return":
        hv = "  [HIGH VALUE]" if payload.get("high_value") else ""
        conf = payload.get("confidence", 0)
        label = str(payload.get("item_label") or "-")[:30]
        print(f"  {item.id}  {item.review_status:<14} claim {payload.get('claim_id', '-'):<24} "
             f"-> {label:<30} conf {conf:>3}%{hv}{marker}")
    else:
        subject = payload.get("subject") or (payload.get("extracted") or {}).get("description", "")
        print(f"  {item.id}  {item.review_status:<14} {item.kind:<16} {str(subject)[:50]}{marker}")


def cmd_list(store, args) -> int:
    items = list_queue(store, status=args.status, kind=args.kind, limit=args.limit)
    if not items:
        print("Nothing is waiting for you.")
        return 0
    print(f"{len(items)} item(s) waiting:\n")
    for item in items:
        _print_item_line(item)
    print("\nRun `python3 tools/review.py show <id>` for the full draft.")
    return 0


def cmd_digest(store, args) -> int:
    summary = queue_summary(store)
    print(f"{summary['waiting_on_human']} waiting for a person, "
         f"{summary['in_send_queue']} approved and queued to send, "
         f"{summary['sent']} sent so far.")
    return cmd_list(store, argparse.Namespace(status=None, kind=None, limit=args.limit))


def cmd_show(store, args) -> int:
    item = store.get_item(args.id)
    if item is None:
        print(f"error: no item {args.id}", file=sys.stderr)
        return 1
    payload = item.payload or {}
    draft = item.draft or {}
    if item.is_sample:
        print("[SAMPLE DATA] this item was read through a mock adapter, not your "
             "property - see docs/integrations.md.\n")
    print(f"item {item.id}  kind={item.kind}  status={item.review_status}")
    if item.kind == "lost_found_return":
        print(f"claim {payload.get('claim_id')} -> \"{payload.get('item_label')}\" "
             f"(item {payload.get('item_id')})")
        print(f"confidence {payload.get('confidence')}%  "
             f"high_value={'yes' if payload.get('high_value') else 'no'}")
        print(f"rationale: {payload.get('rationale')}")
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    if draft:
        print(f"\ndraft to {draft.get('to')}")
        print(f"subject: {draft.get('subject')}\n")
        print(draft.get("body", ""))
    events = store.list_events(item.id)
    if events:
        print("\nevents:")
        for ev in events:
            print(f"  {ev['ts']}  {ev['actor']:<6} {ev['action']}")
    return 0


def cmd_approve(store, args) -> int:
    item = store.get_item(args.id)
    if item is None:
        print(f"error: no item {args.id}", file=sys.stderr)
        return 1
    payload = item.payload or {}
    high_value = bool(payload.get("high_value"))
    if high_value and not args.duty_manager_ack:
        print(f"error: item {args.id} is high-value (claim {payload.get('claim_id')} -> "
             f"\"{payload.get('item_label')}\") - approve requires "
             f"--duty-manager-ack \"<name>\" before it can ship. See docs/safety.md.",
             file=sys.stderr)
        return 1
    note = args.note or ""
    if high_value:
        note = f"duty manager ack: {args.duty_manager_ack}" + (f" - {note}" if note else "")
    item = approve(store, args.id, note=note)
    print(f"approved {item.id} - now in the send queue")
    return 0


def cmd_edit(store, args) -> int:
    item = store.get_item(args.id)
    if item is None:
        print(f"error: no item {args.id}", file=sys.stderr)
        return 1
    payload = item.payload or {}
    high_value = bool(payload.get("high_value"))
    if high_value and not args.duty_manager_ack:
        print(f"error: item {args.id} is high-value - edit still requires "
             f"--duty-manager-ack \"<name>\" (same rule as approve). See docs/safety.md.",
             file=sys.stderr)
        return 1
    body = Path(args.body_file).read_text(encoding="utf-8")
    new_draft = dict(item.draft or {})
    new_draft["body"] = body
    if args.subject:
        new_draft["subject"] = args.subject
    note = args.note or ""
    if high_value:
        note = f"duty manager ack: {args.duty_manager_ack}" + (f" - {note}" if note else "")
    edit(store, args.id, new_draft, note=note)
    print(f"edited {item.id} - now in the send queue")
    return 0


def cmd_reject(store, args) -> int:
    item = store.get_item(args.id)
    if item is None:
        print(f"error: no item {args.id}", file=sys.stderr)
        return 1
    claim_id = (item.payload or {}).get("claim_id")
    item = reject(store, args.id, reason=args.reason or "")
    if claim_id:
        revert_match(store, claim_id)
        print(f"rejected {item.id} - claim {claim_id} and its item are back in the pool")
    else:
        print(f"rejected {item.id}")
    return 0


def cmd_send(store, settings, args) -> int:
    claimed = store.claim_for_send(limit=args.limit)
    if not claimed:
        print("Nothing approved or edited is waiting to send.")
        return 0
    email = get_email(settings)
    sent, failed = 0, 0
    for item in claimed:
        draft = item.draft or {}
        to = draft.get("to")
        try:
            result = email.send(to, draft.get("subject", ""), draft.get("body", ""), item=item)
        except WriteBlocked as exc:
            # Not a failure: the mode blocked it. The approval stands for go-live.
            store.transition(item.id, "approved", "agent", {"blocked": str(exc)[:200]})
            print(f"blocked {item.id} (approval kept): {exc}")
            failed += 1
            continue
        except Exception as exc:  # noqa: BLE001 - record and move on, never crash the batch
            store.mark_send_failed(item.id, str(exc))
            print(f"failed {item.id}: {exc}")
            failed += 1
            continue
        store.mark_sent(item.id, result.get("message_id"))
        print(f"sent {item.id} to {to}")
        sent += 1
    print(f"\n{sent} sent, {failed} failed.")
    return 0 if failed == 0 else 1


def cmd_ship(store, settings, args) -> int:
    result = ship_claim(settings, store, args.claim_id, duty_manager_ack=args.duty_manager_ack)
    if not result["ok"]:
        print(f"error: {result['reason']}", file=sys.stderr)
        return 1
    print(f"shipped {result['claim_id']} - tracking {result['tracking_number']} "
         f"({result['courier_note']}). Held {result['hold_days']} days if not collected.")
    if not result["logged"]:
        print("  note: could not write the courier log (see docs/safety.md and mode).")
    if not result["notified"]:
        print("  note: could not notify staff (see docs/safety.md and mode).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="what is waiting for a human")
    p_list.add_argument("--status", default=None)
    p_list.add_argument("--kind", default=None)
    p_list.add_argument("--limit", type=int, default=50)

    p_digest = sub.add_parser("digest", help="one-line summary, then the list (for a cron digest)")
    p_digest.add_argument("--limit", type=int, default=50)

    p_show = sub.add_parser("show", help="full detail for one item")
    p_show.add_argument("id")

    p_approve = sub.add_parser("approve", help="approve the draft unchanged")
    p_approve.add_argument("id")
    p_approve.add_argument("--duty-manager-ack", dest="duty_manager_ack", default=None,
                           help="required for a high-value match, e.g. \"Duty manager\"")
    p_approve.add_argument("--note", default="")

    p_edit = sub.add_parser("edit", help="rewrite the draft, then queue it")
    p_edit.add_argument("id")
    p_edit.add_argument("--body-file", required=True)
    p_edit.add_argument("--subject", default=None)
    p_edit.add_argument("--duty-manager-ack", dest="duty_manager_ack", default=None)
    p_edit.add_argument("--note", default="")

    p_reject = sub.add_parser("reject", help="discard the draft; the item goes back in the pool")
    p_reject.add_argument("id")
    p_reject.add_argument("--reason", default="")

    p_send = sub.add_parser("send", help="send everything approved or edited")
    p_send.add_argument("--limit", type=int, default=20)

    p_ship = sub.add_parser("ship", help="issue tracking and close the case (2nd approval click)")
    p_ship.add_argument("claim_id")
    p_ship.add_argument("--duty-manager-ack", dest="duty_manager_ack", default=None)

    sub.add_parser("stale", help="go-live step: mark everything still un-sent as stale "
                                 "(the shadow-era queue was never sent and is out of date)")

    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    store = Store(settings)
    try:
        if args.command == "list":
            return cmd_list(store, args)
        if args.command == "digest":
            return cmd_digest(store, args)
        if args.command == "show":
            return cmd_show(store, args)
        if args.command == "approve":
            return cmd_approve(store, args)
        if args.command == "edit":
            return cmd_edit(store, args)
        if args.command == "reject":
            return cmd_reject(store, args)
        if args.command == "send":
            return cmd_send(store, settings, args)
        if args.command == "ship":
            return cmd_ship(store, settings, args)
        if args.command == "stale":
            moved = stale_backlog(store)
            print(f"marked {len(moved)} item(s) stale. Nothing from before go-live will be sent.")
            return 0
        parser.error(f"unknown command {args.command}")
        return 2
    except StoreError as exc:
        print(f"cannot do that: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
