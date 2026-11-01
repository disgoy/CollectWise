# Voice checks (Milestone 4.7)

Measures whether speech-to-text keeps the facts that matter (amounts, yes/no,
account names, handoff requests), whether the confidence threshold is right,
and how long speech takes. Spends a little Deepgram credit (about $0.10); no
Claude calls.

```bash
set -a; source .env; set +a
COLLECTWISE_SPEECH=deepgram python -m evals.voice_checks.run_voice_checks --recordings evals/voice_checks/recordings
```

Leave out `--recordings` to use only Deepgram's synthetic voices.

## Recording your own voice (optional)

Your recordings are personal data: they stay in `evals/voice_checks/recordings/`,
which git ignores. Never commit them.

1. Record each phrase below as its own clip (Voice Memos or QuickTime is fine),
   speaking naturally. Name each file after the phrase id, e.g. `amount_fifteen.m4a`.
2. Put the `.m4a` files in one folder, then convert them all to the WAV format
   the app accepts (16-bit, mono, 16 kHz):

   ```bash
   mkdir -p evals/voice_checks/recordings
   for f in ~/Desktop/voice-recordings/*.m4a; do afconvert -f WAVE -d LEI16@16000 -c 1 --mix "$f" "evals/voice_checks/recordings/$(basename "${f%.m4a}").wav"; done
   ```

   (Replace `~/Desktop/voice-recordings` with the folder you used.)

You don't need all of them; missing phrases are skipped.

| File name (id) | Say |
|---|---|
| `amount_fifteen` | Please pay fifteen dollars from my main account. |
| `amount_fifty` | Please pay fifty dollars from my main account. |
| `amount_thirteen` | Pay thirteen dollars, please. |
| `amount_thirty` | Pay thirty dollars, please. |
| `amount_dollars_and_cents` | Can you pay one hundred twenty five dollars and fifty cents? |
| `amount_a_hundred_and_twenty` | I'd like to pay a hundred and twenty. |
| `amount_two_thousand_rupees` | Pay two thousand rupees from my savings account. |
| `confirm_yes` | Yes. |
| `confirm_go_ahead` | Yeah, go ahead. |
| `confirm_no` | No. |
| `confirm_cancel` | Nope, cancel that. |
| `confirm_unsure` | Hmm, maybe. I'm not sure. |
| `label_savings` | Use my savings account. |
| `label_current` | From my current account, please. |
| `handoff_person` | Can I talk to a real person? |

## Hindi and Hinglish (Milestone 7.6, part A)

Can a live call hear Hinglish and Indian English, and which voice should
answer? `run_hindi_checks` streams every clip, clean and with noise, into
both listening models the way a browser call hears it (16 kHz): today's
English-only `flux-general-en` and the multilingual `flux-general-multi`,
with the hints Hindi and English. It also checks that multi hears English as
well as en does. Scoring is on meaning, in Devanagari or Latin letters
(`hindi_text.py` reads "do hazaar", "डेढ़ लाख", "२,०००" and the Hindi words
for yes, no and a person). It also writes listening samples to `samples/`:
a payment read-back in Devanagari words, Devanagari with digits and Latin
letters, by two Hindi voices, and in English by two Indian-English voices. Listen
and pick the voice and writing style that sounds right. About $0.40 of
Deepgram credit; no Claude calls.

```bash
set -a; source .env; set +a
COLLECTWISE_SPEECH=deepgram python -m evals.voice_checks.run_hindi_checks --recordings evals/voice_checks/recordings
```

Your recordings matter most here: they're the only real Hinglish voice in
the test. Record the phrases below the same way as the English ones (same
folder, same `afconvert` command). Read them as you'd naturally say them;
the Latin spelling is only a guide. Your English recordings from the table
above are used too, to compare the two models on your English.

| File name (id) | Say |
|---|---|
| `hi_amount_do_hazaar` | Mere savings account se do hazaar rupaye pay kar do. |
| `hi_amount_dedh_sau` | Dedh sau rupaye pay karne hain. |
| `hi_amount_saadhe_teen_sau` | Saadhe teen sau rupaye bhej do. |
| `hi_amount_dhai_hazaar` | Dhai hazaar ka payment kar dijiye. |
| `hi_amount_paanch_sau_pachaas` | Paanch sau pachaas rupaye current account se. |
| `hi_amount_dedh_lakh` | Mujhe dedh lakh rupaye pay karne hain. |
| `hi_amount_english_number` | Two thousand five hundred rupees pay kar do. |
| `hi_confirm_haan` | Haan, kar do. |
| `hi_confirm_haan_ji` | Haan ji, theek hai. |
| `hi_confirm_nahi` | Nahi. |
| `hi_confirm_rehne_do` | Nahi, rehne do, cancel kar do. |
| `hi_confirm_pata_nahi` | Pata nahi, shayad. |
| `hi_label_savings` | Savings wale account se karo. |
| `hi_label_current` | Current account se kar do. |
| `hi_handoff_insaan` | Mujhe kisi insaan se baat karni hai. |
| `hi_handoff_agent` | Kisi agent se baat karwa do. |
