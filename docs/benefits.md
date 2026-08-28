# The business case

## Why this exists

**"Lost & found is a daily trickle of emails and calls nobody owns, and slow
handling sours an otherwise great stay."** That is the roster's own framing,
and it is the whole reason this agent exists. A found bracelet sits in a
drawer at reception until someone remembers to log it. A guest's email asking
"did anyone hand in my sunglasses?" waits behind check-in questions and
reservation calls, because nobody owns replying to it. By the time someone
gets to it, the guest has often already given up and left a so-so review
about an otherwise good stay - not because of the room or the food, but
because the last thing that happened was silence about a lost bracelet.

## What it claims, and what that means day to day

**Does.** Logs found items with photos, matches inbound guest claims to the
log, handles the guest conversation, arranges paid return shipping with a
courier, and closes the case with tracking. In practice: a housekeeper or a
front-desk agent logs an item once (10 seconds, one row in a spreadsheet);
every guest email asking about a lost item gets read, matched, and answered
without anyone hunting through old messages or the lost-property drawer.

**Won't.** High-value items (passports, jewellery) route to the duty manager
before anything ships. This is not a limitation to apologise for - it is the
one thing this agent is built to get right on purpose. See
`docs/safety.md`.

**Output.** Every found item logged and every claim matched, answered, and
shipped without staff chasing couriers. The measurable version of that:
every logged item has a status (`logged` / `matched` / `returned`); every
claim has an outcome (`matched`, `open` with a written reason, `shipped`, or
`expired` after the 14-day sweep) - never a claim that just goes quiet.

**ROI.** -90% lost & found admin time (labor). This is the roster's number,
not a claim this repo re-derives - it holds only if the property actually
runs the found-items log and the review queue through this agent instead of
a spreadsheet and an inbox folder.

## What to measure

`make report` (`tools/report.py`) reads `data/agent.db` and prints:

- **Volume.** How many items are logged, how many claims come in, and how
  many of each outcome (matched / still open / shipped / expired).
  A rising "still open" count with no matches is a sign the found-items log
  is not being kept up to date, not that the matcher is broken - check
  `make doctor`'s "found-items intake" line.
- **Match rate.** The percentage of claims that matched a logged item. This
  is the number that maps most directly to the ROI claim: every claim that
  matches automatically is one nobody had to search the lost-property room
  for by hand.
- **High-value volume.** How many shipments needed, and got, a duty-manager
  sign-off, and how many are currently waiting for one. This is the number
  to show anyone asking "are we actually catching the risky ones" - it
  should never silently drop to zero while jewellery keeps being logged.
- **Speed.** Average days from a claim arriving to a return email being
  drafted (usually well under a day, since matching runs on every pass) -
  the thing that actually prevents the "slow handling" the roster's `why`
  describes.
- **Edit rate.** Of the drafts a human approved, how many were edited first.
  A high rate is not a failure - it means the review queue is doing its job.
  A rate that never moves once the property's own vocabulary is in
  `knowledge/lost-and-found-policy.md` is worth a look.
- **LLM spend.** Calls, tokens, and cost for the one model call this agent
  makes (`extract_claim` - turning a guest's free text into structured
  fields). Everything else - the matching, the scoring, the veto, the
  high-value gate - is plain Python and costs nothing per run.

## Honest caveats

- **The -90% figure is the roster's claim, not something this repo measures
  for you.** `make report` gives you the raw numbers (volume, match rate,
  time saved per match); turning that into a labor-hours estimate for one
  specific property is a conversation with the hotel, not a formula baked
  into the code.
- **The match rate depends entirely on the found-items log being kept
  current.** An agent that matches 95% of claims against a log nobody
  updates is matching against stale data, not doing its job well. This is
  a staff habit this agent supports, not one it can replace.
- **Courier and tracking are simulated.** `tools/engine.py:ship_claim`
  generates its own tracking id; no real shipping label is purchased and no
  real carrier is called. The time saved is real (drafting, matching, and
  the review queue), but "arranges paid return shipping" is not yet backed
  by a real integration - see `docs/integrations.md`.
- **A confident match is not a guaranteed one.** The matcher is deliberately
  conservative (see `docs/how-it-works.md`) - it under-matches on purpose
  rather than over-matches, which means some real matches will need a human
  to notice and act on manually the first few times a property is live.
