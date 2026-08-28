# Workflow: first-run setup

Objective: get Lost & Found AI from a fresh clone to a working demo, then to
real config, in one sitting.

## Steps

1. **Install and check.**
   ```bash
   make setup
   make doctor
   ```
   `make setup` creates the virtualenv, installs `requirements.txt`, and
   copies `.env.example` -> `.env` and every `config/*.example.yaml` ->
   `config/*.yaml` (only if those files do not exist yet - it never overwrites
   your own copies). `make doctor` will show a `FAIL` on "hotel identity"
   right after setup - that is expected, it means the property name is still
   the shipped placeholder ("Hotel Aurora"). Everything else should be `ok`
   or `warn`.

2. **Run the demo.** No credentials needed.
   ```bash
   make demo
   ```
   Expect the found-items log (10 sample items) to be read, 6 sample guest
   emails turned into claims, 3 of them matched with a drafted return email
   (all three happen to be high-value here, so all three need the duty
   manager before they can be approved), one match vetoed on purpose (the
   eyewear trap - see `docs/how-it-works.md`), and the line
   `DEMO OK — 16 items processed, 3 drafted, 0 sent (shadow)`. If you do not
   see that, stop and read `workflows/99-troubleshooting.md` before going
   further.

3. **Fill in the property.** Edit `config/hotel.yaml` (name, address, contact,
   languages). Then:
   ```bash
   cp knowledge/property.example.md              knowledge/property.md
   cp knowledge/faq.example.md                   knowledge/faq.md
   cp knowledge/lost-and-found-policy.example.md  knowledge/lost-and-found-policy.md
   cp knowledge/signature.example.md              knowledge/signature.md
   ```
   Replace the Hotel Aurora content with the real property's facts, and
   delete the `<!-- Copy this to... -->` comment block at the top of each
   file while you are in there - it is only a note to you, not something
   the agent needs (the `extract_claim` prompt strips it automatically if
   you forget, but there is no reason to leave it). The lost-and-found
   policy file matters most for this agent: hold periods, who counts as the
   duty manager, and which categories are high-value beyond the two the
   roster names. See `knowledge/README.md` for how to write it well.
   `knowledge/signature.md` is the AI-disclosure line every outbound email
   ends with - delete the copy-me comment in it and edit the line, and the
   email adapter appends it automatically when it sends (not baked into the
   draft you review) - see `docs/safety.md`.

   **If the property has a category the built-in 12 do not cover** (a
   beach-front hotel's "beach towels", say), listing it in
   `lost-and-found-policy.md` is not enough on its own - the matcher only
   scores a category it has keywords for. Add it to `config/agent.yaml`'s
   `lost_found.extra_categories` too (see the example in
   `knowledge/lost-and-found-policy.example.md`'s "Categories we log"
   section); `make doctor` reports how many custom categories are
   configured.

4. **Pick how the agent thinks.** `config/hotel.yaml`'s `llm.provider` starts
   as `interactive` - it asks you, in this Claude Code session, instead of
   calling a model. That costs nothing extra and is the best way to see how
   the one LLM call in this agent (turning a guest email into a structured
   claim) actually works. `docs/how-it-works.md` and `docs/safety.md` explain
   the other three providers (`mock`, `claude-code`, `anthropic`).

5. **Decide where the found-items log lives.** `systems.sheets.adapter` in
   `config/hotel.yaml` starts as `csv`, which reads
   `data/imports/found_items.csv` - a file housekeeping (or whoever logs
   found items) can maintain and re-export. `docs/integrations.md` covers the
   `google` option for a live shared spreadsheet. Run `make doctor` after
   changing it; it tells you whether the file or the sheet is actually there.

6. **Connect a real mailbox (optional for now).** `systems.email.adapter`
   starts as `mock`, which only ever sees the 6 sample claim emails.
   `docs/integrations.md` covers `imap` (works with any provider) and
   `gmail`. Run `make doctor` after changing it.

7. **Re-check.**
   ```bash
   make doctor
   ```
   Once the property name is real and `knowledge/property.md` and
   `knowledge/lost-and-found-policy.md` exist, the "hotel identity" and
   "knowledge" lines turn green. Move on to `workflows/10-match-claims.md` to
   run the loop for real.
