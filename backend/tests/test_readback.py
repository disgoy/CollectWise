"""Finding the amount in a read-back (M7.6 C): digits only, either script,
any grouping, by value."""

from decimal import Decimal

import pytest

from backend.app.agent.readback import amounts_in, states_amount


@pytest.mark.parametrize("text", [
    "I've prepared a payment request for 120.00 US dollars from your main account.",
    "That's 120 dollars from main, shall I submit it?",
    "That's $120, shall I submit it?",
    "मैं आपकी सेविंग्स अकाउंट से 120.00 रुपये की रिक्वेस्ट तैयार कर रहा हूँ।",
    "१२०.०० रुपये",
    "१२० रुपये",
])
def test_the_amount_in_digits_counts(text) -> None:
    assert states_amount(text, Decimal("120.00"))


@pytest.mark.parametrize(("text", "amount"), [
    ("2,000.00 रुपये", "2000.00"),
    ("₹2,000", "2000.00"),
    ("२,००० रुपये", "2000.00"),
    ("1,50,000.00 रुपये", "150000.00"),
    ("150,000.00 रुपये", "150000.00"),
    ("125.50 US dollars.", "125.50"),
    ("125.5 dollars", "125.50"),
])
def test_any_grouping_and_decimals(text, amount) -> None:
    assert states_amount(text, Decimal(amount))


@pytest.mark.parametrize(("text", "amount"), [
    ("दो हज़ार रुपये", "2000.00"),  # words don't count
    ("two thousand rupees", "2000.00"),
    ("200.00 रुपये", "2000.00"),  # a different amount
    ("2,000.00 रुपये", "200.00"),
    ("125.50 dollars", "125.00"),
    ("Your balance is 500.00 and the request is ready.", "120.00"),
    ("", "120.00"),
])
def test_words_or_other_numbers_dont(text, amount) -> None:
    assert not states_amount(text, Decimal(amount))


def test_numbers_are_read_in_order_without_trailing_punctuation() -> None:
    assert amounts_in("Pay 2,000.00. Then 15, and 1,50,000.") == [Decimal("2000.00"), Decimal("15"), Decimal("150000")]
