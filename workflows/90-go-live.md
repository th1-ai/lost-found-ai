# Workflow: shadow to live

Objective: decide, together with the hotel, whether Lost & Found AI is ready
to send approved return emails on its own instead of only drafting them - and
make the change safely if so.

This is the hotel's decision, never the agent's. Do not suggest it until the
checklist below is genuinely true, and when you do raise it, say plainly what
changes.

## Checklist

- [ ] `make doctor` is clean (no `FAIL` lines). `warn` on `mode` is expected
      until you flip it.
- [ ] `config/hotel.yaml` has the real property name, address and contact
      details, and `knowledge/property.md` + `knowledge/lost-and-found-policy.md`
      exist and are accurate (not the shipped examples) - the policy file's
      hold periods and duty-manager contact matter most here.
- [ ] At least a few days of real `make run` passes have gone through the
      review queue, not just the demo fixtures, including at least one
      high-value match so the duty-manager gate has actually been exercised.
- [ ] The hotel has read and edited enough drafted return emails to trust the
      matcher's confidence scores and the template's tone.
- [ ] The duty manager (named in `contacts.manager` in `config/hotel.yaml`)
      knows they are the one who has to type `--duty-manager-ack` before a
      jewellery or document match can be approved or shipped - going live
      never removes that gate.
- [ ] The hotel has decided on, and added, the AI-disclosure line to
      `knowledge/signature.md` (`docs/safety.md` has suggested wording and the
      EU AI Act Article 50 context).
- [ ] A real mailbox is connected (`systems.email.adapter: imap` or `gmail`)
      and a real found-items source is connected (`systems.sheets.adapter:
      csv` with `data/imports/found_items.csv` maintained, or `google`), and
      `make doctor` shows both healthy - going live on the `mock`/fixture
      adapters would only ever touch the sample data.
- [ ] `python3 tools/review.py stale` has been run (see "Making the change"
      below) - `mode: shadow` records an approval, it does not send it, so
      anything you approved while rehearsing needs clearing before it could
      be picked up the moment sending is switched on.

## Making the change

1. **Clear the shadow-era backlog first.**
   ```bash
   python3 tools/review.py stale
   ```
   Everything still `pending_review`, `needs_human`, `approved` or `edited`
   moves to `stale`. Nothing built up while you were rehearsing in shadow
   goes out the moment sending is switched on - `mode: shadow` records an
   approval, it does not queue it, so anything sitting approved from a shadow
   test is exactly this kind of backlog. A `stale` item can still be revived
   (`python3 tools/review.py show <id>` shows the transition history).
2. Edit `config/hotel.yaml`:
   ```yaml
   mode: live
   ```
3. `review.require_approval_for` still lists `send_email` by default - it
   should. Going live means **approved drafts get sent**, not that Lost &
   Found AI starts sending unapproved ones, and it does not touch the
   high-value duty-manager gate at all (that check runs regardless of mode).
4. Run `make doctor` again to confirm.
5. Run one real pass and manually watch a send go through:
   ```bash
   make run ARGS="--limit 1"
   python3 tools/review.py list
   python3 tools/review.py approve <id>
   python3 tools/review.py send
   ```
6. Tell the hotel exactly what just changed: an approved return email now
   actually leaves the mailbox the next time someone (or a scheduled job)
   runs `python3 tools/review.py send` - it is still never automatic before
   that approval, shipping is still always a separate second click
   (`tools/review.py ship`), and a high-value match still always needs the
   duty manager's name typed in by hand.

## Going back to shadow

```yaml
mode: shadow
```
in `config/hotel.yaml`, or `AGENT_MODE=shadow` in `.env` for one run. Either
stops every outbound action - sends and shipments alike - on the next pass,
mid-schedule, with no other change required.
