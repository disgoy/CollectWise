# v1: request IDs never reach the model; prompt rules for named accounts and handoffs

Targets the three failures that repeated across both baseline reps:

1. **Request IDs read aloud (10/50 baseline rows, `no_internal_ids`).** Code change:
   `confirm_payment`, `list_recent_payment_requests` and `get_request_status` no longer
   return `request_id`, so the model can't say an ID it never sees (D55). The
   `get_request_status` description now points at caller-supplied IDs, and at
   `list_recent_payment_requests` for payments made in the conversation.
2. **Trying an account the caller names (compliance 2/2).** Prompt rule: only use account
   IDs from a `list_accounts` result in this conversation; otherwise say you can only help
   with accounts on their sign-in, without saying whether that account exists or whose it is.
3. **Promising what the human will do (1/2).** Prompt rule: after handing off, say they're
   being connected to a member of the team; don't say what that person will do or decide.

Unchanged: model, effort, policy, loop, the eval (cases, grader, judge).
Caveat: tuned after reading the same cases it's scored on; rules 1 and 3 came from the
smoke test, before the eval existed.
