# Phone call checklist (Milestone 7.5)

A real call from your own phone to the agent on your laptop, through a free
tunnel, and then measured. There's no deployment: the number works only
while the tunnel and the server are running, and when you stop them the
number stops answering. It costs a few cents (Claude and Deepgram) plus
Twilio trial credit, and uses synthetic data only.

**What's exposed:** through the tunnel, only Twilio's two routes answer (the
voice webhook and its media stream). Every other route, including demo
sign-in, the back office, `/live` and the call-watching socket, is a 404 on the
public address. They keep working on `http://127.0.0.1:8000`. The preflight
checks this through your actual tunnel.

## Once

1. **Twilio trial.** Sign up at twilio.com and take the free trial number.
   Under *Verified Caller IDs*, verify the phone you'll call from: a trial
   number accepts calls only from verified numbers.
2. **ngrok.** Sign up, run `ngrok config add-authtoken <your ngrok token>`,
   and claim your free static domain (*Domains* in the ngrok dashboard). The
   address then stays the same on every run.
3. **`.env`.** Add `TWILIO_AUTH_TOKEN=<Auth Token from the Twilio console>`
   with your editor, so it doesn't land in your shell history. Then add the
   address:
   ```bash
   printf '\nCOLLECTWISE_PUBLIC_BASE_URL=https://<your-domain>\n' >> .env
   ```
   `ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY` and `COLLECTWISE_SESSION_SECRET`
   should already be there from the live-call checklist.
4. **Database and your number.** Run `alembic upgrade head`, then
   `python -m scripts.seed_demo --phone +<your number, E.164>`. It asks for a
   6-digit PIN twice and doesn't echo it. The number is stored only in your
   local database.
5. **The webhook.** In the Twilio console, open your number, find
   *A call comes in*, and set it to Webhook, HTTP POST, with the URL the
   preflight prints (`https://<your-domain>/telephony/twilio/voice`).

## Each run (three terminals)

1. `ngrok http --url=https://<your-domain> 8000`. Don't add
   `--host-header=rewrite`: the app tells the public address apart by its
   host name.
2. Start the server:
   ```bash
   set -a; source .env; set +a
   COLLECTWISE_DEMO_LOGIN=1 COLLECTWISE_AGENT_LLM=anthropic COLLECTWISE_SPEECH=deepgram uvicorn backend.app.main:app
   ```
3. Run the preflight (free) and fix anything it marks `FIX`:
   ```bash
   set -a; source .env; set +a
   python -m scripts.phone_preflight
   ```
4. Open http://127.0.0.1:8000/live, the local address, and sign in as the
   demo customer. Don't start a browser call: the page shows the phone call
   when it comes in.

## During: say or do, and note what happens

| # | Say or do | Expect | Tests |
|---|---|---|---|
| 1 | Call the Twilio number from your verified phone | Twilio's trial message; press a key when it asks | the trial account (Twilio's, not ours) |
| 2 | Listen | "Hello, you've reached the CollectWise automated assistant", then a request for your PIN | the webhook, the stream handshake, the disclosure |
| 3 | Type a wrong 6-digit PIN | "Sorry, that didn't match" | verification (the right PIN next resets the count) |
| 4 | Type your PIN | "Thank you, you're verified"; `/live` shows a phone call in progress | keypad timing on a real line, captions |
| 5 | "What's the balance on my main account?" | 500 US dollars, with a caption on `/live` | speed over the phone |
| 6 | Ask something with a long answer, then talk over it | the agent stops at once | barge-in (Twilio `clear`) |
| 7 | "Pay a hundred and twenty dollars from my savings" | read back: 120 US dollars from savings | amounts heard at 8 kHz |
| 8 | Say "yes yes" over that read-back | it reads the payment back again instead of confirming | the read-back rule through Twilio's marks |
| 9 | Let it finish, then "Yes, go ahead" | "recorded" (never "paid") | confirmation |
| 10 | "Can I talk to a person?" | the whole sentence about a member of our team, then the call ends | last words play out (D103) |
| 11 | Optional: call again, verify, and hang up while it's speaking | the server shows the call ending, no error | a caller leaving mid-reply |

Note anything slow, wrong or odd. Keep the server's terminal output: it
holds codes and timings, never words, digits or phone numbers.

## After

- Speed and hearing for the phone call:
  `python -m scripts.live_report --minutes 30 --phone`. Without `--phone`,
  a window that also has browser calls shows the headline for each.
- Stop ngrok and the server. The number stops answering.

**Reading the numbers:** the report's timings start when Flux decides you've
finished (add 0.5-1.2 s for that) and stop when our server has the first
audio. The phone line and Twilio's playback add more on top, unmeasured. A
call from India to a US trial number also crosses an international leg.
One call is one sample.

## If something goes wrong

| What you hear or see | Look for in the server's output | What it means |
|---|---|---|
| The trial message, then "an application error has occurred" | `Rejected an unsigned or wrongly signed telephony webhook` | The URL in Twilio isn't exactly the preflight's, or the server's `COLLECTWISE_PUBLIC_BASE_URL` or token differs from Twilio's |
| The same, with no line in the server's output | ngrok shows `503` or nothing | The server started without `.env` loaded, or ngrok isn't running |
| Silence after the trial message, then a hang-up | `Rejected an unsigned or wrongly signed telephony stream` | Twilio signed the stream handshake differently from what the app checks. **Stop and report it; don't switch the check off.** |
| The same | `Rejected a telephony stream pass` | The pass from the webhook expired (60 s) or belongs to another call |
| "Sorry, I couldn't verify you" with the right PIN | `Phone verification failed for …: unknown_number` | Your caller ID didn't match the number you seeded (often the country code): seed it again as Twilio shows it under *Monitor → Calls* |
| The same | `…: no_entry` or `…: locked` | No keys within 15 s; or three wrong PINs locked it for 15 minutes (wait, or seed again) |
| `/live` shows nothing during the call | — | Sign in on `http://127.0.0.1:8000/live` as the demo customer; on the ngrok address the page doesn't exist |

**Privacy:** your number is stored only in your local database, and the
server's logs never show it or the digits you type. Twilio keeps its own
call log (numbers, times) in your Twilio console; no call recording is
switched on. Your voice goes to Deepgram exactly as on browser calls.
