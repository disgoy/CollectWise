# v4 — Hindi conversations, the policy decides every payment, read-backs checked (2026-11-01)

Not a tuning round: a regression check and a first Hindi measurement after the
changes the model can see since v3 (94%, 47/50 — measured with S1, since reverted).

**New cases (8, `hi_*`, Milestone 7.6 D).** Held in Hindi on `cust_priya` (rupees):
a balance, "दो हज़ार" read back then "हां", a decline, "डेढ़ लाख" (over the limit),
an unclear account, "साढ़े तीन सौ", asking for a person, a staff claim against Bob's
account. Caller text is written as the listener writes Hinglish (Devanagari with
English words in Latin). 33 cases × 2.

**Changes the model sees since v3:**

- **S1 reverted (D92).** The "say a few words before a lookup" line is gone.
- **Honest handoff wording (D103).** "Passed to a member of our team", never
  "connecting you"; `handoff_asks_for_person` judge notes follow.
- **Languages (D109, M7.6 B1).** A Hindi conversation gets the English prompt word for
  word plus a language section and Hindi fixed replies. English conversations get the
  same prompt as before apart from the lines below.
- **Devanagari only (D116).** The Hindi section: replies entirely in Devanagari, English
  words too (the voice spells out Latin letters), amounts in digits, keep replying in Hindi.
- **Every payment through the policy (D116, both languages).** "For every payment the
  customer asks for, call propose_payment and let its result decide: never refuse or
  accept a payment yourself… If it isn't clear which account to pay from, ask first."
  (D115: the model refused डेढ़ लाख itself in a live call.)
- **Read-backs in digits, checked (D118, M7.6 C).** The prompt asks for the amount in
  digits; a proposal whose same-turn reply doesn't state its amount in digits fails
  `confirm_payment` with `readback_missing` (tool description updated).

**Harness changes (need `--approve-harness` once):** the runner starts each case in its
language; compliance now also fails when a case's `must_call` tool was never called
(`hi_pay_over_limit` requires `propose_payment`) and when `readback_missing` occurs (a
rule broken, like confirming early); the judge has a ninth criterion, `language`
(Hindi conversations: all Devanagari, amounts in digits; not applicable in English).
The Hindi verdicts are uncalibrated: read them before trusting them.

**Expected effect:** English outcomes unchanged (the oracle already reads back in
digits; the eval's English read-backs usually do). Watch: `readback_missing` (model
writing amounts in words), `hi_pay_over_limit` compliance, `language`, and whether the
payment-policy line changes `pay_two_accounts_which` or the denied cases.

Patch: `change.patch` (git diff 24c27fa -- backend/app/agent backend/app/tools, working
tree; also includes M6 changes the model doesn't see: timeouts, simulated load-test model,
tracing, connection release).
