"""Integration tests for the full loop (intake -> claim -> match) against the
bundled fixtures, with provider=mock and mode=shadow. No network, no
credentials - this is what `make demo` runs.
"""

from __future__ import annotations

from core.adapters import get_email
from core.config import load_settings
from core.store import Store
from tools.engine import (ensure_schema, intake_found_items, process_new_claim_email,
                          run_matching_pass)

EXPECTED_HIGH_VALUE_MATCHES = {
    "claim-claim-01-bracelet",
    "claim-claim-05-earring-highvalue",
    "claim-claim-06-passport-highvalue",
}


def _settings():
    settings = load_settings(provider="mock", mode="shadow")
    settings.agent.setdefault("lost_found", {}).setdefault("intake", {})["source"] = "fixtures"
    return settings


def _messages(settings):
    return get_email(settings).fetch_unread(limit=50)


def _run_full_pass(store, settings):
    intake_stats = intake_found_items(settings, store)
    for msg in _messages(settings):
        process_new_claim_email(settings, store, msg, provider="mock")
    match_stats = run_matching_pass(settings, store)
    return intake_stats, match_stats


def test_intake_logs_all_ten_items_and_flags_three_high_value(tmp_path):
    settings = _settings()
    store = Store(settings, path=tmp_path / "t1.db")
    ensure_schema(store)
    stats = intake_found_items(settings, store)
    assert stats["logged"] == 10
    assert stats["high_value"] == 3
    store.close()


def test_intake_is_idempotent_on_rerun(tmp_path):
    settings = _settings()
    store = Store(settings, path=tmp_path / "t2.db")
    ensure_schema(store)
    first = intake_found_items(settings, store)
    second = intake_found_items(settings, store)
    assert first["logged"] == 10
    assert second["logged"] == 0  # nothing new the second time
    store.close()


def test_full_pass_matches_three_claims_all_high_value(tmp_path):
    settings = _settings()
    store = Store(settings, path=tmp_path / "t3.db")
    ensure_schema(store)
    _intake, match_stats = _run_full_pass(store, settings)
    assert match_stats["matched"] == 3
    assert match_stats["escalated"] == 3  # every confident match here is high-value
    matched_ids = {row["id"] for row in store.db.execute(
        "SELECT id FROM lf_claims WHERE status='matched'").fetchall()}
    assert matched_ids == EXPECTED_HIGH_VALUE_MATCHES
    store.close()


def test_eyewear_trap_is_vetoed_not_matched(tmp_path):
    settings = _settings()
    store = Store(settings, path=tmp_path / "t4.db")
    ensure_schema(store)
    _run_full_pass(store, settings)
    row = store.db.execute(
        "SELECT * FROM lf_claims WHERE id='claim-claim-02-eyewear-trap'").fetchone()
    assert row["status"] == "open"
    assert "sunglasses" in row["rationale"].lower()
    assert "reading glasses" in row["rationale"].lower()
    store.close()


def test_matching_pass_is_idempotent_on_rerun(tmp_path):
    settings = _settings()
    store = Store(settings, path=tmp_path / "t5.db")
    ensure_schema(store)
    _run_full_pass(store, settings)
    second_pass = run_matching_pass(settings, store)
    # every matched claim is no longer "open", so re-running never re-queues
    # a second return email for it (docs/how-it-works.md "Idempotency").
    assert second_pass["queued"] == 0
    store.close()


def test_reject_then_rematch_creates_a_new_visible_queue_item(tmp_path):
    # SIMULATION.md round 2, new finding B: `Store.upsert_item` (core/
    # store.py) returns an EXISTING (source, external_id) row untouched once
    # one exists, regardless of its review_status. A bare
    # `external_id=claim_id` meant a claim's second-ever match - after a
    # human `reject` put it back in the pool (workflows/80-review.md) and
    # the matcher re-scored it, unchanged, on the next pass - silently
    # returned the OLD, terminal `rejected` row instead of a new one: the
    # claim believed it had a fresh confident match (`lf_claims.status:
    # matched`), the run's own narration said a return email was drafted,
    # but `make review` never showed it again. `run_matching_pass` now keys
    # each match attempt `<claim_id>:m<N>` (`store.next_sequence`), so a
    # re-match always creates a fresh row. Regression: match -> reject ->
    # re-run the matcher with no other change -> a NEW pending_review /
    # needs_human item must exist.
    from core.review import reject as review_reject
    from tools.engine import _active_return_item, revert_match

    settings = _settings()
    store = Store(settings, path=tmp_path / "t5b.db")
    ensure_schema(store)
    _run_full_pass(store, settings)

    claim_id = "claim-claim-01-bracelet"
    first = _active_return_item(store, claim_id)
    assert first is not None
    assert first.review_status == "needs_human"  # high-value match

    # Mirrors tools/review.py's cmd_reject exactly: reject the queue item,
    # then put the claim and its item back in the pool.
    review_reject(store, first.id, reason="wrong item")
    revert_match(store, claim_id)

    second_pass = run_matching_pass(settings, store)
    assert second_pass["queued"] == 1     # a fresh draft really was queued
    assert second_pass["escalated"] == 1  # still high-value, still needs_human

    rows = store.db.execute(
        "SELECT * FROM items WHERE source='lost_found' AND kind='lost_found_return' "
        "AND (external_id=? OR external_id LIKE ?) ORDER BY rowid",
        (claim_id, f"{claim_id}:m%")).fetchall()
    assert len(rows) == 2                          # the old row AND a new one
    assert rows[0]["id"] == first.id
    assert rows[0]["review_status"] == "rejected"   # untouched, still terminal

    second = _active_return_item(store, claim_id)
    assert second is not None
    assert second.id != first.id
    assert second.review_status == "needs_human"    # visible in `make review` again
    store.close()


def test_shadow_mode_never_sends_or_ships_anything(tmp_path):
    settings = _settings()
    store = Store(settings, path=tmp_path / "t6.db")
    ensure_schema(store)
    _run_full_pass(store, settings)
    counts = store.counts()
    assert counts.get("sent", 0) == 0
    assert counts.get("auto_sent", 0) == 0
    shipped = store.db.execute(
        "SELECT COUNT(*) AS n FROM lf_claims WHERE status='shipped'").fetchone()["n"]
    assert shipped == 0
    store.close()


def test_high_value_matches_are_queued_as_needs_human_not_pending_review(tmp_path):
    settings = _settings()
    store = Store(settings, path=tmp_path / "t7.db")
    ensure_schema(store)
    _run_full_pass(store, settings)
    rows = store.db.execute(
        "SELECT review_status FROM items WHERE kind='lost_found_return'").fetchall()
    statuses = {r["review_status"] for r in rows}
    assert statuses == {"needs_human"}


def test_dry_run_leaves_no_trace_and_is_repeatable(tmp_path):
    # "computes everything, writes nothing" (README/docs/safety.md): no DB
    # rows, no schema tables left behind, and running it twice in a row must
    # never hit an IntegrityError from a half-applied "write".
    from tools.run import one_pass
    settings = _settings()
    settings.dry_run = True
    store = Store(settings, path=tmp_path / "t8.db")

    code1, stats1 = one_pass(settings, store, limit=50, provider="mock")
    assert code1 == 0
    assert stats1["claims_new"] == 6

    tables = {r["name"] for r in store.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "lf_claims" not in tables  # even the schema migration was undone
    assert store.db.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 0
    assert store.db.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 0

    code2, stats2 = one_pass(settings, store, limit=50, provider="mock")
    assert code2 == 0
    assert stats2["claims_new"] == 6  # identical to the first pass - nothing stuck
    store.close()
