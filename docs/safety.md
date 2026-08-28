# Guardrails and safety

This agent talks to your guests and touches your systems. Everything below is
built in, not optional, and this page explains what it does and what is left for
you to decide.

## The two modes

| Mode | What happens |
|---|---|
| `shadow` (default) | The agent reads, thinks, drafts and queues. It **never** sends a message and **never** writes to your PMS. |
| `live` | Items you approved are really sent. Everything else still waits. |

`mode` lives in `config/hotel.yaml`. It is a global kill switch: flipping it back
to `shadow` stops every outbound action immediately, mid-schedule, with no other
change. `config/agent.yaml` can be stricter than `hotel.yaml`, never looser.

**Shadow blocks every write, approved or not.** `python3 tools/review.py
approve` still works in shadow mode - it still moves an item into the send
queue and still teaches the agent from your edits - but `send` and `ship`
refuse to act on it while `mode: shadow`. Approving in shadow *records* the
decision; it does not queue a real send. That is why `workflows/90-go-live.md`
has you run `python3 tools/review.py stale` right before the flip: it clears
out anything approved during shadow rehearsals so nothing old goes out the
moment sending is switched on.

Two more brakes:

- `make run ARGS="--dry-run"` computes everything and writes nothing, even in
  live mode. Use it when you change a prompt.
- `review.require_approval_for` in `config/hotel.yaml` lists the actions that
  need a human even in live mode. The defaults are `send_email`, `send_message`,
  `pms_write`, `payment`, `publish`. Shortening that list is how you hand the
  agent more rope, one action at a time.

Every outbound action in the codebase goes through one function,
`core/review.py:assert_write_allowed`. There is no second path.

## The review queue

Nothing reaches a guest without passing through the queue.

```bash
make review                       # what is waiting
python3 tools/review.py show <id>  # the full draft and how it got there
python3 tools/review.py approve <id>
python3 tools/review.py edit <id> --body-file my-version.txt
python3 tools/review.py reject <id> --reason "wrong tone"
```

An item moves `new -> classified -> drafted -> pending_review` and then waits.
Only `tools/review.py` can write `approved`, `edited` or `rejected`; only
`tools/run.py` can write `sent`. A crash between "about to send" and "sent" is
picked up on the next pass and shown to you as failed rather than silently
retried.

**Your edits teach it.** When you rewrite a draft, the before and after are
stored. Over time that is what makes the drafts sound like your hotel instead of
like a machine.

## What the agent will not do

- Send anything while `mode: shadow`.
- Send an item a human has not approved, when the action needs approval.
- Take a payment, issue a refund, or move money. Payment adapters are read-only
  by design.
- Invent a fact that is not in `knowledge/` or in the data it was given. When it
  is not sure, it queues the item as `needs_human` instead of guessing.
- Argue. Complaints, refund requests, legal or medical topics, and anything that
  reads as distressed go straight to a person.
- **Approve, edit, or ship a match touching jewellery or an identity document
  (passport, ID card, driving licence) without a named duty-manager
  acknowledgement** - see "High-value items" below. This is the roster's
  explicit "cant" and it is enforced in code, not just written here.
- **Propose a match between two different things that read alike in free
  text but are never the same object** - today that means reading glasses
  and sunglasses. A pair like this is hard-vetoed to a score of zero and the
  claim stays open with a written reason, however similar the wording.
- **Draft a reply in a language `hotel.languages` (`config/hotel.yaml`) does
  not list.** Detected before the one LLM call runs (`core.i18n.
  detect_language`, no extra cost) - the item goes straight to `needs_human`
  with the language it was actually written in, and the property's own
  default language, so a person answers it instead of a guess.

## High-value items: the duty-manager gate

Any confident match where the claim or the logged item is jewellery
(bracelets, rings, necklaces, earrings, watches with precious metal or
stones) or an identity document (passports, ID cards, driving licences) is
forced to `needs_human` the moment it is scored, regardless of confidence -
`tools/engine.py:is_high_value`, families configurable in `config/agent.yaml`
(`lost_found.high_value_families`).

```bash
python3 tools/review.py approve <id> --duty-manager-ack "Duty manager name"
python3 tools/review.py ship <id>    --duty-manager-ack "Duty manager name"
```

`tools/review.py` refuses `approve`, `edit`, and `ship` on a high-value item
without that flag - both when the return email is approved and again, a
second time, when the item actually ships. The name is recorded on the audit
trail (`python3 tools/review.py show <id>`), so this is not a box to tick -
the duty manager named in `contacts.manager` (`config/hotel.yaml`) should
have actually looked at the claim and the item first. A high-value item is
also flagged the moment it is logged, before any claim exists, so the duty
manager knows what is sitting in the safe (`workflows/10-match-claims.md`).

## Data handling

**What leaves your machine.** With `llm.provider: anthropic` or `claude-code`,
the prompt goes to Anthropic. That prompt contains the guest message and the
relevant property facts. With `llm.provider: mock` or `interactive`, nothing
leaves the machine at all.

**What is stored, and where.** Everything lives in `data/` inside this folder:
`agent.db` (SQLite), `logs/*.jsonl`, `exports/`. `data/` is gitignored. There is
no cloud service behind this repo and no telemetry.

**Card numbers are redacted on the way in.** Every inbound message passes through
`core/redact.py` before it is stored, logged or put into a prompt. A payment card
number is replaced with `[CARD REDACTED ****1234]`, and labelled CVC and expiry
values in the same message go with it. Detection requires a real card prefix and
a valid Luhn checksum, so booking references and door codes survive. IBANs are
masked the same way. Nothing you can do in config turns this off.

**Retention.** `privacy.retention_days` (default 365) is how long processed items
stay in the database. Deleting `data/agent.db` deletes everything the agent knows.

## GDPR, in practice

If you are in the EU or handle EU guests' data, the short version:

- **You are the controller.** This software runs on your machine, under your
  control, on your data. TH1 does not receive it.
- **Your model provider is a processor.** If you use the `anthropic` or
  `claude-code` provider, Anthropic processes guest data on your behalf. Check
  their data processing terms and record them in your processing register.
- **Purpose and minimisation.** The agent sees the message and the property facts
  it needs. Do not put staff phone numbers, card data or full guest histories in
  `knowledge/`.
- **Right to erasure.** A guest asking to be deleted means removing their rows
  from `data/agent.db` and any exported CSVs. Ask your Claude session:
  *"Delete every item in data/agent.db whose payload mentions this email address,
  and tell me how many rows you removed."*
- **Retention.** Set `privacy.retention_days` to what your own policy says, not
  to the default.

This is a practical summary, not legal advice.

## Telling guests they are talking to AI

The EU AI Act (Article 50) requires that a person is told when they are
interacting with an AI system, unless it is obvious. Whether it applies to you
depends on where you and your guests are, but it is good practice everywhere and
guests react well to it.

Add a line like this to the signature of any message the agent sends
(`knowledge/signature.md`):

> This reply was prepared with AI assistance and reviewed by our team. Reply to
> this message any time to reach a person directly.

`cp knowledge/signature.example.md knowledge/signature.md` (`workflows/00-setup.md`
step 3), **delete the copy-me comment block**, and edit the line itself - the
whole file is appended, as written, to the end of every outbound email by the
email adapter's own `send()` (`core/adapters/base.py:Email.with_signature`),
the same way for `mock`, `imap` and `gmail` alike, right before the message
leaves the mailbox. It is not baked into the draft you see in the review
queue, only into what is actually sent. No further wiring needed, and `make
doctor`'s "knowledge" line goes green once it (or any other real knowledge
file) exists.

If you run in live mode with auto-send for some intents, say so plainly:

> This reply was written by our AI assistant. If you would rather speak to a
> person, just say so and we will take over.

Keep the escape hatch in the sentence. A guest who wants a human should never
have to work out how to get one.

## Subscription or API: an honest note

Two ways to pay for the reasoning:

**Your Claude Code subscription** (`llm.provider: claude-code` or `interactive`).
Flat monthly cost, no per-message billing. This is genuinely the cheapest way to
run a small hotel's agent.

The caveat, plainly: a personal Pro or Max subscription is intended for
interactive use, and Anthropic's usage policy and rate limits apply to automated
use of it. A handful of scheduled runs a day is a normal way to work. Pointing
a busy inbox at it around the clock is not, and you will hit rate limits at the
worst moment. Read the terms and decide for yourself.

**The Anthropic API** (`llm.provider: anthropic`). Pay per token, no ambiguity
about automated use, proper rate limits, and usage you can attribute. This is
the right answer for production volume. `make report` shows what you are
spending.

Start on the subscription while you are learning what the agent does. Move to the
API when it becomes part of how the hotel runs.

## If something goes wrong

1. `mode: shadow` in `config/hotel.yaml`, or `AGENT_MODE=shadow` in `.env`. Every
   outbound action stops on the next pass.
2. Remove the schedule (`crontab -e`, `launchctl unload`, or
   `systemctl disable --now <slug>.timer`).
3. `make doctor` to see what the agent thinks its state is.
4. `data/logs/*.jsonl` has every decision, with the run id, in order.
