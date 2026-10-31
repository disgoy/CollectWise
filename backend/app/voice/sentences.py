"""Cut a reply into sentences as its text streams in, so each can be spoken
as soon as it is complete (M5).

A sentence ends at ".", "!" or "?" (plus any closing quote or bracket) that
is followed by whitespace, or at a line break. Punctuation at the very end of
the text so far waits for the next piece: "120." may become "120.50", which
is never split. A sentence that runs past `soft_limit` characters is cut at
its last comma, semicolon or colon, so the first audio isn't held up.
"""

import re

SOFT_LIMIT = 200

_SENTENCE_END = re.compile(r"[.!?]+[\"')\]]*\s+|\n+")
_CLAUSE_END = re.compile(r"[,;:]\s+")


class SentenceSplitter:
    def __init__(self, soft_limit: int = SOFT_LIMIT) -> None:
        self._soft_limit = soft_limit
        self._buffer = ""

    def feed(self, text: str) -> list[str]:
        """Add streamed text; return the sentences it completed, in order."""
        self._buffer += text
        sentences = []
        while True:
            end = _SENTENCE_END.search(self._buffer)
            if end is None and len(self._buffer) > self._soft_limit:
                clauses = list(_CLAUSE_END.finditer(self._buffer))
                end = clauses[-1] if clauses else None
            if end is None:
                break
            sentence = self._buffer[: end.end()].strip()
            self._buffer = self._buffer[end.end():]
            if sentence:
                sentences.append(sentence)
        return sentences

    def flush(self) -> str | None:
        """The text left when the reply ends, if any."""
        rest = self._buffer.strip()
        self._buffer = ""
        return rest or None
