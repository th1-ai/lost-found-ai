# Workflow: troubleshooting

Read the whole error before doing anything - every tool here is written to
say what broke and what to do about it. If you fix something not covered
below, add it here.

## `make doctor` shows a FAIL

Each `FAIL` line has a `->` fix hint right under it. Common ones:

- **`hotel identity`: name is still 'Hotel Aurora'.** Expected on a fresh
  clone. Edit `config/hotel.yaml`.
- **`llm provider`: claude-code selected but `claude` is not on PATH.**
  Install Claude Code, or switch `llm.provider` to `interactive` or
  `anthropic` in `config/hotel.yaml`.
- **`llm provider`: ANTHROPIC_API_KEY is not set.** Add it to `.env`, or
  switch `llm.provider` to `claude-code` or `interactive`.
- **`lost_found config`: no lost_found block in config/agent.yaml.** Copy
  `config/agent.example.yaml` to `config/agent.yaml` - `make setup` does this
  for you.
- **An adapter shows FAIL, not warn.** `universal`/`built` adapters fail loud
  when misconfigured (a `warn` is reserved for stubs). Read the `detail`
  column - it names the missing file or variable.

## `make demo` does not print `DEMO OK`

- Make sure `make setup` ran first (`.venv` must exist).
- `tools/demo.py` forces `llm.provider=mock`, `mode=shadow` and
  `lost_found.intake.source=fixtures`, and reads `fixtures/hotel/found_items.csv`
  and `fixtures/inbound/*.json` - if you deleted or renamed those files,
  restore them from git.
- Read the traceback if there is one; `tools/demo.py` does not swallow errors
  on purpose, so a fixture problem shows up immediately.

## `make run` exits with code 3

Not an error. `llm.provider: interactive` parked a prompt. Read
`data/pending/*.prompt.md`, write your answer to the matching
`*.answer.json` (JSON only, matching the schema shown, no prose, no code
fence), and run the same command again.

**Checking `$?` after `make run` will not show a 3.** GNU Make always exits
`2` itself when a recipe fails, whatever the recipe's own exit code was -
that is Make's behaviour, not a bug here. Look at what was printed (a parked
prompt looks nothing like an error) rather than the shell exit code, or run
`python3 tools/run.py --once` directly to see the real `3`.

## `tools/review.py approve` or `edit` refuses with "is high-value"

Working as intended - the roster's "cant" promise. Get the duty manager to
actually look at the claim and the matched item first
(`python3 tools/review.py show <id>`), then re-run with
`--duty-manager-ack "<their name>"`. If the match itself looks wrong, `reject`
it instead - do not force an ack past a bad match.

## `tools/review.py ship` refuses with "is not 'sent' yet"

The return email has to actually go out before the item can ship - approve
and `send` it first (`workflows/80-review.md`). This is what stops a claim
being shipped before the guest has even been told it was found.

## An item is stuck at `sending`

A process died between claiming an item and finishing the send.
`tools/run.py` calls `core.store.Store.reap_stuck_sending()` on every pass,
which moves anything stuck for more than 30 minutes to `failed` so you see it
in the queue instead of it vanishing. Use
`python3 tools/review.py retry <id>` once the cause is fixed.

## The found-items log is not showing up

- `systems.sheets.adapter: csv` reads `data/imports/found_items.csv` -
  confirm the file exists and has an `id` and an `item` column for every row
  (`make doctor` says so under "found-items intake").
- A row with a blank `id` or `item` is skipped on purpose - fill both in and
  re-export.
- Re-reading the same row twice never duplicates it (`lf_items` is keyed on
  the sheet's own `id`).

## The matcher gets a claim wrong, or is too cautious

Fix it in the review queue first (`edit`, not `reject`, so the correction is
on record), then look at whether `config/agent.yaml`'s `lost_found.match_threshold`
needs adjusting, or whether `knowledge/lost-and-found-policy.md` is missing a
category the property actually logs. The scoring rules themselves live in
`tools/engine.py` and are plain Python - read `docs/how-it-works.md` before
changing them, since the confusable-family veto and the high-value gate are
deliberate safety features, not bugs.

## `python3 tools/*.py` says `ModuleNotFoundError: No module named 'core'`

You ran it with a Python that is not the repo's virtualenv, or from outside
the repo root. Use `make run` / `make doctor` / etc. (they call
`.venv/bin/python` for you), or run `.venv/bin/python tools/run.py` directly
from the repo root.

## Still stuck

`data/logs/*.jsonl` has every decision the agent made, in order, with a run
id. `python3 tools/review.py show <id>` has the full event trail for one item.
If neither explains it, that is a real bug - describe exactly what you ran
and what you expected, and ask.
