
# the en dash are correct typography here, not Latin look-alikes; pyproject pins ruff's
# defaults (no allowed-confusables), so the exemption is declared per file.
"""The check v1 never had.

v1 took whatever code the model produced and rendered it. Nothing asked the database whether
that code existed, and nothing asked whether it was a code you may actually declare. This is
the gate, and it is two lines of SQL behind one function.

It is not the provenance gate — `TurnLedger` answers "did *this turn* show the model this
code?", this answers "is this code real and final?". Both have to pass.
"""

from __future__ import annotations

from enum import StrEnum

from app.agent.schemas import NodeDetail
from app.tariff.repo import TariffRepo


class CodeStatus(StrEnum):
    """Outcome of validating one code against the active dataset."""

    VALID = "valid"
    """Exists and is terminal. May be shown."""

    NOT_FOUND = "not_found"
    """Not in the dataset at all — a hallucination, or a code from another nomenclature."""

    NOT_TERMINAL = "not_terminal"
    """Real, but an intermediate heading. Not declarable: the model must drill deeper."""

    AMBIGUOUS = "ambiguous"
    """Valid and terminal, but its `full_path` is shared with siblings.

    A PASS, not a failure. 2,487 terminals (23.71%) share a path with a sibling because the
    real tariff separates them by header text this snapshot dropped. The honest answer is
    "here are the N codes this dataset cannot separate", so the answer must carry the set.
    """

    @property
    def is_pass(self) -> bool:
        return self in (CodeStatus.VALID, CodeStatus.AMBIGUOUS)


_MESSAGES = {
    CodeStatus.NOT_FOUND: (
        "Коду {code} немає в довіднику. Перевір через open_category і виклич знову."
    ),
    CodeStatus.NOT_TERMINAL: (
        "Код {code} проміжний, а не кінцевий. Відкрий його через expand і обери "
        "один із {child_count} нащадків."
    ),
    CodeStatus.AMBIGUOUS: (
        "Код валідний, але його текстовий шлях не унікальний серед сусідів. "
        "Покажи користувачу сусідні коди разом із цим."
    ),
    CodeStatus.VALID: "",
}


async def validate_code(repo: TariffRepo, code: str) -> CodeStatus:
    """Does this code exist in the active dataset, and is it terminal?"""
    status, _ = await validate_and_resolve(repo, code)
    return status


async def validate_and_resolve(
    repo: TariffRepo, code: str
) -> tuple[CodeStatus, NodeDetail | None]:
    """`validate_code` plus the row, because every caller needs both.

    The row is what the answer is rendered from: `full_path` and `description` are ALWAYS
    overwritten from the database, never taken from the model. That deletes a whole class of
    plausible-looking-but-wrong descriptions.
    """
    detail = await repo.resolve(code)
    if detail is None:
        return CodeStatus.NOT_FOUND, None
    if not detail.is_terminal:
        return CodeStatus.NOT_TERMINAL, detail
    if detail.path_is_ambiguous:
        return CodeStatus.AMBIGUOUS, detail
    return CodeStatus.VALID, detail


def message_for(status: CodeStatus, code: str, detail: NodeDetail | None = None) -> str:
    """The Ukrainian correction line for a failed or qualified check.

    Lives here rather than in the tool so that the wording and the condition cannot drift
    apart — v1's rules and its checks were in different files and disagreed.
    """
    return _MESSAGES[status].format(
        code=code, child_count=detail.child_count if detail else 0
    )
