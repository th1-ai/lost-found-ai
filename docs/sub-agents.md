# Sub-agents in this repo

None.

The roster lists no children for Lost & Found AI, and the Email Optimizer /
Coach layer does not apply to it either. This repo is one self-contained
loop: `tools/run.py` does everything described in `docs/how-it-works.md`,
and `tools/review.py` is the only human touchpoint.

If a hotel wants to extend this agent - a housekeeping-app integration for
found-item intake instead of a CSV/sheet, a real courier API instead of the
simulated tracking number, a manual override for a below-threshold match -
those are changes to `tools/engine.py` and the adapters in `core/adapters/`,
not new sub-agents. `docs/how-it-works.md`'s "Design decisions" section and
`docs/integrations.md`'s "Implement your own" recipe are the places to start.
