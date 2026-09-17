"""What the user reads after a classification.

Three rounds of browser feedback are pinned here, because each fix caused the next problem:

1. Unreadable. The database `full_path` and the model's `Шлях` were the same chain printed
   twice, both starting with a ~200-character SECTION title that describes no goods.
2. Misleading. Replacing them with a short label built from the heading's LAST semicolon
   clause labelled a camera (9006) as «фотоспалахи» — flashbulbs.
3. Empty. Using the deepest segment instead labelled a cotton t-shirt «З бавовни», which is a
   differentia, not a description.

The conclusion those three encode: a UKTZED code HAS no short label. It is a heading plus
successive narrowings, and the only honest label is the whole chain from the heading down.
"""

from __future__ import annotations

from app.agent.tools_terminal import _breadcrumb, _describe

SECTION = "Машини, обладнання та механізми; електротехнічне обладнання; їх частини"
CHAPTER = "Електричні машини, обладнання та їх частини"
HEADING_9006 = "Фотокамери (крім кінокамер); фотоспалахи та лампи-спалахи"
HEADING_6109 = "Футболки, майки та інша натільна білизна, трикотажні машинного чи ручного в’язання"


def _path(*segments: str) -> str:
    return " > ".join(segments)


def test_the_section_and_chapter_are_dropped() -> None:
    """A 10-digit code's legal description begins at the 4-digit heading."""
    specific, heading = _describe(_path(SECTION, CHAPTER, HEADING_9006, "інші"))
    assert SECTION not in specific and SECTION not in heading
    assert CHAPTER not in specific and CHAPTER not in heading
    assert heading == HEADING_9006


def test_the_narrowing_leads_and_the_heading_is_context() -> None:
    """Round 3: «З бавовни» names no goods, so it is never shown WITHOUT its heading — but it
    leads, because it is the part that identifies this code rather than its 40 siblings."""
    specific, heading = _describe(_path(SECTION, CHAPTER, HEADING_6109, "з бавовни"))
    # As stored: a narrowing is a mid-sentence fragment and the renderer no longer
    # re-cases it — it is set after an em dash on the heading line.
    assert specific == "з бавовни"
    assert heading == HEADING_6109


def test_a_long_heading_never_leads() -> None:
    """Round 4: heading 8516 enumerates water heaters, hair dryers and irons; a coffee machine
    is one clause of it. Leading with that wall buried the answer."""
    long_heading = (
        "Електричні водонагрівачі акумулювальні або безінерційні; праски електричні; "
        "інші побутові електронагрівальні прилади"
    )
    specific, heading = _describe(_path(SECTION, CHAPTER, long_heading, "для приготування кави"))
    assert specific == "для приготування кави"
    assert heading == long_heading


def test_residual_narrowings_still_lead() -> None:
    """All-residual narrowings read oddly, but promoting the heading instead bolded 240
    characters of enumeration and buried the answer a second time."""
    specific, heading = _describe(_path(SECTION, CHAPTER, HEADING_9006, "інші", "інші"))
    assert specific == "інші → інші"
    assert heading == HEADING_9006


def test_a_heading_is_never_reduced_to_one_of_its_clauses() -> None:
    """Round 2: heading 9006 enumerates cameras AND flashbulbs. Nothing in the text says which
    one a subdivision belongs to, so no clause may be selected as 'the' label."""
    _, heading = _describe(_path(SECTION, CHAPTER, HEADING_9006, "інші"))
    assert heading == HEADING_9006, "the heading is shown whole, never a clause of it"


def test_a_code_hanging_straight_off_its_heading_has_no_context_line() -> None:
    """Nothing narrows it, so the heading IS the description and repeating it would be the
    duplication this whole rework removed."""
    specific, heading = _describe(_path(SECTION, CHAPTER, HEADING_6109))
    assert specific == HEADING_6109
    assert heading == ""


def test_the_breadcrumb_is_derived_from_the_code_not_from_a_path() -> None:
    """No model input, so no label can be wrong — and the section, which is not part of any
    code, cannot leak in."""
    trail = _breadcrumb("6109100000")
    assert trail.startswith("61 ")
    assert "6109" in trail
    assert "**6109 10 00 00**" in trail


def test_the_breadcrumb_survives_a_non_ten_digit_code() -> None:
    assert "**3919**" in _breadcrumb("3919")
