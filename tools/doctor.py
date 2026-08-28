#!/usr/bin/env python3
"""tools/doctor.py - is Lost & Found AI configured and reachable right now?

    make doctor
    python3 tools/doctor.py

Runs the generic core.doctor checks (python, deps, config, .env, hotel
identity, mode, llm provider, every adapter, the store, knowledge) plus three
checks specific to this agent: the lost_found config block (thresholds and
high-value families), the found-items intake source, and the extract_claim
prompt + schema. Exits 0 when everything passed, 1 when a FAIL line needs
fixing. Never a traceback: a config error is shown as a FAIL row like any
other.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import ConfigError, Settings, load_settings  # noqa: E402
from core.doctor import Check, FAIL, PASS, WARN, print_table, run_checks  # noqa: E402
from engine import load_found_items_rows  # noqa: E402


def check_lost_found_config(settings: Settings) -> Check:
    threshold = settings.agent_get("lost_found.match_threshold")
    high_value = settings.agent_get("lost_found.high_value_families") or []
    confusable = settings.agent_get("lost_found.confusable_families") or []
    extra_categories = settings.agent_get("lost_found.extra_categories") or []
    if threshold is None:
        return Check("lost_found config", FAIL, "no lost_found block in config/agent.yaml",
                     "Copy config/agent.example.yaml to config/agent.yaml.")
    if not high_value:
        return Check("lost_found config", WARN,
                     "high_value_families is empty - jewellery and documents will "
                     "never route to the duty manager",
                     "Set lost_found.high_value_families in config/agent.yaml.")
    extra_note = (f", {len(extra_categories)} custom categor"
                 f"{'y' if len(extra_categories) == 1 else 'ies'}" if extra_categories else "")
    return Check("lost_found config", PASS,
                 f"threshold {threshold}, high-value: {', '.join(high_value)}, "
                 f"confusable: {', '.join(confusable) or 'none'}{extra_note}")


def check_intake_source(settings: Settings) -> Check:
    """Calls the SAME loader `tools/run.py` uses (`engine.load_found_items_rows`)
    instead of stat()-ing a path by hand, so this cannot show green for a
    file the real pipeline never reads (SIMULATION.md finding 3)."""
    source = settings.agent_get("lost_found.intake.source", "sheet")
    if source == "fixtures":
        return Check("found-items intake", WARN,
                     "source is 'fixtures' - that is what `make demo` forces for itself",
                     "Set lost_found.intake.source to 'sheet' in config/agent.yaml for "
                     "real use.")
    sheet = settings.agent_get("lost_found.intake.found_items_sheet", "found_items")
    adapter = settings.systems.sheets.adapter
    path_hint = (f"data/imports/{sheet}.csv" if adapter in ("csv", "mock")
                else f"the '{sheet}' tab of your Google Sheet (systems.sheets.spreadsheet_id)")
    try:
        rows = load_found_items_rows(settings)
    except Exception as exc:  # noqa: BLE001 - doctor never crashes, it reports
        return Check("found-items intake", FAIL, f"reading via {adapter} failed: {exc}",
                     f"Fix systems.sheets in config/hotel.yaml, or export to {path_hint}.")
    if not rows:
        return Check("found-items intake", WARN,
                     f"reads via systems.sheets.adapter={adapter}, but found 0 rows",
                     f"Export your found-items log to {path_hint}.")
    return Check("found-items intake", PASS,
                 f"{len(rows)} row(s) via systems.sheets.adapter={adapter} ({path_hint})")


def check_prompts() -> Check:
    missing = [p for p in ("prompts/extract_claim.md", "prompts/schemas/extract-claim.json")
              if not (REPO_ROOT / p).is_file()]
    if missing:
        return Check("prompts", FAIL, f"missing {', '.join(missing)}",
                     "These ship with the repo - restore them from git.")
    return Check("prompts", PASS, "extract_claim.md + schema present")


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        checks = run_checks(None) + [Check("config", FAIL, str(exc),
                                           "Fix config/hotel.yaml or config/agent.yaml.")]
        return print_table(checks, title="Lost & Found AI - doctor")

    checks = run_checks(settings, extra=[check_lost_found_config, check_intake_source])
    checks.append(check_prompts())
    return print_table(checks, title="Lost & Found AI - doctor")


if __name__ == "__main__":
    raise SystemExit(main())
