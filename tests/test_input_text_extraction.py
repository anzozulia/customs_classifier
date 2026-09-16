"""What /history shows as "the thing that was classified".

Found in the browser: answering a clarification wrote ChatKit's own model-facing boilerplate
into the register, so a row read

    A structured input request was displayed to the user with the following status: answered
    <StructuredInput> - Ці кросівки призначені саме для занять спортом…?: Для занять спортом
    </StructuredInput>

instead of «Кросівки чоловічі з верхом із натуральної шкіри». The boilerplate is correct to
send a MODEL and wrong to store as a product description.
"""

from __future__ import annotations

from app.chat.server import (
    _is_structured_input,
    _last_user_text,
    _product_text,
    _structured_answers,
    _title_from,
)

PRODUCT = "Кросівки чоловічі з верхом із натуральної шкіри"

# Verbatim shape from chatkit/agents.py.
ANSWERED = (
    "A structured input request was displayed to the user with the following status: answered\n"
    "<StructuredInput>\n"
    "- З якого матеріалу зовнішня підошва кросівок: гума/пластмаса чи натуральна шкіра?: "
    "Гума або пластмаса\n"
    "</StructuredInput>"
)


def _user(text: str) -> dict:
    return {"role": "user", "content": [{"type": "input_text", "text": text}]}


def _assistant(text: str) -> dict:
    return {"role": "assistant", "content": [{"type": "output_text", "text": text}]}


def test_the_boilerplate_is_recognised() -> None:
    assert _is_structured_input(ANSWERED) is True
    assert _is_structured_input(PRODUCT) is False


def test_only_the_answer_is_extracted_and_the_question_keeps_its_colon() -> None:
    """The question contains a colon, so the split has to come from the right."""
    assert _structured_answers(ANSWERED) == "Гума або пластмаса"


def test_several_questions_answered_in_one_item() -> None:
    text = (
        "status: answered\n<StructuredInput>\n"
        "- Матеріал?: Гума\n- Призначення?: Для занять спортом\n"
        "</StructuredInput>"
    )
    assert _structured_answers(text) == "Гума; Для занять спортом"


def test_unanswered_and_skipped_are_not_answers() -> None:
    text = (
        "status: answered\n<StructuredInput>\n"
        "- Матеріал?: skipped\n- Розмір?: unanswered\n- Колір?: Чорний\n"
        "</StructuredInput>"
    )
    assert _structured_answers(text) == "Чорний"


def test_the_register_shows_the_product_not_the_answer() -> None:
    """THE regression. Three turns of one thread must all read as the same product."""
    items = [
        _user(PRODUCT),
        _assistant("уточнення"),
        _user(ANSWERED),
    ]
    assert _product_text(items) == PRODUCT
    # The naive reading — what shipped and what the screenshot caught.
    assert _last_user_text(items) == ANSWERED


def test_the_thread_title_is_the_product_too() -> None:
    items = [_user(PRODUCT), _assistant("уточнення"), _user(ANSWERED)]
    assert _title_from(items) == PRODUCT[:60]
    assert "StructuredInput" not in _title_from(items)


def test_a_window_holding_only_answers_falls_back_to_the_answers() -> None:
    """A long thread can push the original message out of the bounded window. Better the
    answer than the boilerplate."""
    assert _product_text([_user(ANSWERED)]) == "Гума або пластмаса"


def test_no_user_text_at_all_is_empty_not_an_error() -> None:
    assert _product_text([]) == ""
    assert _product_text([_assistant("привіт")]) == ""
