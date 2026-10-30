# v3 — hardening H3 + H4 (2026-10-30)

Not a tuning round: a regression check after two behaviour changes the model can see.

**H3 payment expiry (D73).** A proposed payment can be confirmed only within the next 2 caller
turns and 5 minutes; otherwise `confirm_payment` fails with `payment_expired`. The
`confirm_payment` tool description gains one sentence telling the model to say the request
timed out and offer to set it up again.

**H4 fixed-reply notes (D74).** Fixed replies the caller heard but the model never wrote are
prefixed to the next caller message as a bracketed note. The system prompt gains one line
explaining the note.

**Expected effect on this eval:** none on outcomes. Every payment case confirms one turn after
its last proposal, and fixed replies only occur on outages (which stop the attempt). What can
change: the model reads a longer system prompt and one longer tool description, so phrasing
and the D56 judge call may move. Compare all_pass, outcome, compliance and the quality criteria
against v2 (98%, 49/50).

Harness: run_eval.py and grading.py changed in H1 (shared price table, public
`is_transient_error`); grading logic is unchanged. Needs `--approve-harness` once.

Patch: `change.patch` (git diff c0a82f0 -- backend/app: H3 and H4).
