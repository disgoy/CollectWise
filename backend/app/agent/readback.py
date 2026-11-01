"""Did the read-back state the amount? (M7.6 C)

After proposing a payment the agent reads it back, and only then may the
caller confirm it. The model writes that read-back, so code checks it: the
reply must state the proposed amount in digits. Latin or Devanagari digits,
any comma grouping (2,000 or 1,50,000), with or without decimals; amounts in
words ("दो हज़ार", "two thousand") don't count, because only digits can be
checked exactly. Only the amount is checked, not the currency or account.
"""

import re
from decimal import Decimal

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
# A run of digits with thousands commas and optional decimals; a sentence's
# full stop after it isn't part of it.
_NUMBER = re.compile(r"[0-9][0-9,]*(?:\.[0-9]+)?")


def amounts_in(text: str) -> list[Decimal]:
    """Every number written in digits, in order."""
    found = []
    for match in _NUMBER.findall(text.translate(_DEVANAGARI_DIGITS)):
        digits = match.replace(",", "")
        if digits:
            found.append(Decimal(digits))
    return found


def states_amount(text: str, amount: Decimal) -> bool:
    """True if the text states this amount in digits (by value: 2000 is 2000.00)."""
    return any(found == amount for found in amounts_in(text))
