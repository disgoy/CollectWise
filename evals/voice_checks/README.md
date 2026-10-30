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
