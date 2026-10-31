# v3 — hardening H3 + H4, Milestone 5.4 and speed step S1 (2026-10-30, widened 2026-11-01)

Not a tuning round: a regression check after two behaviour changes the model can see.

**H3 payment expiry (D73).** A proposed payment can be confirmed only within the next 2 caller
turns and 5 minutes; otherwise `confirm_payment` fails with `payment_expired`. The
`confirm_payment` tool description gains one sentence telling the model to say the request
timed out and offer to set it up again.

**H4 fixed-reply notes (D74).** Fixed replies the caller heard but the model never wrote are
prefixed to the next caller message as a bracketed note. The system prompt gains one line
explaining the note.

**M5.4 interruptions and the read-back rule (D82).** The H4 prompt line now also says a
note may report that the customer heard only part of a reply they interrupted. A payment
whose read-back was interrupted fails `confirm_payment` with `readback_not_heard`. Typed
turns (all eval turns) always count as fully heard, so this path can't trigger here.

**S1 words before a lookup (D90).** One prompt line: before a tool, say a few neutral words
that you're checking ("One moment, let me check that."), once per reply, never a fact, result
or decision. These words appear in the judge's transcript as an assistant line before the tool
call (the typed reply returned to the app is still the last call's text). Watch
`grounded`, `no_paid_claim`, `plain_language`, `responsive` and `no_overpromise` for these lines.

**Expected effect on this eval:** none on outcomes. Every payment case confirms one turn after
its last proposal, and fixed replies only occur on outages (which stop the attempt). What can
change: the model reads a longer system prompt and one longer tool description, so phrasing
and the D56 judge call may move. Compare all_pass, outcome, compliance and the quality criteria
against v2 (98%, 49/50).

Harness: run_eval.py and grading.py changed in H1 (shared price table, public
`is_transient_error`); grading logic is unchanged. Needs `--approve-harness` once.

Patch: `change.patch` (git diff c0a82f0 -- backend/app/agent backend/app/tools, working tree: H3, H4, M5.1 streaming, M5.4, S1).
