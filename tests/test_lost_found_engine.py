"""Tests for tools/engine.py's pure matching logic: category detection,
scoring, the confusable-family veto, the high-value gate, and the greedy
assignment. No I/O, no store, no LLM - see docs/how-it-works.md.
"""

from __future__ import annotations

from tools.engine import (Claim, DEFAULT_HIGH_VALUE_FAMILIES, DEFAULT_MATCH_THRESHOLD, FoundItem,
                          _language_gate, build_categories, category_of, draft_return_email,
                          is_high_value, normalise, run_claim_matcher, score_pair,
                          strip_example_comments, with_english_gloss)


class _FakeSettings:
    """Stand-in for core.config.Settings - build_categories only ever calls
    .agent_get()."""

    def __init__(self, extra_categories):
        self._extra = extra_categories

    def agent_get(self, path, default=None):
        return self._extra if path == "lost_found.extra_categories" else default


def _claim(id_="c1", guest_name="Priya Shah", contact="priya.shah@example.com",
          description="", stay_note="") -> Claim:
    return Claim(id=id_, guest_name=guest_name, contact=contact, description=description,
                stay_note=stay_note)


def _item(id_="fi-1", item="", description="", found_where="", found_days_ago=0) -> FoundItem:
    return FoundItem(id=id_, item=item, description=description, found_where=found_where,
                     found_days_ago=found_days_ago)


def test_category_order_charger_beats_watch():
    # "Garmin watch charger" must classify as charger, not watch - order matters
    # in CATEGORIES (spec section 3, step 3).
    cat = category_of("garmin watch charger")
    assert cat is not None
    assert cat.id == "charger"


def test_category_eyewear_family_shared_by_two_categories():
    sunglasses = category_of("sunglasses, tortoiseshell frame")
    readers = category_of("reading glasses in a red case")
    assert sunglasses.family == readers.family == "eyewear"
    assert sunglasses.id != readers.id


def test_score_pair_confident_bracelet_match():
    # Spec section 8, example 1.
    claim = _claim(description="a silver bracelet with a small anchor charm",
                  stay_note="lost near the bar last weekend")
    item = _item(item="Silver bracelet with a small anchor charm",
                description="Ladies bracelet, silver chain, anchor charm",
                found_where="Bar", found_days_ago=2)
    scored = score_pair(claim, item)
    assert scored.score >= 100
    assert scored.veto is None
    assert any("silver" in e and "anchor" in e and "charm" in e for e in scored.evidence)


def test_score_pair_confusable_family_veto():
    # Spec section 8, example 2 - the deliberate trap.
    claim = _claim(description="tortoiseshell reading glasses in a red case",
                  stay_note="left at reception when checking out")
    item = _item(item="Sunglasses, tortoiseshell frame", description="Designer sunglasses, no case",
                found_where="Lobby", found_days_ago=4)
    scored = score_pair(claim, item)
    assert scored.score == 0
    assert scored.veto is not None
    assert "sunglasses" in scored.veto.lower()
    assert "reading glasses" in scored.veto.lower()


def test_score_pair_different_family_no_veto_no_score():
    claim = _claim(description="a grey fleece")
    item = _item(item="Navy blazer", description="Men's navy blazer, size L")
    scored = score_pair(claim, item)
    assert scored.score == 0
    assert scored.veto is None


def test_is_high_value_covers_jewellery_and_documents():
    assert is_high_value(category_of("gold hoop earring"), DEFAULT_HIGH_VALUE_FAMILIES)
    assert is_high_value(category_of("eu passport, burgundy cover"), DEFAULT_HIGH_VALUE_FAMILIES)
    assert not is_high_value(category_of("grey fleece"), DEFAULT_HIGH_VALUE_FAMILIES)
    assert not is_high_value(None, DEFAULT_HIGH_VALUE_FAMILIES)


def test_run_claim_matcher_never_double_assigns_an_item_or_claim():
    # Two claims that could both plausibly want the one logged item - the
    # matcher must award it to at most one of them.
    item = _item(id_="fi-1", item="Silver bracelet with a small anchor charm",
                description="Ladies bracelet, silver chain, anchor charm",
                found_where="Bar", found_days_ago=2)
    strong = _claim(id_="c-strong", description="a silver bracelet with a small anchor charm",
                    stay_note="lost near the bar last weekend")
    weak = _claim(id_="c-weak", description="a bracelet, not sure what colour")
    result = run_claim_matcher([strong, weak], [item])
    matched_claims = {m.claim_id for m in result.matches if m.item_id}
    assert matched_claims == {"c-strong"}
    weak_match = next(m for m in result.matches if m.claim_id == "c-weak")
    assert weak_match.item_id is None
    assert weak_match.rationale  # never a silent blank


def test_run_claim_matcher_below_threshold_never_proposed():
    item = _item(id_="fi-1", item="Black umbrella", description="Plain black umbrella")
    claim = _claim(description="a bracelet, silver")
    result = run_claim_matcher([claim], [item])
    assert result.summary["matched"] == 0
    assert result.matches[0].item_id is None


def test_extra_categories_from_config_are_recognised():
    # SIMULATION.md finding 6: a category listed only in
    # knowledge/lost-and-found-policy.md scored 0% because the matcher never
    # saw it. lost_found.extra_categories (config/agent.yaml) is the fix.
    settings = _FakeSettings([{"id": "beach_towel", "family": "beach_gear",
                              "label": "beach towel", "keywords": ["beach towel"]}])
    categories = build_categories(settings)
    cat = category_of("a striped beach towel", categories)
    assert cat is not None and cat.id == "beach_towel"

    claim = _claim(description="my navy beach towel, lost by the pool")
    item = _item(item="Navy beach towel", description="found by the pool",
                found_days_ago=1)
    assert score_pair(claim, item, categories=categories).score >= 60
    # Without the config, the same pair is invisible to the matcher.
    assert score_pair(claim, item).score == 0


def test_draft_return_email_uses_first_name_item_and_hold_days():
    claim = _claim(guest_name="Priya Shah", contact="priya.shah@example.com")
    item = _item(item="Silver bracelet", found_where="Bar", found_days_ago=1)
    draft = draft_return_email(claim, item, hold_days=90)
    assert draft["to"] == "priya.shah@example.com"
    assert "Dear Priya," in draft["body"]
    assert "Silver bracelet" in draft["body"]
    assert "90 days" in draft["body"]


def test_draft_return_email_uses_team_name_from_config():
    # SIMULATION.md finding 7 (adjacent dead config): lost_found.signature.
    # team_name (config/agent.yaml) used to be defined but never read - every
    # draft signed off as "Housekeeping" regardless of what was configured.
    from core.config import HotelConfig, Settings
    settings = Settings(hotel=HotelConfig(name="Casa Miravalle"),
                        agent={"lost_found": {"signature": {"team_name": "Front Desk"}}})
    claim = _claim(guest_name="Priya Shah", contact="priya.shah@example.com")
    item = _item(item="Silver bracelet", found_where="Bar", found_days_ago=1)
    draft = draft_return_email(claim, item, settings=settings)
    assert "Front Desk, Casa Miravalle" in draft["body"]
    # The AI-disclosure line itself is NOT baked in here - core.adapters.
    # base.Email.with_signature appends knowledge/signature.md once, at send
    # time, the same way for every adapter (see docs/safety.md).


def test_draft_return_email_team_name_falls_back_with_no_settings():
    # No settings at all - must not crash, and the sign-off falls back cleanly.
    claim = _claim(guest_name="Priya Shah", contact="priya.shah@example.com")
    item = _item(item="Silver bracelet", found_where="Bar", found_days_ago=1)
    draft = draft_return_email(claim, item)
    assert "Housekeeping, the hotel" in draft["body"]


def test_language_gate_flags_a_language_outside_hotel_languages():
    from core.config import HotelConfig, Settings
    settings = Settings(hotel=HotelConfig(languages=["en"]))
    text = ("Bonjour, j'ai perdu mes lunettes de soleil a la reception hier. "
           "Merci beaucoup, cordialement Jean")
    guess = _language_gate(settings, text)
    assert guess is not None
    assert guess.lang == "fr"


def test_language_gate_allows_a_supported_language():
    from core.config import HotelConfig, Settings
    settings = Settings(hotel=HotelConfig(languages=["en", "fr"]))
    text = ("Bonjour, j'ai perdu mes lunettes de soleil a la reception hier. "
           "Merci beaucoup, cordialement Jean")
    assert _language_gate(settings, text) is None


def test_category_of_recognises_es_fr_de_it_pt_accent_folded():
    # SIMULATION.md round 2, new finding A: category_of() was English-only,
    # so a guest claim written in the hotel's own listed language (Portuguese,
    # for that round's persona) never classified. These are the exact 4
    # phrases the simulation confirmed returned category_of() == None; all 4
    # must now classify, and "oculos de sol" proves the accent fold
    # (normalise() strips accents before every CATEGORIES pattern is tried).
    assert category_of(normalise("passaporte, capa bordo")).id == "documents"
    assert category_of(normalise("pulseira de prata")).id == "jewellery"
    assert category_of(normalise("óculos de sol")).id == "sunglasses"
    assert category_of(normalise("carteira de identidade")).id == "documents"
    # One phrase each for the other four languages the extract_claim prompt
    # reads (es/fr/de/it), for coverage beyond this round's pt persona.
    assert category_of(normalise("una pulsera de plata con dije")).id == "jewellery"          # es
    assert category_of(normalise("j'ai perdu mon passeport bleu")).id == "documents"           # fr
    assert category_of(normalise("meine Sonnenbrille ist weg")).id == "sunglasses"             # de
    assert category_of(normalise("ho perso il mio passaporto")).id == "documents"              # it


def test_portuguese_claim_matches_the_same_bracelet_an_english_claim_matches():
    # SIMULATION.md round 2, new finding A regression: a claim faithfully
    # extracted in Portuguese must match the same logged item an
    # English-written claim matches, not silently score 0 against it because
    # category_of() couldn't read the language.
    item = _item(item="Silver bracelet with a small anchor charm",
                description="Ladies bracelet, silver chain, anchor charm",
                found_where="Bar", found_days_ago=2)
    english = _claim(description="a silver bracelet with a small anchor charm",
                     stay_note="lost near the bar last weekend")

    # The raw guest text alone (no English gloss) must already classify -
    # "the matcher must not depend on" the extract_claim prompt's optional
    # description_en translation.
    portuguese_raw = "uma pulseira de prata com um pequeno pingente em forma de âncora"
    assert category_of(normalise(portuguese_raw)).id == \
        category_of(normalise(english.description)).id == "jewellery"

    # with_english_gloss is the same helper process_new_claim_email uses to
    # fold extract_claim's optional description_en into the stored
    # description - here it supplies the shared "silver"/"anchor"/"charm"
    # words the (English-only) ATTRIBUTES scorer looks for, exactly as a
    # real extraction would.
    portuguese = _claim(
        description=with_english_gloss(portuguese_raw,
                                       "a silver bracelet with a small anchor charm"),
        stay_note="perto do bar no fim de semana passado")

    en_scored = score_pair(english, item)
    pt_scored = score_pair(portuguese, item)
    assert en_scored.score >= DEFAULT_MATCH_THRESHOLD
    assert pt_scored.score >= DEFAULT_MATCH_THRESHOLD
    assert pt_scored.veto is None
    assert any("silver" in e and "anchor" in e and "charm" in e for e in pt_scored.evidence)


def test_with_english_gloss_is_a_noop_without_a_translation():
    # No description_en (mock/interactive fixtures that predate the field,
    # or the model left it blank) - description passes through unchanged.
    assert with_english_gloss("uma pulseira de prata", "") == "uma pulseira de prata"
    assert with_english_gloss("uma pulseira de prata", None) == "uma pulseira de prata"
    # Already-English descriptions aren't glossed onto themselves.
    same = "a silver bracelet"
    assert with_english_gloss(same, "A Silver Bracelet") == same


def test_with_english_gloss_appends_once():
    out = with_english_gloss("uma pulseira de prata", "a silver bracelet")
    assert out == "uma pulseira de prata (a silver bracelet)"


def test_build_categories_accent_folds_hotel_configured_keywords():
    # extra_categories keywords go through the same accent fold as the
    # built-in table, in both directions: a hotel that types an accented
    # keyword ("às riscas") still matches guest text without the accent, and
    # a guest who does type the accent still matches too.
    settings = _FakeSettings([{"id": "beach_towel", "family": "beach_gear",
                              "label": "beach towel", "keywords": ["toalha de praia às riscas"]}])
    categories = build_categories(settings)
    unaccented = category_of(normalise("uma toalha de praia as riscas azul e branca"), categories)
    accented = category_of(normalise("uma toalha de praia às riscas azul e branca"), categories)
    assert unaccented is not None and unaccented.id == "beach_towel"
    assert accented is not None and accented.id == "beach_towel"


def test_strip_example_comments_removes_the_copy_me_block():
    # SIMULATION.md round 2, NIT: workflows/00-setup.md only tells the hotel
    # to delete the copy-me comment for signature.md; the other three
    # knowledge/*.example.md files' leftover comment would otherwise reach
    # the extract_claim prompt verbatim (core/templates.py:load_knowledge
    # reads knowledge/*.md as-is - it is core, so the cleanup has to happen
    # on this side, at the point the assembled prompt leaves this repo's
    # code).
    raw = (
        "### lost-and-found-policy\n"
        "# Lost & found policy - Hotel Aurora\n\n"
        "<!--\n"
        "Copy this to knowledge/lost-and-found-policy.md and replace it with\n"
        "your own numbers.\n"
        "-->\n\n"
        "Hold period: 90 days.\n"
    )
    cleaned = strip_example_comments(raw)
    assert "Copy this to" not in cleaned
    assert "<!--" not in cleaned and "-->" not in cleaned
    assert "Hold period: 90 days." in cleaned


def test_strip_example_comments_is_a_noop_with_no_comment():
    text = "### faq\nDo you allow pets? Yes, up to two per room."
    assert strip_example_comments(text) == text
