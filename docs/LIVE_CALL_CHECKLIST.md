# Live call checklist (Milestone 5.7)

A short, repeatable trial of the live voice agent with real services. It
costs a few cents and uses synthetic data only. Nothing you say is stored by
CollectWise; your voice goes to Deepgram with model-improvement opt-out on.

## Before

1. `.env` has `ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY` and
   `COLLECTWISE_SESSION_SECRET` (32+ random characters).
2. `python -m scripts.seed_demo`
3. `set -a; source .env; set +a`
4. `COLLECTWISE_DEMO_LOGIN=1 COLLECTWISE_AGENT_LLM=anthropic COLLECTWISE_SPEECH=deepgram uvicorn backend.app.main:app`
5. Open http://127.0.0.1:8000/live in Chrome, with headphones.

## During: say or do, and note what happens

| # | Say or do | Expect | Tests |
|---|---|---|---|
| 1 | Sign in as the demo customer, Start call, allow the mic | "Listening" | sign-in, mic, connection |
| 2 | "What's the balance on my main account?" | live caption, then the answer spoken (500 USD) | speed, captions |
| 3 | "And my savings?" | 900 USD | account names heard |
| 4 | Ask something with a long answer, then say "Stop, the other account" while it speaks | the agent goes quiet at once; the reply is marked "(interrupted)" | barge-in |
| 5 | "Pay a hundred and twenty dollars from my savings" | read back: 120.00 US dollars from savings | amounts, read-back |
| 6 | Say "yes yes" over that read-back | it reads the payment back again instead of confirming | the read-back rule (D82) |
| 7 | Let it finish, then "Yes, go ahead" | "recorded" (never "paid") | confirmation |
| 8 | Mumble something | "Sorry, I didn't catch that" | the guard |
| 9 | Stay silent for a minute | nothing happens | silence is ignored |
| 10 | Take the headphones off, ask a question, let it answer | it should not interrupt itself | the echo check |
| 11 | "Can I talk to a person?" | handed off; the call ends | handoff |

Note anything slow, wrong or odd, and keep the server's terminal output
(it holds codes and timings, never words).

## After

- Speed and hearing for the call: `python -m scripts.live_report --minutes 30`
- The live confidence threshold, from your recordings and one synthetic voice
  heard through Flux (a few cents):
  `COLLECTWISE_SPEECH=deepgram python -m evals.voice_checks.run_voice_checks --live --recordings evals/voice_checks/recordings`

The report's timings start when Flux decides you've finished; add Flux's own
end-of-turn lag (0.5-1.2 s measured) for what you experience.
