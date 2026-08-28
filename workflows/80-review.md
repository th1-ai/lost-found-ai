# Workflow: working the review queue

Objective: turn a matched claim into a decision - approve, edit, or reject -
send the return email once approved, and ship the item once the guest
confirms. Nothing reaches a guest, and nothing ships, without going through
this. `mode: shadow` blocks sending and shipping unconditionally, even for
an item you have approved - approving in shadow records the decision, it
does not queue a real send; see `docs/safety.md` for the full guard.

## Steps

1. **See what is waiting.**
   ```bash
   make review
   python3 tools/review.py digest      # a one-line summary first, useful for a morning check
   ```
   Each line shows the item id, its status (`pending_review` or
   `needs_human`), the claim id, the matched item, the confidence, and
   `[HIGH VALUE]` when it touches jewellery or documents.

2. **Read one in full.**
   ```bash
   python3 tools/review.py show <id>
   ```
   This prints the claim, the matched item, the confidence and rationale, the
   drafted return email, and the full event history. Summarise it for the
   hotel in plain language - who wrote in, what they lost, what matched it,
   how confident the match is - do not paste raw JSON at them.

3. **Decide.**
   ```bash
   python3 tools/review.py approve <id>
   python3 tools/review.py approve <id> --duty-manager-ack "Duty manager name"
   python3 tools/review.py edit <id> --body-file my-version.txt [--subject "New subject"]
   python3 tools/review.py reject <id> --reason "wrong item"
   ```
   **A `[HIGH VALUE]` item refuses `approve` (and `edit`) without
   `--duty-manager-ack "<name>"`.** This is the roster's "cant" promise in
   code, not just in this doc - `tools/review.py` will not let you skip it.
   Get the duty manager to actually look at the match first; the name you
   pass is recorded on the event, not just typed in.

   `reject` puts both the claim and the logged item back in the pool
   (`open` / `logged`) so the matcher can consider them again on the next
   pass - use it when the match is wrong, not when the guest simply has not
   replied yet. If it re-matches (the same pair, or a different one), the
   next `make run` queues a brand-new item for it - `make review` shows it
   again as a fresh `pending_review`/`needs_human` row, not the rejected one
   you already decided on.

4. **Send the return email.**
   ```bash
   python3 tools/review.py send
   ```
   This claims everything `approved`/`edited`, calls the email adapter's
   `send()`, and records the result. **`mode: shadow` blocks this
   unconditionally, even for an item you just approved** - approving in
   shadow only records the decision (`core/review.py`); nothing sends until
   `mode: live` (`workflows/90-go-live.md`). `send` reports each blocked
   item plainly (`blocked <id> (approval kept): mode is shadow...`) rather
   than crashing, and puts the item straight back to `approved` - a shadow
   block is not a failure, so it never needs `retry`; it just sends for
   real the moment `mode: live` and you run `send` again.

5. **Ship it - the second, separate click.** Only after the guest has replied
   confirming the address (or you have decided to proceed) and the return
   email shows as `sent`:
   ```bash
   python3 tools/review.py ship <claim-id>
   python3 tools/review.py ship <claim-id> --duty-manager-ack "Duty manager name"
   ```
   A high-value claim needs the ack a **second** time here - sending the
   email and handing the item to a courier are two different decisions, and
   `ship` will tell you plainly if the email has not actually gone out yet.
   `mode: shadow` blocks `ship` unconditionally too, the same as `send` - no
   tracking number is issued and the claim is not marked shipped. This
   prints a tracking number and the hold period; both the courier log
   entry and the staff notification are best-effort (see
   `docs/integrations.md` - no real courier is called).

6. **A failed send.** A real failure (bad mailbox credential, adapter error)
   marks the item `failed` with the error attached.
   ```bash
   python3 tools/review.py retry <id>
   ```
   re-queues it for another attempt after you have fixed the cause (usually a
   mailbox credential - `make doctor` will say which). A `mode: shadow`
   block is different - see step 4 - the item stays `approved`, not
   `failed`, so `retry` is neither needed nor available for it; just send
   again once you are in `mode: live`.

## Rules

- Only `tools/review.py` writes `approved` / `edited` / `rejected`.
- A high-value match (jewellery, documents) never reaches `approved` without
  a named duty-manager acknowledgement, and never ships without one either.
  This is enforced in code, not just written here.
- Confirm with the hotel before sending or shipping, even an approved item,
  the first few times. `workflows/90-go-live.md` covers when to stop doing
  that.
