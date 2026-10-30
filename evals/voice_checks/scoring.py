"""Did the transcript keep the fact that matters? Scores meaning, not wording:
"$15" and "15 dollars" are both a correct 15.

The yes/no/unsure and handoff checks are deliberately simple word lists. They
only judge whether speech-to-text kept the caller's meaning; the agent itself
interprets the words with the model.
"""

import re
import statistics
from decimal import Decimal

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WORD = re.compile(r"[a-z']+")

UNSURE = {"maybe", "unsure", "hmm", "perhaps"}
NO = {"no", "nope", "cancel", "don't", "stop"}
YES = {"yes", "yeah", "yep", "sure", "okay", "ok", "ahead"}
HANDOFF = {"person", "human", "someone", "agent", "representative"}

THRESHOLDS = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)


def amounts_in(text: str) -> list[Decimal]:
    return [Decimal(match.replace(",", "")) for match in _NUMBER.findall(text)]


def classify_confirmation(text: str) -> str:
    lowered = text.lower()
    words = set(_WORD.findall(lowered))
    # "not sure" contains "sure", so doubt wins over agreement.
    if words & UNSURE or "not sure" in lowered:
        return "unsure"
    if words & NO:
        return "no"
    if words & YES:
        return "yes"
    return "none"


def heard_fact(kind: str, text: str) -> str:
    """The fact a transcript conveys, in the phrase file's terms."""
    words = set(_WORD.findall(text.lower()))
    if kind == "amount":
        found = amounts_in(text)
        # Fixed-point text: normalize() alone writes 20 as "2E+1".
        return ",".join(format(amount.normalize(), "f") for amount in found) or "none"
    if kind == "confirm":
        return classify_confirmation(text)
    if kind == "label":
        return ",".join(sorted(words & {"main", "savings", "current"})) or "none"
    if kind == "handoff":
        return "handoff" if words & HANDOFF else "none"
    raise ValueError(f"unknown kind {kind}")


def is_correct(kind: str, expect: str, text: str) -> bool:
    heard = heard_fact(kind, text)
    if kind == "amount":
        # Exactly one number, equal in value: "15" and "15.00" match, "15, 50" doesn't.
        return heard != "none" and "," not in heard and Decimal(heard) == Decimal(expect)
    return heard == expect


def _confidences(rows: list[dict]) -> str:
    values = sorted(r["confidence"] for r in rows if r["confidence"] is not None)
    if not values:
        return "-"
    return f"min {values[0]:.2f}, median {statistics.median(values):.2f}, max {values[-1]:.2f} (n={len(values)})"


def threshold_table(rows: list[dict]) -> list[dict]:
    """For each threshold: correct transcripts it would wrongly block, and
    wrong ones it would let through to the agent."""
    table = []
    for threshold in THRESHOLDS:
        blocked_correct = sum(
            r["correct"] and r["confidence"] is not None and r["confidence"] < threshold for r in rows
        )
        passed_wrong = sum(
            not r["correct"] and (r["confidence"] is None or r["confidence"] >= threshold) for r in rows
        )
        table.append({"threshold": threshold, "blocked_correct": blocked_correct, "passed_wrong": passed_wrong})
    return table


def summarize(rows: list[dict]) -> str:
    """Rows: {source, noise, phrase_id, kind, transcript, confidence, correct, heard, ...}."""
    lines = []
    conditions = sorted({(r["source"] == "you", r["noise"] is not None, r["noise"] or 0) for r in rows},
                        key=lambda c: (c[0], c[1], -c[2]))
    lines.append("Accuracy of the fact that matters, by condition:")
    for is_you, noisy, snr in conditions:
        subset = [r for r in rows if (r["source"] == "you") == is_you and (r["noise"] or 0) == snr
                  and (r["noise"] is not None) == noisy]
        label = ("your voice" if is_you else "synthetic voices") + (f", noise at {snr} dB SNR" if noisy else ", clean")
        by_kind = ", ".join(
            f"{kind} {sum(r['correct'] for r in subset if r['kind'] == kind)}/{sum(r['kind'] == kind for r in subset)}"
            for kind in ("amount", "confirm", "label", "handoff")
            if any(r["kind"] == kind for r in subset)
        )
        lines.append(f"  {label:40} {sum(r['correct'] for r in subset)}/{len(subset)}  ({by_kind})")

    lines.append("Confidence when heard correctly: " + _confidences([r for r in rows if r["correct"]]))
    lines.append("Confidence when heard wrongly:   " + _confidences([r for r in rows if not r["correct"]]))

    lines.append("Threshold check (blocked correct = needless 'say that again'; passed wrong = misheard reaches agent):")
    for entry in threshold_table(rows):
        lines.append(f"  {entry['threshold']:.1f}: blocked correct {entry['blocked_correct']:3}, passed wrong {entry['passed_wrong']:3}")

    wrong = [r for r in rows if not r["correct"]]
    lines.append(f"Wrongly heard ({len(wrong)}):")
    for r in sorted(wrong, key=lambda r: -(r["confidence"] or 0)):
        noise = f"{r['noise']} dB" if r["noise"] is not None else "clean"
        confidence = f"{r['confidence']:.2f}" if r["confidence"] is not None else "-"
        lines.append(f"  conf {confidence}  {r['source']:22} {noise:6} {r['phrase_id']:28} heard {r['heard']!r} in {r['transcript']!r}")
    return "\n".join(lines)
