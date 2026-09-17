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

from app.agent.tools_terminal import _breadcrumb, _goods_description

SECTION = "Машини, обладнання та механізми; електротехнічне обладнання; їх частини"
CHAPTER = "Електричні машини, обладнання та їх частини"
HEADING_9006 = "Фотокамери (крім кінокамер); фотоспалахи та лампи-спалахи"
HEADING_6109 = "Футболки, майки та інша натільна білизна, трикотажні машинного чи ручного в’язання"


def _path(*segments: str) -> str:
    return " > ".join(segments)


def test_the_section_and_chapter_are_dropped() -> None:
    """A 10-digit code's legal description begins at the 4-digit heading."""
    goods = _goods_description(_path(SECTION, CHAPTER, HEADING_9006, "інші"))
    assert SECTION not in goods
    assert CHAPTER not in goods
    assert goods.startswith("Фотокамери")


def test_the_whole_chain_below_the_heading_survives() -> None:
    """Round 3: the differentia alone says nothing, so it is never shown alone."""
    goods = _goods_description(_path(SECTION, CHAPTER, HEADING_6109, "з бавовни"))
    assert "Футболки" in goods, "the goods class must be present"
    assert "з бавовни" in goods, "the narrowing must be present"
    assert goods == f"{HEADING_6109} → з бавовни"


def test_a_heading_is_never_reduced_to_one_of_its_clauses() -> None:
    """Round 2: heading 9006 enumerates cameras AND flashbulbs. Nothing in the text says which
    one a subdivision belongs to, so no clause may be selected as 'the' label."""
    goods = _goods_description(_path(SECTION, CHAPTER, HEADING_9006, "інші"))
    assert goods.startswith("Фотокамери (крім кінокамер)")
    assert not goods.startswith("фотоспалахи")


def test_a_short_path_is_not_truncated_into_nothing() -> None:
    """A code hanging directly off its heading has only three segments; dropping two would
    leave the leaf alone, which is the round-3 bug."""
    goods = _goods_description(_path(SECTION, CHAPTER, HEADING_6109))
    assert goods == HEADING_6109


def test_the_breadcrumb_is_derived_from_the_code_not_from_a_path() -> None:
    """No model input, so no label can be wrong — and the section, which is not part of any
    code, cannot leak in."""
    trail = _breadcrumb("6109100000")
    assert trail.startswith("61 ")
    assert "6109" in trail
    assert "**6109 10 00 00**" in trail


def test_the_breadcrumb_survives_a_non_ten_digit_code() -> None:
    assert "**3919**" in _breadcrumb("3919")
