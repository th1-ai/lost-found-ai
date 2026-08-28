"""tools/engine.py - Lost & Found AI's matching logic.

Deterministic decisioning, LLM for language: the ONE model call in this agent
is `extract_claim` (turn a guest's free-text email into structured fields).
Everything below that - category detection, scoring, the confusable-family
veto, the high-value gate, the return-email template - is pure Python with no
model in the loop. See docs/how-it-works.md.

Ported from the demo's `runClaimMatcher` (housekeeping-engine.ts) with two
additions the spec flags as unbuilt there: a `documents` category and the
high-value duty-manager gate (roster "cant": "High-value items (passports,
jewellery) route to the duty manager before anything ships").

Shared by tools/run.py (the real loop), tools/review.py (approve/ship) and
tools/demo.py, so all three exercise exactly the same code path.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.adapters import get_sheets
from core.config import Settings, repo_root, sub_data_dir
from core.i18n import strip_accents
from core.llm import LLMResult, LLMSchemaError, complete
from core.store import Item, Store
from core.templates import build_prompt

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "prompts" / "schemas"
FIXTURES_HOTEL_DIR = repo_root() / "fixtures" / "hotel"


def _schema(name: str) -> dict:
    return json.loads((SCHEMAS_DIR / f"{name}.json").read_text(encoding="utf-8"))


EXTRACT_CLAIM_SCHEMA = _schema("extract-claim")

# --------------------------------------------------------------------------
# category table - order matters (charger must beat watch on "Garmin watch
# charger"), exactly as in the source engine.
#
# Each pattern is a union of the English keywords (the original source
# engine) plus es/fr/de/it/pt equivalents - the five other languages
# `prompts/extract_claim.md` asks the model to read (see
# `config/hotel.yaml`'s `hotel.languages` and `core/i18n.py`'s
# `STOPWORDS`/`LANGUAGES`). A guest who writes in their own listed language
# must classify exactly like one who writes in English (SIMULATION.md round
# 2, new finding A: a Portuguese "pulseira de prata" scored 0% against a
# near-identical English-described item because `category_of()` returned
# `None`, forcing `score_pair` to 0 with no distinguishing error - it looked
# exactly like an honest "nothing similar in the log").
#
# Keywords are written WITHOUT accents: `normalise()` (below) accent-folds
# both the claim/item text and any hotel-configured `extra_categories`
# keywords before matching (`core.i18n.strip_accents`), so an accented
# keyword here would simply never match. Short, collision-prone words are
# `\b`-bounded the same way the original English list bounds `ring`/`belt`/
# `watch`/`cable`/`id card` - e.g. Portuguese `anel` (ring) is bounded
# because it is a substring of unrelated words like `painel` (panel).
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Category:
    id: str
    family: str
    label: str
    phrase: str
    pattern: "re.Pattern[str]"


def _pat(*alternatives: str) -> "re.Pattern[str]":
    """Compile a case-insensitive union of keyword alternatives. Every
    alternative must be accent-folded - see the module note above."""
    return re.compile("|".join(alternatives), re.I)


CATEGORIES: list[Category] = [
    Category("plush", "toy", "soft toy", "a child's soft toy", _pat(
        r"rabbit", r"bunny", r"plush", r"teddy", r"soft toy", r"cuddly",             # en
        r"peluche", r"conejo", r"osito",                                            # es
        r"lapin", r"doudou", r"nounours",                                           # fr (peluche shared)
        r"plueschtier", r"kuscheltier", r"teddybar", r"hase", r"kaninchen",         # de
        r"coniglio", r"orsacchiotto",                                               # it (peluche shared)
        r"pelucia", r"coelho", r"ursinho",                                          # pt
    )),
    Category("earphones", "audio", "earphones", "the same earphones", _pat(
        r"airpods", r"ear ?bud", r"ear ?phone", r"head ?phone",                     # en
        r"auriculares", r"audifonos",                                               # es (auriculares shared w/ pt)
        r"ecouteurs", r"casque audio",                                              # fr
        r"kopfhorer", r"ohrhorer",                                                  # de
        r"auricolari", r"cuffie",                                                   # it
        r"fone de ouvido", r"fones de ouvido",                                      # pt
    )),
    Category("jewellery", "jewellery", "jewellery", "the same piece of jewellery", _pat(
        r"bracelet", r"charm", r"necklace", r"\bring\b", r"earring",                # en
        r"pulsera", r"collar", r"anillo", r"pendiente", r"arete",                   # es
        r"collier", r"bague", r"boucle d'oreille",                                  # fr
        r"armband", r"halskette", r"kette", r"ohrring",                             # de
        r"braccialetto", r"collana", r"anello", r"orecchino",                       # it
        r"pulseira", r"colar", r"\banel\b", r"brinco",                              # pt
    )),
    Category("sunglasses", "eyewear", "sunglasses", "a pair of sunglasses", _pat(
        r"sun ?glass",                                                              # en
        r"gafas de sol", r"lentes de sol",                                          # es
        r"lunettes de soleil",                                                      # fr
        r"sonnenbrille",                                                            # de
        r"occhiali da sole",                                                        # it
        r"oculos de sol",                                                           # pt
    )),
    Category("readers", "eyewear", "reading glasses", "a pair of reading glasses", _pat(
        r"reading glasses", r"spectacles", r"\breaders\b", r"eyeglasses",           # en
        r"gafas de lectura", r"gafas graduadas",                                    # es
        r"lunettes de lecture",                                                     # fr
        r"lesebrille",                                                              # de
        r"occhiali da lettura",                                                     # it
        r"oculos de leitura", r"oculos de grau",                                    # pt
    )),
    Category("documents", "documents", "an identity document",
             "the same identity document", _pat(
        r"passport", r"driver'?s licen[cs]e", r"\bid card\b", r"identity card",     # en
        r"pasaporte", r"carnet de identidad", r"carne de identidad",                # es
        r"licencia de conducir",
        r"passeport", r"carte d'identite", r"permis de conduire",                   # fr
        r"reisepass", r"personalausweis", r"fuehrerschein",                         # de
        r"passaporto", r"carta d'identita", r"patente",                             # it
        r"passaporte", r"carteira de identidade", r"cartao de identidade",          # pt
        r"carta de conducao",
    )),
    Category("charger", "electronics", "charger", "a charger", _pat(
        r"charger", r"charging cable", r"\bcable\b",                                # en
        r"cargador",                                                                # es
        r"chargeur",                                                                # fr
        r"ladegeraet", r"ladekabel", r"\bkabel\b",                                  # de
        r"caricabatterie", r"\bcavo\b",                                             # it
        r"carregador", r"\bcabo\b",                                                 # pt
    )),
    Category("ereader", "electronics", "e-reader", "an e-reader", _pat(
        r"kindle", r"e-?reader", r"paperwhite",                                     # en (brand names, universal)
        r"lector electronico",                                                      # es
        r"liseuse",                                                                 # fr
        r"e-book-reader",                                                           # de
        r"leitor de ebooks", r"leitor de e-books",                                  # pt
    )),
    Category("fleece", "clothing", "fleece", "a fleece", _pat(
        r"fleece", r"hoodie", r"jumper", r"sweatshirt",                             # en
        r"forro polar", r"sudadera",                                                # es
        r"polaire", r"sweat",                                                       # fr
        r"fleecejacke", r"pullover",                                                # de
        r"\bpile\b", r"felpa",                                                      # it
        r"casaco polar", r"moletom",                                                # pt
    )),
    Category("jacket", "clothing", "jacket", "a jacket", _pat(
        r"blazer", r"jacket", r"\bcoat\b",                                          # en
        r"chaqueta", r"abrigo",                                                     # es
        r"veste", r"manteau",                                                       # fr
        r"\bjacke\b", r"\bmantel\b",                                                # de
        r"giacca", r"cappotto",                                                     # it
        r"casaco", r"jaqueta",                                                      # pt
    )),
    Category("belt", "clothing", "belt", "a belt", _pat(
        r"\bbelt\b",                                                                # en
        r"cinturon",                                                                # es
        r"ceinture",                                                                # fr
        r"guertel",                                                                 # de
        r"cintura",                                                                 # it (pt "cintura" means waist, not belt)
        r"\bcinto\b",                                                               # pt
    )),
    Category("watch", "electronics", "watch", "a watch", _pat(
        r"\bwatch\b",                                                               # en
        r"\breloj\b",                                                               # es
        r"\bmontre\b",                                                              # fr
        r"uhr",                                                                     # de (matches "Armbanduhr" too)
        r"orologio",                                                                # it
        r"relogio",                                                                 # pt
    )),
]

#: families where two different categories read alike but are never the same
#: object. Overridable via agent.yaml lost_found.confusable_families.
DEFAULT_CONFUSABLE_FAMILIES = frozenset({"eyewear"})
#: categories that always route to the duty manager before anything ships.
DEFAULT_HIGH_VALUE_FAMILIES = frozenset({"jewellery", "documents"})
DEFAULT_MATCH_THRESHOLD = 60

ATTRIBUTES = [
    "silver", "gold", "grey", "navy", "black", "red", "teal", "tortoiseshell",
    "leather", "linen", "plush", "anchor", "charm", "engraved", "restitched",
    "ear", "pro", "case", "burgundy", "hoop", "brown",
]

#: where things turn up at this (fictional) property - see fixtures/hotel and
#: knowledge/property.example.md. Customise for your own hotel's spaces.
PLACES = ["bar", "lobby", "reception", "breakfast room", "courtyard",
          "laundry room", "lift", "room"]


def normalise(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"re-?sewn|re-?stitched|restitched", "restitched", text)
    text = text.replace("gray", "grey")
    # Accent-fold ("pulseira" / "pulsèira", "oculos" / "óculos" both match the
    # same keyword) - every CATEGORIES/extra_categories pattern is written
    # without accents to match, so this has to run before category_of() ever
    # sees the text. See the CATEGORIES module note.
    text = strip_accents(text)
    return re.sub(r"\s+", " ", text)


def with_english_gloss(description: str, description_en: str) -> str:
    """Append ``extract_claim``'s optional English gloss to the guest's own
    description, e.g. ``"pulseira de prata" -> "pulseira de prata (silver
    bracelet)"``.

    Convenience only, never load-bearing: the matcher classifies
    ``description`` in the guest's own language via CATEGORIES' es/fr/de/it/pt
    keyword sets regardless (see the module note above `CATEGORIES`), so this
    is a no-op when the model leaves `description_en` blank (mock/interactive
    fixtures that predate the field, or a description already in English) -
    every existing behaviour keeps working with no gloss appended. It helps
    two things beyond the matcher: an English-speaking duty manager reading
    `tools/review.py show` can recognise the item without knowing the guest's
    language, and it gives the scorer's English-only ATTRIBUTES/PLACES word
    lists a chance to pick up extra evidence points that pure category
    matching alone does not award. It never reaches the guest - the drafted
    return email (`draft_return_email`) quotes the logged item's own text,
    not the claim's.
    """
    description = (description or "").strip()
    gloss = (description_en or "").strip()
    if not gloss or gloss.casefold() == description.casefold():
        return description
    return f"{description} ({gloss})"


def has(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text, re.I) is not None


def category_of(text: str, categories: list[Category] | None = None) -> Category | None:
    for cat in (categories if categories is not None else CATEGORIES):
        if cat.pattern.search(text):
            return cat
    return None


def build_categories(settings: Settings | None) -> list[Category]:
    """``CATEGORIES`` plus any hotel-specific ones from
    ``config/agent.yaml``'s ``lost_found.extra_categories``.

    Listing a category in ``knowledge/lost-and-found-policy.md`` only changes
    what the agent *talks about* (the extract_claim prompt, the return-email
    template) - the MATCHER only ever recognises the 12 categories hardcoded
    below, plus whatever is registered here. This is the fix for
    SIMULATION.md finding 6 ("beach towels" scored 0% even though the
    knowledge file listed it): a category with real keywords in
    ``extra_categories`` is added to the same table `score_pair` and
    `intake_found_items` use, no code edit required.

    Keywords are accent-folded the same way the built-in CATEGORIES are
    (``core.i18n.strip_accents``) - a hotel that types an accented keyword
    (e.g. "toalha de praia") still matches accent-folded guest text, and a
    keyword typed without the accent still matches text that has one.
    """
    if settings is None:
        return CATEGORIES
    extra_raw = settings.agent_get("lost_found.extra_categories", []) or []
    if not extra_raw:
        return CATEGORIES
    extra: list[Category] = []
    for row in extra_raw:
        if not isinstance(row, dict):
            continue
        cat_id = str(row.get("id") or "").strip()
        keywords = row.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [keywords]
        keywords = [strip_accents(str(k).strip()) for k in keywords if str(k).strip()]
        if not cat_id or not keywords:
            continue
        label = str(row.get("label") or cat_id).strip()
        pattern = re.compile("|".join(re.escape(k) for k in keywords), re.I)
        extra.append(Category(id=cat_id, family=str(row.get("family") or cat_id).strip(),
                              label=label, phrase=str(row.get("phrase") or f"the same {label}"),
                              pattern=pattern))
    return CATEGORIES + extra


def initials_of(name: str) -> str:
    parts = (name or "").strip().split()
    if len(parts) < 2:
        return ""
    return f"{parts[0][0]}{parts[-1][0]}".upper()


# --------------------------------------------------------------------------
# data model
# --------------------------------------------------------------------------
@dataclass
class FoundItem:
    """One row of ``lf_items`` - a logged found item."""

    id: str
    item: str
    description: str = ""
    found_where: str = ""
    found_days_ago: int = 0
    photo_url: str = ""
    reported_by: str = ""
    status: str = "logged"          # logged | matched | returned
    claim_id: str | None = None

    @property
    def text(self) -> str:
        return normalise(f"{self.item} {self.description} {self.found_where}")


@dataclass
class Claim:
    """One row of ``lf_claims`` - an inbound guest claim."""

    id: str
    guest_name: str
    contact: str
    description: str = ""
    stay_note: str = ""
    status: str = "open"            # open | matched | shipped | expired
    matched_item_id: str | None = None
    confidence: float = 0.0
    rationale: str = ""
    high_value: bool = False
    tracking_number: str | None = None
    source_email_id: str | None = None

    @property
    def text(self) -> str:
        return normalise(f"{self.description} {self.stay_note}")


@dataclass
class ScoredPair:
    claim: Claim
    item: FoundItem
    score: int = 0
    evidence: list[str] = field(default_factory=list)
    veto: str | None = None


@dataclass
class ClaimMatch:
    claim_id: str
    guest_name: str
    item_id: str | None
    item_label: str | None
    confidence: int
    rationale: str
    evidence: list[str]
    draft_email: dict | None
    high_value: bool = False


@dataclass
class MatchResult:
    steps: list[dict]
    matches: list[ClaimMatch]
    summary: dict


def is_high_value(cat: Category | None, high_value_families: frozenset[str]) -> bool:
    return bool(cat) and cat.family in high_value_families


# --------------------------------------------------------------------------
# scoring - the deliberately conservative, evidence-scored matcher
# --------------------------------------------------------------------------
_CASE_COLOUR = re.compile(
    r"\b(red|blue|black|brown|green|grey|navy|tan|tortoiseshell)\b[^.]{0,14}\bcase\b", re.I)
_ENGRAVED = re.compile(r"[\"“']\s*([A-Z])\.?\s*([A-Z])\.?\s*[\"”']")
_DAYS_AGO = re.compile(r"(\d+)\s+days? ago")


def score_pair(claim: Claim, item: FoundItem,
               confusable_families: frozenset[str] = DEFAULT_CONFUSABLE_FAMILIES,
               categories: list[Category] | None = None) -> ScoredPair:
    """Score one claim against one item. Pure function - no I/O, no model."""
    c_text, i_text = claim.text, item.text
    c_cat, i_cat = category_of(c_text, categories), category_of(i_text, categories)
    evidence: list[str] = []

    if not c_cat or not i_cat:
        return ScoredPair(claim, item, 0, evidence, None)

    if c_cat.id == i_cat.id:
        score = 45
        evidence.append(f"Both records describe {c_cat.phrase}")
    elif c_cat.family == i_cat.family and c_cat.family in confusable_families:
        # Same family, different thing - the trap. Never match these, say why.
        case_hit = _CASE_COLOUR.search(claim.description)
        case_detail = f"{case_hit.group(1).lower()} case" if case_hit else None
        veto = (
            f'The only {c_cat.family} handed in is "{item.item}" from '
            f"{item.found_where.lower()}. The guest describes {c_cat.label}"
            f"{f' in a {case_detail}' if case_detail else ''} — {i_cat.label} are a "
            f"different thing entirely, and no {case_detail or 'matching case'} has "
            f'been logged anywhere. Posting the wrong pair is worse than an honest '
            f'"not yet"'
        )
        return ScoredPair(claim, item, 0, evidence, veto)
    else:
        return ScoredPair(claim, item, 0, evidence, None)

    # Distinguishing attributes - colour, material, shape words shared by both.
    attr_points, shared = 0, []
    for word in ATTRIBUTES:
        if attr_points >= 45:
            break
        if has(c_text, word) and has(i_text, word):
            attr_points += 15
            shared.append(word)
    if attr_points:
        score += attr_points
        evidence.append(f"Matching detail: {', '.join(shared)}")

    # Engraved initials against the claimant's own name.
    eng = _ENGRAVED.search(item.item)
    if eng and re.search(r"engrav|initial", claim.description, re.I):
        stamped = f"{eng.group(1)}{eng.group(2)}".upper()
        if stamped == initials_of(claim.guest_name):
            score += 25
            evidence.append(f'Engraved "{eng.group(1)}.{eng.group(2)}." matches {claim.guest_name}')

    # Where it turned up.
    place = next((p for p in PLACES if has(c_text, p) and has(i_text, p)), None)
    if place:
        score += 10
        evidence.append(f"Found {item.found_where.lower()} — the guest points at the same place")

    # When it turned up.
    if "yesterday" in c_text and 1 <= item.found_days_ago <= 2:
        score += 10
        evidence.append(
            f"Logged {'yesterday' if item.found_days_ago == 1 else 'two days ago'}, "
            "matching the checkout date")
    elif "weekend" in c_text and 2 <= item.found_days_ago <= 3:
        score += 8
        evidence.append(f"Logged {item.found_days_ago} days ago — the weekend the guest was on the property")
    else:
        m = _DAYS_AGO.search(c_text)
        if m and abs(item.found_days_ago - int(m.group(1))) <= 1:
            score += 8
            evidence.append(f"Logged {item.found_days_ago} days ago, in line with the stay")

    return ScoredPair(claim, item, score, evidence, None)


def run_claim_matcher(claims: list[Claim], items: list[FoundItem], *,
                      threshold: int = DEFAULT_MATCH_THRESHOLD,
                      confusable_families: frozenset[str] = DEFAULT_CONFUSABLE_FAMILIES,
                      high_value_families: frozenset[str] = DEFAULT_HIGH_VALUE_FAMILIES,
                      settings: Settings | None = None) -> MatchResult:
    """Score every open claim against every available item, assign greedily.

    Mirrors the source engine's ``runClaimMatcher``: sort all scored pairs
    descending, assign one-to-one (each item and each claim used at most
    once), never propose anything below ``threshold``. Every claim that does
    not get a confident match still gets a reason, never a silent blank.
    """
    categories = build_categories(settings)
    open_claims = [c for c in claims if c.status not in ("shipped", "expired")]
    pool = [i for i in items if i.status != "returned"]

    pairs: list[ScoredPair] = []
    vetoes: list[ScoredPair] = []
    for claim in open_claims:
        for item in pool:
            scored = score_pair(claim, item, confusable_families, categories)
            if scored.veto:
                vetoes.append(scored)
            if scored.score > 0:
                pairs.append(scored)
    pairs.sort(key=lambda p: (-p.score, p.claim.id))

    taken_items: set[str] = set()
    taken_claims: set[str] = set()
    matches: list[ClaimMatch] = []

    for pair in pairs:
        if pair.score < threshold:
            continue
        if pair.item.id in taken_items or pair.claim.id in taken_claims:
            continue
        taken_items.add(pair.item.id)
        taken_claims.add(pair.claim.id)
        confidence = min(96, round(pair.score))
        high_value = (is_high_value(category_of(pair.claim.text, categories), high_value_families)
                     or is_high_value(category_of(pair.item.text, categories), high_value_families))
        draft = draft_return_email(pair.claim, pair.item, settings=settings)
        matches.append(ClaimMatch(
            claim_id=pair.claim.id, guest_name=pair.claim.guest_name,
            item_id=pair.item.id, item_label=f"{pair.item.item}",
            confidence=confidence,
            rationale=f"{'. '.join(pair.evidence)}. Confidence {confidence}%.",
            evidence=pair.evidence, draft_email=draft, high_value=high_value))

    for claim in open_claims:
        if claim.id in taken_claims:
            continue
        veto = next((v for v in vetoes if v.claim.id == claim.id), None)
        c_cat = category_of(claim.text, categories)
        if veto:
            rationale = (f"{veto.veto}. Claim stays open and goes on the 14-day sweep "
                        "— if the right pair is handed in, this claim is re-scored "
                        "automatically.")
        else:
            same_family = [i for i in pool
                           if (ic := category_of(i.text, categories)) and c_cat
                           and ic.family == c_cat.family]
            label = c_cat.label if c_cat else "item of this kind"
            if same_family:
                names = " and ".join(f'"{i.item}"' for i in same_family[:2])
                verb = "is" if len(same_family[:2]) == 1 else "are"
                rationale = (f"Nothing similar in the log — no {label} has been handed in. "
                            f"The only {c_cat.family if c_cat else 'related items'} logged "
                            f"this week {verb} {names}, and neither is what the guest "
                            "describes. Claim stays open for the 14-day sweep instead of "
                            "a hopeful guess.")
            else:
                rationale = (f"Nothing similar in the log: no {label} has been handed in "
                            "from any outlet. Claim stays open for the 14-day sweep, and "
                            "housekeeping has the description on the floor sheet.")
        high_value = is_high_value(c_cat, high_value_families)
        matches.append(ClaimMatch(claim_id=claim.id, guest_name=claim.guest_name,
                                  item_id=None, item_label=None, confidence=0,
                                  rationale=rationale, evidence=[], draft_email=None,
                                  high_value=high_value))

    matches.sort(key=lambda m: m.claim_id)
    matched = sum(1 for m in matches if m.item_id)

    steps = [{
        "title": f"Reading {len(open_claims)} open claim(s) against {len(pool)} logged items",
        "detail": f"{len(open_claims) * len(pool)} pairs scored on item class, "
                 "distinguishing detail, place and date — no free-text guessing",
    }]
    confident = [m for m in matches if m.item_id][:3]
    if confident:
        steps.append({
            "title": "Scoring the distinguishing details, not just the noun",
            "detail": " · ".join(f"{m.claim_id}: {m.evidence[1] if len(m.evidence) > 1 else m.evidence[0]}"
                                 for m in confident),
        })
    if vetoes:
        v = vetoes[0]
        steps.append({
            "title": f"Rejecting {len(vetoes)} near-miss(es) on purpose",
            "detail": f"{v.claim.id} vs {v.item.id}: {v.veto}",
        })
    steps.append({
        "title": f"{matched} confident match(es), {len(matches) - matched} left open",
        "detail": f"Return emails drafted for the {matched} match(es) — nothing ships "
                 "until a human approves it, and high-value items always route to the "
                 "duty manager first",
    })

    return MatchResult(steps=steps, matches=matches, summary={
        "claims": len(open_claims), "items": len(pool), "matched": matched,
        "unmatched": len(matches) - matched,
        "headline": f"{matched} of {len(open_claims)} claims matched to a logged item",
    })


def draft_return_email(claim: Claim, item: FoundItem, *, settings: Settings | None = None,
                       hold_days: int = 90, courier_note: str = "tracked courier, "
                       "postage paid by the hotel", team_name: str | None = None) -> dict:
    """Pure template, no LLM - the return email stays a fixed reply, not a
    generated narrative. The sign-off's team name comes from
    ``lost_found.signature.team_name`` (``config/agent.yaml``, falling back to
    "Housekeeping" - this used to be configured but never actually read,
    SIMULATION.md finding 7).

    The AI-disclosure line (EU AI Act Article 50, ``knowledge/signature.md``)
    is NOT added here - `core.adapters.base.Email.with_signature` appends it
    once, at send time, the same way for every email adapter (mock/imap/gmail
    alike), so a drafted body never carries it twice and no per-adapter or
    per-agent reimplementation can fall out of sync with the others in this
    family. See docs/safety.md.
    """
    first = (claim.guest_name or "").strip().split()[0] if claim.guest_name else "Guest"
    if item.found_days_ago <= 0:
        when = "this morning"
    elif item.found_days_ago == 1:
        when = "yesterday"
    else:
        when = f"{item.found_days_ago} days ago"
    hotel_name = settings.hotel.name if settings else "the hotel"
    if team_name is None:
        team_name = (settings.agent_get("lost_found.signature.team_name", "Housekeeping")
                    if settings else "Housekeeping")
    closing = f"Warm regards,\n{team_name}, {hotel_name}"
    body = "\n\n".join([
        f"Dear {first},",
        f"Good news. {item.item} was handed in {when} from "
        f"{item.found_where.lower()}, and the description you gave us matches it "
        "down to the detail. It is in the lost-property safe at reception with "
        "your name on it.",
        f"Reply to this email and we will send it out today by {courier_note} to "
        "the address on your reservation. The tracking number reaches you the "
        f"moment it leaves the hotel. If you would rather collect it in person, "
        f"we will hold it for {hold_days} days.",
        "Thank you for letting us know so quickly. It made this easy to find.",
        closing,
    ])
    return {"to": claim.contact, "subject": f"We have it: your {item.item[0].lower()}{item.item[1:]}",
           "body": body}


# --------------------------------------------------------------------------
# storage - lf_items / lf_claims, added alongside the core tables with
# Store.migrate() (core/store.py). See ensure_schema below.
# --------------------------------------------------------------------------
LF_SCHEMA = """
CREATE TABLE IF NOT EXISTS lf_items (
  id TEXT PRIMARY KEY, item TEXT NOT NULL, description TEXT, found_where TEXT,
  found_days_ago INTEGER NOT NULL DEFAULT 0, photo_url TEXT, reported_by TEXT,
  status TEXT NOT NULL DEFAULT 'logged', claim_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lf_claims (
  id TEXT PRIMARY KEY, guest_name TEXT NOT NULL, contact TEXT NOT NULL,
  description TEXT, stay_note TEXT, status TEXT NOT NULL DEFAULT 'open',
  matched_item_id TEXT, confidence REAL DEFAULT 0, rationale TEXT,
  high_value INTEGER NOT NULL DEFAULT 0, tracking_number TEXT,
  source_email_id TEXT UNIQUE,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


def ensure_schema(store: Store) -> None:
    store.migrate(LF_SCHEMA)


def _row_item(row: Any) -> FoundItem:
    return FoundItem(id=row["id"], item=row["item"], description=row["description"] or "",
                     found_where=row["found_where"] or "", found_days_ago=row["found_days_ago"],
                     photo_url=row["photo_url"] or "", reported_by=row["reported_by"] or "",
                     status=row["status"], claim_id=row["claim_id"])


def _row_claim(row: Any) -> Claim:
    return Claim(id=row["id"], guest_name=row["guest_name"], contact=row["contact"],
                description=row["description"] or "", stay_note=row["stay_note"] or "",
                status=row["status"], matched_item_id=row["matched_item_id"],
                confidence=row["confidence"] or 0, rationale=row["rationale"] or "",
                high_value=bool(row["high_value"]), tracking_number=row["tracking_number"],
                source_email_id=row["source_email_id"])


def upsert_found_item(store: Store, row: dict) -> tuple[FoundItem, bool]:
    """Insert a found-item row by its own ``id``; a repeat read is a no-op."""
    ensure_schema(store)
    from core.store import utcnow
    existing = store.db.execute("SELECT * FROM lf_items WHERE id=?", (row["id"],)).fetchone()
    if existing is not None:
        return _row_item(existing), False
    now = utcnow()
    store.db.execute(
        "INSERT INTO lf_items (id, item, description, found_where, found_days_ago, "
        "photo_url, reported_by, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (row["id"], row["item"], row.get("description", ""), row.get("found_where", ""),
         int(row.get("found_days_ago") or 0), row.get("photo_url", ""),
         row.get("reported_by", ""), "logged", now, now))
    return _row_item(store.db.execute("SELECT * FROM lf_items WHERE id=?", (row["id"],)).fetchone()), True


def list_available_items(store: Store) -> list[FoundItem]:
    """Items free to be matched: ``logged`` only.

    Deliberately narrower than the source engine's "``status !== returned``"
    (which would leave an item already paired to one claim sitting in the
    pool for a *different* claim to poach on a later run, since a matched
    claim is no longer ``open`` and stops competing for it). An item stays
    reserved for its own claim from the moment it is matched until it is
    shipped, or until a human rejects that match (``revert_match`` below
    puts it back to ``logged``).
    """
    ensure_schema(store)
    rows = store.db.execute("SELECT * FROM lf_items WHERE status='logged' "
                            "ORDER BY id").fetchall()
    return [_row_item(r) for r in rows]


def get_found_item(store: Store, item_id: str) -> FoundItem | None:
    ensure_schema(store)
    row = store.db.execute("SELECT * FROM lf_items WHERE id=?", (item_id,)).fetchone()
    return _row_item(row) if row else None


def update_item(store: Store, item_id: str, **fields: Any) -> None:
    from core.store import utcnow
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    store.db.execute(f"UPDATE lf_items SET {cols}, updated_at=? WHERE id=?",
                     (*fields.values(), utcnow(), item_id))


def upsert_claim(store: Store, claim_id: str, *, guest_name: str, contact: str,
                 description: str = "", stay_note: str = "",
                 source_email_id: str | None = None) -> tuple[Claim, bool]:
    """Insert a claim row keyed on its own id; ``source_email_id`` is UNIQUE
    so the same inbound email can never create a second claim."""
    ensure_schema(store)
    from core.store import utcnow
    existing = store.db.execute("SELECT * FROM lf_claims WHERE id=?", (claim_id,)).fetchone()
    if existing is not None:
        return _row_claim(existing), False
    now = utcnow()
    store.db.execute(
        "INSERT INTO lf_claims (id, guest_name, contact, description, stay_note, status, "
        "source_email_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (claim_id, guest_name, contact, description, stay_note, "open", source_email_id,
         now, now))
    return _row_claim(store.db.execute("SELECT * FROM lf_claims WHERE id=?",
                                       (claim_id,)).fetchone()), True


def list_open_claims(store: Store) -> list[Claim]:
    ensure_schema(store)
    rows = store.db.execute("SELECT * FROM lf_claims WHERE status='open' "
                            "ORDER BY id").fetchall()
    return [_row_claim(r) for r in rows]


def get_claim(store: Store, claim_id: str) -> Claim | None:
    ensure_schema(store)
    row = store.db.execute("SELECT * FROM lf_claims WHERE id=?", (claim_id,)).fetchone()
    return _row_claim(row) if row else None


def update_claim(store: Store, claim_id: str, **fields: Any) -> None:
    from core.store import utcnow
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    store.db.execute(f"UPDATE lf_claims SET {cols}, updated_at=? WHERE id=?",
                     (*fields.values(), utcnow(), claim_id))


# --------------------------------------------------------------------------
# intake - found items
# --------------------------------------------------------------------------
def _rows_to_dicts(rows: list[list[Any]]) -> list[dict]:
    """``Sheets.read()`` returns raw string rows; the first is the header."""
    if not rows:
        return []
    header = [str(h).strip() for h in rows[0]]
    return [dict(zip(header, (str(v) for v in row))) for row in rows[1:] if any(row)]


def load_found_items_rows(settings: Settings) -> list[dict]:
    """Read the found-items intake sheet.

    ``agent.lost_found.intake.source: fixtures`` (what ``make demo`` forces)
    reads ``fixtures/hotel/found_items.csv`` directly, exactly like
    ``core.adapters.pms_mock`` reads ``fixtures/hotel/*.json`` - no
    credentials, no network.

    For real use, sheet name comes from
    ``agent.lost_found.intake.found_items_sheet`` (default ``found_items``).
    When ``systems.sheets.adapter: csv`` (the default), this is a file the
    hotel maintains and re-exports themselves - the same "your own CSV drop"
    convention ``core.adapters.pms_csv`` uses, so it is read straight from
    ``data/imports/<sheet>.csv``, never through ``core.adapters.sheets_csv``
    (that adapter's directory is ``data/exports`` - the agent's own reporting
    output, e.g. the courier log. Reading found-items through it was
    SIMULATION.md finding 3: every doc said ``data/imports``, `make doctor`
    said ``data/imports``, but the code read ``data/exports`` and silently
    found nothing). Any other adapter (``google``) goes through the Sheets
    adapter as normal - a live spreadsheet is not a directory-based import.
    """
    source = settings.agent_get("lost_found.intake.source", "sheet")
    if source == "fixtures":
        import csv
        path = FIXTURES_HOTEL_DIR / "found_items.csv"
        if not path.exists():
            return []
        with path.open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    sheet_name = settings.agent_get("lost_found.intake.found_items_sheet", "found_items")
    if settings.systems.sheets.adapter in ("csv", "mock"):
        import csv
        path = sub_data_dir("imports") / f"{sheet_name}.csv"
        if not path.exists():
            return []
        with path.open(newline="", encoding="utf-8-sig") as fh:
            return list(csv.DictReader(fh))
    sheets = get_sheets(settings)
    return _rows_to_dicts(sheets.read(sheet_name))


def intake_found_items(settings: Settings, store: Store, *, notify_high_value: bool = True) -> dict:
    """Read the intake sheet, log any new rows. Idempotent: a repeat read of
    an already-logged row changes nothing (keyed on the sheet's own id)."""
    high_value_families = frozenset(settings.agent_get(
        "lost_found.high_value_families", sorted(DEFAULT_HIGH_VALUE_FAMILIES)))
    categories = build_categories(settings)
    rows = load_found_items_rows(settings)
    logged, high_value_new = 0, []
    for row in rows:
        if not row.get("id") or not row.get("item"):
            continue
        _, created = upsert_found_item(store, row)
        if not created:
            continue
        logged += 1
        cat = category_of(normalise(f"{row['item']} {row.get('description', '')} "
                                    f"{row.get('found_where', '')}"), categories)
        if is_high_value(cat, high_value_families):
            high_value_new.append(row["id"])
            store.record_event(None, "agent", "high_value_logged",
                              {"item_id": row["id"], "item": row["item"], "family": cat.family})
    if notify_high_value and high_value_new:
        _try_notify_staff(settings, store,
                          f"{len(high_value_new)} high-value item(s) logged and awaiting a "
                          f"claim: {', '.join(high_value_new)}. See docs/safety.md.")
    return {"read": len(rows), "logged": logged, "high_value": len(high_value_new)}


def _try_notify_staff(settings: Settings, store: Store, text: str) -> bool:
    """Best-effort duty-manager ping. Guarded like any other write - shadow
    mode blocks it, and that is by design (see docs/how-it-works.md)."""
    from core.adapters import get_messaging
    from core.review import WriteBlocked
    try:
        get_messaging(settings).notify_staff(text)
        store.record_event(None, "agent", "staff_notified", {"text": text[:200]})
        return True
    except WriteBlocked as exc:
        store.record_event(None, "agent", "staff_notify_blocked", {"reason": str(exc)[:200]})
        return False


# --------------------------------------------------------------------------
# intake - guest claim emails (the one LLM call in this agent)
# --------------------------------------------------------------------------
def _email_to_dict(msg: Any) -> dict:
    return {"id": msg.id, "from": msg.from_email, "from_name": msg.from_name,
           "subject": msg.subject, "body": msg.body_text, "received_at": msg.received_at}


_EXAMPLE_FILE_COMMENT = re.compile(r"<!--.*?-->\s*", re.S)


def strip_example_comments(text: str) -> str:
    """Drop any leftover "copy this file" HTML comment block from prompt text.

    `workflows/00-setup.md` tells the hotel to copy each
    `knowledge/*.example.md` file and fill it in; only `signature.md`'s own
    instructions also say to delete that comment block (SIMULATION.md round
    2, NIT) - the other three (`lost-and-found-policy.md`, `property.md`,
    `faq.md`) do not, so a hotel that follows the docs literally leaves it in
    place, and `core/templates.py:load_knowledge` reads the file verbatim
    into the `extract_claim` prompt. This is a defense-in-depth cleanup at
    the point knowledge content actually enters the prompt, so it does not
    depend on anyone remembering to delete the comment - the same way
    `Email.signature()` (`core/adapters/base.py`) strips `signature.md`'s
    frontmatter regardless of what is left in the file.
    """
    return _EXAMPLE_FILE_COMMENT.sub("", text or "").strip()


def extract_claim_from_email(settings: Settings, store: Store, item: Item, msg: Any,
                             *, provider: str | None = None) -> dict:
    prompt = build_prompt("extract_claim", settings=settings, item=_email_to_dict(msg),
                          fixture_id=msg.id)
    prompt.system = strip_example_comments(prompt.system)
    result: LLMResult = complete("extract_claim", prompt, EXTRACT_CLAIM_SCHEMA,
                                 settings=settings, provider=provider, store=store,
                                 item_id=item.id, fixture_id=msg.id)
    return result.data or {}


def _language_gate(settings: Settings, text: str):
    """``None`` when the guest's language is one of ``hotel.languages``;
    otherwise the detected guess, so the caller can route to ``needs_human``
    with a specific reason instead of drafting a reply in a language the
    property does not support. Uses ``core.i18n.detect_language`` (a
    stopword vote, no extra model call) - not a full model-graded language
    check, but free and good enough to catch the common case."""
    from core.i18n import detect_language
    supported = {str(x).lower() for x in (settings.hotel.languages or ["en"])}
    guess = detect_language(text, settings=settings)
    return None if guess.lang in supported else guess


def process_new_claim_email(settings: Settings, store: Store, msg: Any, *,
                            provider: str | None = None) -> tuple[Claim | None, bool]:
    """Ingest one inbound email as a claim. Idempotent on the email id.

    Returns ``(claim, needs_human)``. ``claim`` is ``None`` when the model
    flagged the email as not actually a lost-item claim, or when the guest
    wrote in a language `hotel.languages` does not list - either way the FSM
    item is still queued as ``needs_human`` so nothing is silently dropped.
    """
    fsm_item = store.upsert_item("email", msg.id, kind="claim_email",
                                payload=_email_to_dict(msg))
    if fsm_item.intent:
        return get_claim(store, fsm_item.intent), False  # already processed this pass

    lang_guess = _language_gate(settings, f"{msg.subject or ''}\n{msg.body_text or ''}")
    if lang_guess is not None:
        default_lang = (settings.hotel.languages or ["en"])[0]
        store.set_fields(fsm_item.id, payload={**_email_to_dict(msg),
                                               "detected_language": lang_guess.lang})
        store.transition(fsm_item.id, "needs_human", actor="agent",
                        detail={"reason": f"guest wrote in {lang_guess.lang}, not in "
                                "hotel.languages", "hotel_default_language": default_lang})
        return None, True

    try:
        data = extract_claim_from_email(settings, store, fsm_item, msg, provider=provider)
    except LLMSchemaError as exc:
        store.set_fields(fsm_item.id, error=str(exc))
        store.transition(fsm_item.id, "needs_human", actor="agent",
                        detail={"error": "extract_claim_schema_error"})
        return None, True

    if data.get("needs_human"):
        store.set_fields(fsm_item.id, payload={**_email_to_dict(msg), "extracted": data})
        store.transition(fsm_item.id, "needs_human", actor="agent",
                        detail={"reason": "not a lost-item claim"})
        return None, True

    claim, _created = upsert_claim(
        store, f"claim-{msg.id}", guest_name=data.get("guest_name", "Guest"),
        contact=data.get("contact", msg.from_email),
        description=with_english_gloss(data.get("description", ""), data.get("description_en", "")),
        stay_note=data.get("stay_note", ""), source_email_id=msg.id)
    store.set_fields(fsm_item.id, intent=claim.id)
    store.transition(fsm_item.id, "skipped", actor="agent",
                    detail={"reason": "structured into lf_claims", "claim_id": claim.id})
    return claim, False


# --------------------------------------------------------------------------
# the matching pass
# --------------------------------------------------------------------------
def _sweep_task_row(store: Store, claim_id: str) -> Any:
    return store.db.execute("SELECT * FROM tasks WHERE kind='lf_sweep' AND ref_id=?",
                            (claim_id,)).fetchone()


def _close_sweep_task(store: Store, claim_id: str, status: str) -> None:
    row = _sweep_task_row(store, claim_id)
    if row is not None and row["status"] == "open":
        store.close_task(row["id"], status=status)


def _active_return_item(store: Store, claim_id: str) -> Item | None:
    """The most recent ``lost_found_return`` FSM item for one claim.

    A claim gets a fresh review item per match attempt
    (``external_id = "<claim_id>:m<N>"``, see ``run_matching_pass``) so a
    rejected-then-re-matched claim always shows up in the queue again
    instead of resurrecting the old terminal ``rejected`` row. Callers that
    need "the item for this claim right now" (``ship_claim``) want the
    latest attempt, not the first one - ``rowid`` orders by insertion,
    unaffected by same-second ``created_at`` timestamps. The bare
    ``external_id=claim_id`` fallback covers rows written before this fix.
    """
    row = store.db.execute(
        "SELECT * FROM items WHERE source='lost_found' AND kind='lost_found_return' "
        "AND (external_id=? OR external_id LIKE ?) ORDER BY rowid DESC LIMIT 1",
        (claim_id, f"{claim_id}:m%")).fetchone()
    return Item.from_row(row) if row else None


def run_matching_pass(settings: Settings, store: Store) -> dict:
    """Score every open claim, queue a return email for each confident match,
    record a reason for every claim that stays open. Safe to call every pass:
    a claim already matched is no longer ``open`` and is not rescored."""
    from datetime import datetime, timedelta, timezone
    threshold = int(settings.agent_get("lost_found.match_threshold", DEFAULT_MATCH_THRESHOLD))
    confusable = frozenset(settings.agent_get("lost_found.confusable_families",
                                              sorted(DEFAULT_CONFUSABLE_FAMILIES)))
    high_value = frozenset(settings.agent_get("lost_found.high_value_families",
                                              sorted(DEFAULT_HIGH_VALUE_FAMILIES)))
    sweep_days = int(settings.agent_get("lost_found.sweep_days", 14))

    claims = list_open_claims(store)
    items = list_available_items(store)
    result = run_claim_matcher(claims, items, threshold=threshold,
                               confusable_families=confusable,
                               high_value_families=high_value, settings=settings)

    queued, escalated = 0, 0
    for match in result.matches:
        if match.item_id:
            update_claim(store, match.claim_id, status="matched",
                        matched_item_id=match.item_id, confidence=match.confidence,
                        rationale=match.rationale, high_value=int(match.high_value))
            update_item(store, match.item_id, status="matched", claim_id=match.claim_id)
            _close_sweep_task(store, match.claim_id, status="matched")
            # Keyed per match ATTEMPT, not per claim (SIMULATION.md round 2,
            # new finding B): a claim only ever re-enters this branch (it
            # only appears in `list_open_claims`) on its first-ever match, or
            # after a human `reject` -> `revert_match` put it back to `open`.
            # A bare `external_id=claim_id` made `upsert_item` return the OLD
            # `lost_found_return` row untouched - terminal `rejected`,
            # `draft` already set - so the `if fsm_item.draft is None` guard
            # below silently skipped drafting/queuing a new one: the claim
            # believed it had a fresh match, but `make review` never showed
            # it again, forever. `<claim_id>:m<N>` makes every attempt its
            # own row, so a re-match always produces a fresh, visible
            # `pending_review`/`needs_human` item - see `_active_return_item`
            # (used by `ship_claim`) for how the latest attempt is found.
            seq = store.next_sequence(f"lf_return_seq:{match.claim_id}",
                                      dry_run=settings.dry_run)
            fsm_item = store.upsert_item(
                "lost_found", f"{match.claim_id}:m{seq}", kind="lost_found_return",
                payload={"claim_id": match.claim_id, "item_id": match.item_id,
                        "item_label": match.item_label, "evidence": match.evidence,
                        "confidence": match.confidence, "high_value": match.high_value,
                        "rationale": match.rationale})
            if fsm_item.draft is None:
                store.set_fields(fsm_item.id, draft=match.draft_email,
                                confidence=match.confidence / 100.0)
                status = "needs_human" if match.high_value else "pending_review"
                fsm_item = store.transition(fsm_item.id, status, actor="agent",
                                           detail={"high_value": match.high_value})
                queued += 1
                if match.high_value:
                    escalated += 1
                    _try_notify_staff(settings, store,
                        f"High-value match needs duty-manager sign-off before it can be "
                        f"approved: claim {match.claim_id} -> {match.item_label}. "
                        "See docs/safety.md.")
        else:
            update_claim(store, match.claim_id, rationale=match.rationale,
                        high_value=int(match.high_value))
            if _sweep_task_row(store, match.claim_id) is None:
                due = (datetime.now(timezone.utc) + timedelta(days=sweep_days)
                      ).isoformat(timespec="seconds")
                store.upsert_task(kind="lf_sweep", ref_id=match.claim_id,
                                 next_action_due=due, max_follow_ups=1,
                                 payload={"claim_id": match.claim_id})

    return {"claims": result.summary["claims"], "items": result.summary["items"],
           "matched": result.summary["matched"], "unmatched": result.summary["unmatched"],
           "queued": queued, "escalated": escalated, "headline": result.summary["headline"],
           "steps": result.steps}


def sweep_stale_claims(settings: Settings, store: Store) -> list[str]:
    """Expire claims whose 14-day sweep task has come due and still unmatched.

    A claim that matched in the meantime already had its task closed by
    ``run_matching_pass``, so ``due_tasks`` only ever returns genuinely stale
    ones here. Pings the duty manager instead of leaving the claim open
    forever silently.
    """
    expired = []
    for task in store.due_tasks(kind="lf_sweep"):
        claim_id = task.ref_id
        claim = get_claim(store, claim_id)
        if claim is None or claim.status != "open":
            store.close_task(task.id, status="skipped")
            continue
        update_claim(store, claim_id, status="expired")
        store.close_task(task.id, status="expired")
        expired.append(claim_id)
    if expired:
        _try_notify_staff(settings, store,
                          f"{len(expired)} claim(s) reached the 14-day sweep with no match: "
                          f"{', '.join(expired)}. Decide whether to tell the guest or close "
                          "the case.")
    return expired


def revert_match(store: Store, claim_id: str) -> None:
    """Undo a proposed match: item goes back to ``logged``, claim back to
    ``open``. Called when a human rejects the drafted return email
    (tools/review.py reject), so the item is available for re-matching."""
    claim = get_claim(store, claim_id)
    if claim is None:
        return
    if claim.matched_item_id:
        update_item(store, claim.matched_item_id, status="logged", claim_id=None)
    update_claim(store, claim_id, status="open", matched_item_id=None, confidence=0,
                rationale="Match rejected by a human — back in the pool.")


def ship_claim(settings: Settings, store: Store, claim_id: str, *,
              duty_manager_ack: str | None = None) -> dict:
    """"Approve return / ship": issue a tracking number, close the case.

    Requires the linked return-email FSM item to already be ``sent`` (the
    confirmation email actually went out) and, for a high-value claim, a
    named duty-manager acknowledgement. The courier log write and the staff
    ping are best-effort and honestly labelled as simulated - see
    docs/how-it-works.md and docs/integrations.md.

    Gated by `core.review.assert_write_allowed` exactly like a send: `mode:
    shadow` blocks this too, unconditionally - no tracking number is issued
    and no status changes, only the same courier-log/staff-ping best-effort
    calls run (and those are themselves blocked in shadow). Before this fix
    (SIMULATION.md finding 1) the shipment itself was never guarded, so
    `mode: shadow` still issued a real tracking number and marked the claim
    shipped - only the auxiliary courier-log write and staff ping were
    actually blocked.
    """
    from core.review import WriteBlocked, assert_write_allowed
    claim = get_claim(store, claim_id)
    if claim is None:
        raise KeyError(f"no claim {claim_id}")
    if claim.status == "shipped":
        return {"ok": False, "reason": f"claim {claim_id} is already shipped "
                f"(tracking {claim.tracking_number})"}
    if claim.status != "matched" or not claim.matched_item_id:
        return {"ok": False, "reason": f"claim {claim_id} has no confident match to ship"}
    fsm_item = _active_return_item(store, claim_id)
    if fsm_item is None or fsm_item.review_status != "sent":
        status = fsm_item.review_status if fsm_item else "not queued"
        return {"ok": False, "reason": f"the return email is '{status}', not 'sent' yet — "
                "approve and send it first (tools/review.py approve / send)"}
    if claim.high_value and not duty_manager_ack:
        return {"ok": False, "reason": f"claim {claim_id} is high-value — ship requires "
                "--duty-manager-ack \"<name>\" (see docs/safety.md)"}
    try:
        assert_write_allowed(settings, "ship_item")
    except WriteBlocked as exc:
        store.record_event(fsm_item.id, "agent", "ship_blocked", {"reason": str(exc)[:200]})
        return {"ok": False, "reason": str(exc)}

    # The last dash-separated token of the claim id is the descriptive word
    # (``claim-claim-01-bracelet`` -> ``bracelet``) - a blind ``claim_id[-6:]``
    # truncated mid-word (SIMULATION.md finding 11: "LF-ACELET-0001").
    tag = re.sub(r"[^A-Za-z0-9]", "", claim_id.rsplit("-", 1)[-1]).upper() or "ITEM"
    tracking = f"LF-{tag[:12]}-{store.next_sequence('courier_tracking', dry_run=settings.dry_run):04d}"
    update_claim(store, claim_id, status="shipped", tracking_number=tracking)
    update_item(store, claim.matched_item_id, status="returned")
    store.record_event(fsm_item.id, "human", "shipped",
                      {"tracking": tracking, "duty_manager_ack": duty_manager_ack})
    hold_days = int(settings.agent_get("lost_found.shipping.hold_days", 90))
    note = settings.agent_get("lost_found.shipping.courier_note",
                              "tracked courier, postage paid by the hotel")
    logged = _try_export_courier_log(settings, store, claim_id, tracking, note)
    notified = _try_notify_staff(settings, store,
                                 f"Claim {claim_id} shipped, tracking {tracking}.")
    return {"ok": True, "claim_id": claim_id, "tracking_number": tracking,
           "hold_days": hold_days, "courier_note": note, "logged": logged, "notified": notified}


def _try_export_courier_log(settings: Settings, store: Store, claim_id: str,
                            tracking: str, note: str) -> bool:
    from core.review import WriteBlocked
    from core.store import utcnow
    try:
        get_sheets(settings).append("courier_log", [[utcnow(), claim_id, tracking, note]])
        return True
    except WriteBlocked as exc:
        store.record_event(None, "agent", "courier_log_blocked", {"reason": str(exc)[:200]})
        return False
