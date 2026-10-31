# Failure modes (Milestone 6.1)

What happens when each part of a call breaks: what the caller hears, what is
recorded, and what state is left. Every row names the test that proves it;
`test_every_failure_mode_names_a_test_that_exists` fails if a named test is
renamed or removed (Python tests by name, the page's Node tests by title).
Tests run with fakes shaped like the real SDKs, so they
show our handling, not the exact shape of every real network failure.

"Having trouble" is the fixed reply *"Sorry, I'm having trouble right now.
Please try again in a moment."* Fixed replies are never the model's words, and
the model is told about them on the next turn (D74).

## The model (Claude)

| Failure | The caller hears | Recorded | Left behind | Proven by |
|---|---|---|---|---|
| Down, overloaded or rate-limited before any text | "Having trouble" | `model_call` unavailable; turn counted | The next turn works normally | `test_llm_outage_gives_safe_reply_and_next_turn_still_works`, `test_transient_failures_become_llm_unavailable` |
| Hangs: never answers | "Having trouble" after the timeout: 15 s per attempt, 1 retry (about 30 s at most) | As above | As above | `test_a_model_that_hangs_gives_up_after_the_timeout`, `test_the_shipped_model_timeout_and_retries` |
| Cut off mid-sentence (live) | The sentences already spoken, then "having trouble" | As above | As above | `test_a_fixed_reply_is_spoken_after_a_stream_cut_off` |
| Fails after preparing a payment, before the read-back | "Having trouble"; never the amount | `payment_cancelled` not_read_back | No pending payment: a later "yes" has nothing to confirm | `test_a_model_failure_after_a_proposal_drops_the_unheard_payment` |
| Fails on a later turn, after a payment was read back | "Having trouble" | As above | The read-back payment can still be confirmed | `test_a_model_failure_leaves_an_earlier_read_back_alone` |
| Refuses, or runs out of tokens | "Sorry, I can't help with that here…" | `turn_completed` cannot_help | Nothing changes | `test_unusable_response_gets_a_safe_reply` |
| Returns no words | "Sorry, could you say that again?" | `turn_completed` empty | Nothing changes | `test_empty_final_answer` |
| Keeps calling tools without answering | "Sorry, I wasn't able to finish that. I'm connecting you…" | Handoff (unresolved_issue) | Handed off; an unconfirmed payment is cancelled | `test_model_call_limit_hands_off` |
| Our own request is wrong (400, 401 …) | Nothing: the error is raised, never disguised as an outage | Server error log | — | `test_our_mistakes_are_not_disguised_as_outages`, `test_a_bug_in_our_code_is_not_hidden` |

## Listening (speech-to-text, live)

| Failure | The caller hears | Recorded | Left behind | Proven by |
|---|---|---|---|---|
| Can't start | The call ends at once ("listening stopped working" on the page) | — | Conversation unchanged | `test_listening_that_cant_start_ends_the_call_at_once` |
| Drops mid-call | A goodbye, then the call ends | — | As above | `test_listening_that_fails_mid_call_ends_it_politely`, `test_audio_that_cant_be_sent_to_the_listener_ends_the_call` |
| Stalls: connected but hears nothing | After 2 minutes without words, a goodbye | — | As above | `test_a_call_ends_after_two_minutes_without_the_callers_words` |
| Unsure of the words (low confidence) | "Sorry, I didn't catch that" | Live timing row, outcome `not_understood` | Nothing reaches the agent | `test_the_guard_uses_mean_word_confidence` |

## Speaking (text-to-speech, live)

| Failure | The caller hears | Recorded | Left behind | Proven by |
|---|---|---|---|---|
| Can't start | Nothing; every reply shows as text | — | The call works as text | `test_no_voice_means_a_text_only_call` |
| Drops mid-reply | That reply and later ones show as text | — | As above | `test_a_voice_that_fails_mid_call_leaves_the_rest_as_text`, `test_a_voice_that_drops_mid_reply_leaves_this_and_later_replies_as_text` |
| Stalls: takes the text, sends no audio | Text only; the call carries on after the 60 s audio wait | — | Text shown counts as heard | `test_a_voice_that_stalls_lets_the_call_carry_on_in_text`, `test_a_read_back_shown_as_text_counts_as_heard` |

## The database

| Failure | The caller hears | Recorded | Left behind | Proven by |
|---|---|---|---|---|
| Error inside a tool | The agent explains it couldn't do that | Tool's changes rolled back; `tool_call` internal_error | Session usable | `test_unexpected_tool_error_is_contained_and_rolled_back`, `test_tool_rollback_does_not_erase_events` |
| Error while confirming a payment | The agent says it didn't go through | No payment request | The payment still waits; a retry records exactly one | `test_a_database_failure_while_confirming_records_nothing_and_can_be_retried` |
| Event log can't be written | Nothing different | Logged and rolled back | The turn is unaffected | `test_recording_failure_never_breaks_the_turn` |
| Live timing row can't be written | Nothing different | Logged | The call is unaffected | `test_a_metrics_failure_never_breaks_the_call` |

## The caller's connection

| Failure | The caller hears | Recorded | Left behind | Proven by |
|---|---|---|---|---|
| Hangs up, or the browser disconnects, while the agent is working | Silence at once (the page stops playing and shows "You hung up") | The turn completes | Tools and history complete: a turn is never abandoned halfway | `test_hanging_up_while_the_agent_works_lets_the_turn_finish_first` |
| Hangs up or disconnects after a payment was read back, before "yes" | — | `payment_cancelled` call_ended | No pending payment: it can only be confirmed on the call where it was read back | `test_a_call_that_ends_after_a_read_back_cancels_the_unconfirmed_payment` |
| The call ends after a payment was confirmed | — | The payment request stands | Nothing cancelled | `test_a_payment_confirmed_on_the_call_survives_its_end`, `test_a_call_that_ends_with_nothing_pending_records_no_cancellation` |
| Interrupts the read-back | The agent reads it back again | — | A "yes" can't confirm it (readback_not_heard) | `test_an_interrupted_read_back_cant_be_confirmed` |
| Leaves mid-call, then calls again | A new call works | — | The session is freed only after the last turn finished | `test_leaving_mid_call_frees_the_session_for_a_new_call` |
| Opens a second call on the same conversation | The second is refused (4409) | — | The first call carries on | `test_one_live_call_per_session` |
| The call ends normally (handoff, time limit, silence) | The last reply plays to the end, up to 20 s | — | — | node: "only a normal end that the caller didn't cause lets the audio finish", node: "the last reply keeps playing after the call closes, until its final chunk ends" |
| Sends audio faster than real time, or a broken frame | The call is cut off (4400) | Protocol violation logged (no audio) | — | `test_audio_faster_than_real_time_ends_the_call`, `test_a_bad_audio_frame_ends_the_call` |

## Limits

| Failure | The caller hears | Recorded | Left behind | Proven by |
|---|---|---|---|---|
| 15-minute limit arrives while the agent is working | The reply, then the time-limit message | — | The turn completes first | `test_the_time_limit_waits_for_a_running_turn`, `test_a_call_ends_at_the_time_limit` |
| A second typed message while a turn is running | 409 "Still answering your last message" after at most 5 s | — | Not counted as a turn | `test_a_second_typed_message_during_a_turn_is_refused_after_a_short_wait`, `test_the_typed_route_answers_409_while_a_turn_is_running` |
| Too many turns in a minute | 429 before the model is called | — | — | `test_the_eleventh_turn_in_a_minute_is_refused_before_the_model` |
| 30 turns in one conversation | Passed to a person | `turn_completed` turn_limit | An unconfirmed payment is cancelled | `test_the_turn_limit_cancels_a_pending_payment` |
| A "yes" long after the read-back | The agent says it timed out and offers to set it up again | — | No payment recorded | `test_confirm_three_turns_later_has_expired` |
