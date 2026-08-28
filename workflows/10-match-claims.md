# Workflow: matching claims

Objective: run one pass over the found-items log and the inbox, and see what
Lost & Found AI did with it.

## Inputs

- The found-items log (`systems.sheets.adapter` - `csv` reads
  `data/imports/found_items.csv`; see `workflows/00-setup.md` step 5).
- A configured `systems.email.adapter` (`mock` by default - see
  `workflows/00-setup.md` step 6 to connect a real mailbox).
- `config/agent.yaml`'s `lost_found` block - `match_threshold` (60),
  `confusable_families` (`eyewear`), `high_value_families` (`jewellery`,
  `documents`) and `sweep_days` (14). The defaults are the spec's own
  numbers; change them once you have watched a few real passes.

## Steps

1. **Run one pass.**
   ```bash
   make run
   make run ARGS="--limit 5"       # just the first five new emails
   make run ARGS="--dry-run"       # compute everything, write nothing
   ```
   This does four things in order: reads the found-items log and logs
   anything new (flagging high-value items for the duty manager the moment
   they are logged, before any claim exists); turns each new guest email into
   a structured claim (the one LLM call in this agent - `prompts/extract_claim.md`);
   scores every open claim against every logged item
   (`tools/engine.py:run_claim_matcher` - pure Python, no model); and expires
   any claim whose 14-day sweep has come due unmatched.

2. **If `llm.provider` is `interactive`,** the run stops with exit code 3 and
   parks a prompt in `data/pending/` for each new email. Read
   `*.prompt.md`, write your answer as JSON to the matching `*.answer.json`
   exactly matching the schema shown, and run the same command again.

3. **See what happened.**
   ```bash
   make review
   ```
   A confident match (score 60 or above) with a drafted return email is
   `pending_review` - or `needs_human` if it touches a high-value family
   (jewellery, documents), which forces the duty-manager gate regardless of
   score. A claim that did not match anything stays open; its rationale
   explains why, and it goes on the 14-day sweep automatically.

   A guest who wrote in a language `hotel.languages` (`config/hotel.yaml`)
   does not list never reaches a claim at all - `python3 tools/review.py show
   <id>` on that item shows `needs_human` with the reason "guest wrote in
   `<lang>`, not in hotel.languages" and the property's default language
   alongside it, so you know to reply by hand. This runs before the one LLM
   call this agent makes, so an out-of-scope language never costs a call.

4. **Work the queue.** `workflows/80-review.md` covers approve / edit /
   reject / send / ship in full.

5. **Keep it running.**
   ```bash
   make watch                       # loop on the configured interval
   ```
   Or schedule it: `make schedule ARGS="--target cron --cadence every-30-min"`
   (or `--target launchd` on a Mac, `--target systemd` on a Linux server) prints a
   snippet with the absolute paths already filled in; paste it where the header
   line says. `scheduler/` holds the same three as static examples.
   `config/agent.yaml`'s `schedule.match_claims` documents the interval this repo
   was built around (every 30 minutes).

## Edge cases

- **No new mail and no new found items.** `make run` prints
  `0 items processed, 0 drafted, 0 sent` and exits 0. Nothing to do.
- **An email that is not actually a lost-item claim** (spam, a booking
  question). The model flags `needs_human: true`, and the item queues
  straight there instead of reaching the matcher.
- **A same-family, different-category near-miss** (reading glasses vs.
  sunglasses - the confusable-family veto). Never proposed as a match, even a
  low-confidence one. The rationale names the one logged item, quotes the
  guest's own words, and explains why. See `docs/how-it-works.md`.
- **A re-run sees the same email or the same found-item row again.**
  `tools/engine.py` skips anything the store has already seen -
  `already_processed()` for email, `INSERT OR IGNORE` keyed on the sheet's
  own `id` for found items.
