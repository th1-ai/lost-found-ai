"""Tests for tools/review.py's command functions - the duty-manager-ack gate
on high-value matches, and reject putting the item back in the pool. These
call the cmd_* functions directly (the same functions main() dispatches to)
against a throwaway Store, so nothing here touches data/agent.db.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.config import Settings  # noqa: E402
from core.store import Store  # noqa: E402
from engine import (ensure_schema, update_claim, update_item, upsert_claim,  # noqa: E402
                    upsert_found_item)

import review as review_tool  # noqa: E402


def _seed_match(store: Store, *, high_value: bool) -> str:
    """Seed one lf_items/lf_claims pair plus the matched lost_found_return
    FSM item, mirroring what run_matching_pass would have produced."""
    ensure_schema(store)
    upsert_found_item(store, {"id": "fi-1", "item": "Gold hoop earring",
                              "description": "single earring", "found_where": "Reception",
                              "found_days_ago": 2})
    claim, _ = upsert_claim(store, "claim-1", guest_name="Elena Petrova",
                            contact="elena@example.com", description="gold hoop earring")
    update_claim(store, claim.id, status="matched", matched_item_id="fi-1",
                confidence=90, rationale="test rationale", high_value=int(high_value))
    update_item(store, "fi-1", status="matched", claim_id=claim.id)
    fsm_item = store.upsert_item(
        "lost_found", claim.id, kind="lost_found_return",
        payload={"claim_id": claim.id, "item_id": "fi-1", "item_label": "Gold hoop earring",
                "confidence": 90, "high_value": high_value, "rationale": "test rationale"})
    store.set_fields(fsm_item.id, draft={"to": "elena@example.com", "subject": "We have it",
                                        "body": "Dear Elena, ..."})
    store.transition(fsm_item.id, "needs_human" if high_value else "pending_review")
    return fsm_item.id


def test_approve_high_value_without_ack_is_refused(tmp_path, capsys):
    store = Store(path=tmp_path / "r1.db")
    item_id = _seed_match(store, high_value=True)
    args = argparse.Namespace(id=item_id, duty_manager_ack=None, note="")
    code = review_tool.cmd_approve(store, args)
    assert code == 1
    assert "duty-manager-ack" in capsys.readouterr().err
    assert store.get_item(item_id).review_status == "needs_human"  # unchanged
    store.close()


def test_approve_high_value_with_ack_succeeds(tmp_path):
    store = Store(path=tmp_path / "r2.db")
    item_id = _seed_match(store, high_value=True)
    args = argparse.Namespace(id=item_id, duty_manager_ack="Jane Doe", note="")
    code = review_tool.cmd_approve(store, args)
    assert code == 0
    assert store.get_item(item_id).review_status == "approved"
    store.close()


def test_approve_non_high_value_needs_no_ack(tmp_path):
    store = Store(path=tmp_path / "r3.db")
    item_id = _seed_match(store, high_value=False)
    args = argparse.Namespace(id=item_id, duty_manager_ack=None, note="")
    code = review_tool.cmd_approve(store, args)
    assert code == 0
    assert store.get_item(item_id).review_status == "approved"
    store.close()


def test_reject_puts_the_item_and_claim_back_in_the_pool(tmp_path):
    store = Store(path=tmp_path / "r4.db")
    item_id = _seed_match(store, high_value=False)
    args = argparse.Namespace(id=item_id, reason="wrong item")
    code = review_tool.cmd_reject(store, args)
    assert code == 0
    assert store.get_item(item_id).review_status == "rejected"
    claim_row = store.db.execute("SELECT * FROM lf_claims WHERE id='claim-1'").fetchone()
    item_row = store.db.execute("SELECT * FROM lf_items WHERE id='fi-1'").fetchone()
    assert claim_row["status"] == "open"
    assert claim_row["matched_item_id"] is None
    assert item_row["status"] == "logged"


def test_ship_refuses_a_high_value_claim_without_ack(tmp_path):
    from engine import ship_claim
    store = Store(path=tmp_path / "r5.db")
    item_id = _seed_match(store, high_value=True)
    settings = Settings(mode="live")
    # approve, send (mock email adapter), THEN try to ship without an ack.
    review_tool.approve(store, item_id, note="duty manager ack: Jane Doe")
    claimed = store.claim_for_send()
    store.mark_sent(claimed[0].id, "mock-1")
    result = ship_claim(settings, store, "claim-1", duty_manager_ack=None)
    assert result["ok"] is False
    assert "duty-manager-ack" in result["reason"]
    store.close()


def test_ship_refuses_before_the_email_is_actually_sent(tmp_path):
    from engine import ship_claim
    store = Store(path=tmp_path / "r6.db")
    _seed_match(store, high_value=False)
    settings = Settings(mode="live")
    result = ship_claim(settings, store, "claim-1")
    assert result["ok"] is False
    assert "not 'sent' yet" in result["reason"]
    store.close()


def test_ship_blocked_in_shadow_mode_even_when_approved_and_sent(tmp_path):
    """SIMULATION.md finding 1: mode: shadow used to let an already-approved
    claim actually ship (issue a real tracking number, mark it shipped) -
    only the courier-log write and staff ping were blocked. This is the
    regression test for the fix: shadow blocks the shipment itself too."""
    from engine import ship_claim
    store = Store(path=tmp_path / "r7.db")
    item_id = _seed_match(store, high_value=False)
    settings = Settings(mode="shadow")
    review_tool.approve(store, item_id)
    claimed = store.claim_for_send()
    store.mark_sent(claimed[0].id, "mock-1")
    result = ship_claim(settings, store, "claim-1")
    assert result["ok"] is False
    assert "shadow" in result["reason"].lower()
    claim_row = store.db.execute("SELECT * FROM lf_claims WHERE id='claim-1'").fetchone()
    assert claim_row["status"] != "shipped"
    assert claim_row["tracking_number"] is None
    store.close()


def test_ship_tracking_number_keeps_the_whole_descriptive_word(tmp_path):
    """SIMULATION.md finding 11: a blind claim_id[-6:] truncated
    'claim-claim-01-bracelet' to 'LF-ACELET-0001'. It should read BRACELET."""
    from engine import ship_claim
    store = Store(path=tmp_path / "r8.db")
    ensure_schema(store)
    upsert_found_item(store, {"id": "fi-9", "item": "Silver bracelet",
                              "description": "silver bracelet", "found_where": "Bar",
                              "found_days_ago": 1})
    claim, _ = upsert_claim(store, "claim-claim-01-bracelet", guest_name="Priya Shah",
                            contact="priya@example.com", description="silver bracelet")
    update_claim(store, claim.id, status="matched", matched_item_id="fi-9", confidence=90,
                rationale="test rationale", high_value=0)
    update_item(store, "fi-9", status="matched", claim_id=claim.id)
    fsm_item = store.upsert_item(
        "lost_found", claim.id, kind="lost_found_return",
        payload={"claim_id": claim.id, "item_id": "fi-9", "item_label": "Silver bracelet",
                "confidence": 90, "high_value": False, "rationale": "test rationale"})
    store.set_fields(fsm_item.id, draft={"to": "priya@example.com", "subject": "We have it",
                                        "body": "Dear Priya, ..."})
    store.transition(fsm_item.id, "pending_review")
    settings = Settings(mode="live")
    review_tool.approve(store, fsm_item.id)
    claimed = store.claim_for_send()
    store.mark_sent(claimed[0].id, "mock-1")
    result = ship_claim(settings, store, claim.id)
    assert result["ok"] is True
    # The old bug produced "LF-ACELET-0001" (claim_id[-6:] mid-word); the tag
    # segment itself must be the whole word, not just end with its letters.
    assert result["tracking_number"].split("-")[1] == "BRACELET"
    store.close()


def test_sample_item_shows_marker_in_list_line_and_show(tmp_path, capsys):
    """core/store.py tags an item read through a mock adapter outside `make
    demo` as `_sample` (`Item.is_sample`) - a human working the real queue
    must see that at a glance, in both `list` and `show`."""
    store = Store(path=tmp_path / "r10.db")
    item = store.upsert_item("email", "sample-marker-1", kind="message",
                             payload={"subject": "Found item enquiry",
                                      "from": "guest@example.com", "_sample": True})
    assert item.is_sample

    capsys.readouterr()
    review_tool._print_item_line(item)
    assert "[SAMPLE DATA]" in capsys.readouterr().out

    rc = review_tool.cmd_show(store, argparse.Namespace(id=item.id))
    assert rc == 0
    assert "[SAMPLE DATA]" in capsys.readouterr().out
    store.close()
