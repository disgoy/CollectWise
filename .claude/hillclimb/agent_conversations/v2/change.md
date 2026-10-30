# v2: accounts have spoken labels; the agent says the label, never the account ID

Milestone 4.5 (D58, D65). In v1, 33 of 82 agent replies read out an account ID
("acct_alex_main"), which is unspeakable on a call.

- Accounts can have an optional label ("main", "savings"), unique per customer, stored in
  a new `account_labels` table; `list_accounts` returns it next to the account ID.
- Prompt: account IDs are for tools only; call accounts by label (or currency when
  unlabelled); don't repeat an account name the caller gave that isn't theirs.
- Eval world: every account now has a label (cases.json), seeded through the real
  `create_account` service. Grading is unchanged, so v2 is comparable with v1.

Expected: all_pass holds near v1's 96%; account IDs in agent replies fall well below 33/82
(counted from transcripts, outside the grader).
