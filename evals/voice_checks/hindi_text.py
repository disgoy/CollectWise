"""Reading Hindi and Hinglish transcripts for the voice checks (M7.6): words in
Devanagari or Latin letters, and amounts written as digits (Latin or
Devanagari) or as Hindi number words, such as "do hazaar", "dedh lakh" or
"साढ़े तीन सौ".

For scoring only: the app never reads amounts itself (the model does, and the
policy decides). Only the number words listed here are known; anything else
is not a number, so an unknown word scores as misheard rather than as a
guess.
"""

import re
import unicodedata
from decimal import Decimal

_NUKTA = "़"
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
# Digits (with thousands commas and decimals), Latin words, Devanagari words
# (without the danda, which ends a sentence).
_TOKEN = re.compile(r"[0-9][0-9,]*(?:\.[0-9]+)?|[a-z']+|[ऀ-ॣ०-ॿ]+")


def normalize(text: str) -> str:
    """Lowercase, one spelling for nukta and nasal variants (हज़ार = हजार,
    पाँच = पांच), and Devanagari digits as Latin ones."""
    text = unicodedata.normalize("NFD", text.lower()).replace(_NUKTA, "").replace("\u0901", "\u0902")
    return unicodedata.normalize("NFC", text).translate(_DEVANAGARI_DIGITS)


def words(text: str) -> list[str]:
    return [token for token in _TOKEN.findall(normalize(text)) if not token[0].isdigit()]


def _spellings(table: dict[str, str]) -> dict[str, Decimal]:
    return {normalize(word): Decimal(value) for value, spellings in table.items() for word in spellings.split()}


# Romanized "do" (2) is also the verb "do" ("kar do"), and "saath" (60) also
# means "with": "do" counts only next to a multiplier or a fraction word, and
# romanized 60 isn't listed (Devanagari साठ and साथ differ).
NUMBERS = _spellings({
    "1": "ek एक", "2": "do दो", "3": "teen tin तीन", "4": "char chaar चार", "5": "paanch panch पांच",
    "6": "chhah chhe छह छः छे", "7": "saat सात", "8": "aath आठ", "9": "nau नौ", "10": "das दस",
    "11": "gyarah gyaarah ग्यारह", "12": "barah baarah बारह", "13": "terah तेरह", "14": "chaudah चौदह",
    "15": "pandrah पंद्रह पन्द्रह", "16": "solah सोलह", "17": "satrah सत्रह", "18": "atharah athaarah अठारह",
    "19": "unnis unees उन्नीस", "20": "bees बीस", "25": "pachchees pachees पच्चीस", "30": "tees तीस",
    "40": "chalees chaalis चालीस", "50": "pachaas pachas पचास", "60": "साठ", "70": "sattar सत्तर",
    "75": "pachhattar पचहत्तर", "80": "assi अस्सी", "90": "nabbe नब्बे",
})
# Within a group: X sau. Groups: X hazaar, X lakh, X crore.
HUNDRED = _spellings({"100": "sau सौ"})
SCALES = _spellings({"1000": "hazaar hazar hajar हजार", "100000": "lakh laakh lac लाख",
                     "10000000": "crore karod karor करोड"})
# Whole numbers on their own: dedh = 1.5, dhai = 2.5.
FRACTIONS = _spellings({"1.5": "dedh derh डेढ", "2.5": "dhai dhaai dhaayi adhai ढाई"})
# Before a number: saadhe teen = 3.5, sava do = 2.25, paune do = 1.75.
ADJUSTERS = _spellings({"0.5": "saadhe sadhe saade साढे", "0.25": "sava sawa सवा", "-0.25": "paune pone पौने"})
_TWO = {normalize("do"), normalize("दो")}


def _is_number_word(tokens: list[str], i: int, after_adjuster: bool) -> bool:
    token = tokens[i]
    if token in _TWO:
        following = tokens[i + 1] if i + 1 < len(tokens) else ""
        return after_adjuster or following in HUNDRED or following in SCALES
    return token in NUMBERS or token in FRACTIONS


def amounts(text: str) -> list[Decimal]:
    """Every amount in the text, in order. A run of number words and digits is
    one amount ("do hazaar paanch sau" = 2500, "2 हजार" = 2000); separate
    runs, or digits one after another, are separate amounts."""
    tokens = _TOKEN.findall(normalize(text))
    found: list[Decimal] = []
    total = chunk = Decimal(0)
    pending: Decimal | None = None
    adjust: Decimal | None = None
    in_run = False

    def flush() -> None:
        nonlocal total, chunk, pending, adjust, in_run
        if in_run:
            found.append(total + chunk + (pending or 0))
        total = chunk = Decimal(0)
        pending = adjust = None
        in_run = False

    for i, token in enumerate(tokens):
        if token[0].isdigit() or _is_number_word(tokens, i, adjust is not None):
            value = Decimal(token.replace(",", "")) if token[0].isdigit() else (
                NUMBERS.get(token) or FRACTIONS[token])
            if pending is not None:  # two numbers in a row: two amounts
                flush()
            pending = value + (adjust or 0)
            adjust = None
            in_run = True
        elif token in ADJUSTERS:
            adjust = ADJUSTERS[token]  # an amount only once a number or multiplier follows
        elif token in HUNDRED:
            chunk += (pending if pending is not None else 1 + (adjust or 0)) * 100
            pending = adjust = None
            in_run = True
        elif token in SCALES:
            group = chunk + (pending if pending is not None else (1 + (adjust or 0) if not chunk else 0))
            total += group * SCALES[token]
            chunk = Decimal(0)
            pending = adjust = None
            in_run = True
        else:
            flush()
    flush()
    return found
