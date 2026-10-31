"""Cutting streamed reply text into sentences for speech (M5.1)."""

import pytest

from backend.app.voice.sentences import SOFT_LIMIT, SentenceSplitter

REPLY = (
    "Your main account has 1,250.50 US dollars. Shall I submit a payment of 120.00?\n"
    'You said "yes." Great! It\'s recorded (reference kept). Anything else?'
)
EXPECTED = [
    "Your main account has 1,250.50 US dollars.",
    "Shall I submit a payment of 120.00?",
    'You said "yes."',
    "Great!",
    "It's recorded (reference kept).",
    "Anything else?",
]


def split_all(text: str, piece: int) -> list[str]:
    splitter = SentenceSplitter()
    sentences = []
    for start in range(0, len(text), piece):
        sentences += splitter.feed(text[start:start + piece])
    rest = splitter.flush()
    return sentences + ([rest] if rest else [])


@pytest.mark.parametrize("piece", [1, 2, 3, 4, 7, 50, 10_000])
def test_sentences_are_the_same_however_the_text_arrives(piece) -> None:
    assert split_all(REPLY, piece) == EXPECTED


def test_a_sentence_is_released_as_soon_as_the_next_one_starts() -> None:
    splitter = SentenceSplitter()

    # Held back: what follows the full stop isn't known yet.
    assert splitter.feed("Your balance is 500 dollars.") == []
    assert splitter.feed(" Anything") == ["Your balance is 500 dollars."]
    assert splitter.flush() == "Anything"


def test_a_decimal_point_is_never_a_sentence_end() -> None:
    splitter = SentenceSplitter()

    assert splitter.feed("That's 120.") == []
    assert splitter.feed("50 dollars. Next") == ["That's 120.50 dollars."]


def test_a_line_break_ends_a_sentence() -> None:
    assert SentenceSplitter().feed("Main account\nSavings account\n") == ["Main account", "Savings account"]


def test_a_long_sentence_is_cut_at_its_last_clause_break() -> None:
    clauses = "you can ask about your balance, " * 7  # over the soft limit, no full stop
    assert len(clauses) > SOFT_LIMIT
    splitter = SentenceSplitter()

    sentences = splitter.feed(clauses + "or about a payment")

    assert sentences == [clauses.strip()]
    assert splitter.flush() == "or about a payment"


def test_a_short_sentence_is_never_cut_at_a_comma() -> None:
    assert SentenceSplitter().feed("Yes, your main account, the US dollar one, is active") == []


def test_a_long_sentence_without_a_clause_break_waits() -> None:
    assert SentenceSplitter().feed("a" * (SOFT_LIMIT + 50)) == []


def test_flush_returns_nothing_when_nothing_is_left() -> None:
    splitter = SentenceSplitter()
    splitter.feed("Done. ")

    assert splitter.flush() is None
    assert SentenceSplitter().flush() is None
    whitespace = SentenceSplitter()
    whitespace.feed("   \n")
    assert whitespace.flush() is None


def test_after_a_flush_the_splitter_starts_clean() -> None:
    splitter = SentenceSplitter()
    splitter.feed("Is there anything else")
    assert splitter.flush() == "Is there anything else"

    assert splitter.flush() is None
    assert splitter.feed("Goodbye. ") == ["Goodbye."]
