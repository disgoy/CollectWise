# Data handling (Milestone 6.3)

Every place CollectWise keeps or sends data: what it holds, what it never
holds, and how long it stays. All data is synthetic. Each claim names the test
that proves it; `test_every_data_handling_claim_names_a_test_that_exists`
fails if a named test is renamed or removed.

## Inside CollectWise

| Where | Holds | Never holds | Kept | Proven by |
|---|---|---|---|---|
| Conversation sessions (server memory) | The caller's words and the agent's replies for this conversation, so the model has context | — | Until 15 minutes idle, a newer conversation replaces it, or the server restarts; never written to disk | `test_a_session_expires_after_the_idle_timeout`, `test_starting_a_session_sweeps_expired_ones` |
| `conversation_events` table | Session and customer ids, turn numbers, event codes, tool names, outcomes, timings, token counts | Message text, tool arguments or results | 30 days, then removed by the purge | `test_events_hold_no_message_text_or_tool_arguments`, `test_old_telemetry_is_purged_and_the_newest_kept` |
| `live_turn_metrics`, `voice_turn_metrics` tables | Timings, confidence scores, outcome codes, character counts | Transcripts or replies | 30 days, then removed by the purge | `test_each_turn_records_timings_and_never_words`, `test_metrics_hold_no_words`, `test_old_telemetry_is_purged_and_the_newest_kept` |
| `servicing_requests`, `servicing_audit_events` tables | Payment requests: customer, account, amount, the policy decision, and its audit trail | Anything the caller said | Not purged: business records under a separate, longer records policy | `test_the_purge_never_touches_servicing_records` |
| Logs | Ids, codes, counts, timings; an exception as its types and code locations | Caller words, replies, tool data, database values, exception messages, client IP addresses, request bodies from the AI or speech SDKs | Wherever the server's output goes | `test_no_log_line_holds_the_callers_words_or_account_data`, `test_a_database_error_names_the_statement_not_its_values`, `test_an_exception_is_logged_as_types_and_locations`, `test_noisy_libraries_stay_quiet_whatever_their_settings`, `test_uvicorn_lines_lose_the_client_address` |
| Traces (off unless `COLLECTWISE_TRACING` is set) | Codes, counts, timings, session ids | Caller words, replies, tool data, customer ids, exception messages | Wherever the trace viewer keeps them | `test_no_span_holds_the_callers_words_the_reply_or_tool_data`, `test_an_exception_keeps_its_type_and_never_its_message` |
| Evidence report (`evidence/`, public) | Aggregates: rates, counts, timings, phrase ids and the fact heard | Transcripts, prompts, replies, judge reasoning | In the repository | `test_a_full_build_holds_no_transcript_prompt_or_judge_reasoning` |
| Voice check recordings and their results | The author's own voice and its transcripts | — | Only on the author's machine, in a git-ignored folder; never on the server | — |

## Sent to other services

| Service | Receives | Not sent | Their retention |
|---|---|---|---|
| Anthropic (Claude) | The conversation so far, the system prompt and tool results, to answer the caller | Trace context (`test_no_span_holds_the_callers_words_the_reply_or_tool_data`) | Anthropic's API data policy |
| Deepgram (speech) | The caller's audio, and the text of each reply to speak | — | Deepgram's policy; live listening opts out of model improvement (`test_flux_is_opened_with_our_settings`) |

## Purging old telemetry

```bash
python -m scripts.purge_old_data
```

```bash
python -m scripts.purge_old_data --yes
```

The first only counts what it would delete; the second deletes conversation
events and timing rows older than `COLLECTWISE_TELEMETRY_RETENTION_DAYS`
(30). Run `python -m scripts.build_evidence` first if the evidence report
should keep the calls about to go.

## Not covered

Logs written by anything we don't configure (a different server command, a
hosting platform's own request logs), and how long Anthropic and Deepgram keep
what they receive, which is their policy.
