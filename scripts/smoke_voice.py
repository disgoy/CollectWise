"""Live smoke test of the voice turn: real Deepgram speech and real Claude.

Spends real money (a few cents: Deepgram from the free credit, Claude from
your Anthropic account). Runs only when both COLLECTWISE_AGENT_LLM=anthropic
and COLLECTWISE_SPEECH=deepgram are set, with ANTHROPIC_API_KEY and
DEEPGRAM_API_KEY available. Uses a throwaway database and synthetic data.

The caller's lines are spoken by Deepgram's own TTS, then sent through the
real voice endpoint, so this checks the plumbing end to end. Synthetic speech
is cleaner than a real caller, so it says little about recognition accuracy;
that's step 4.7.

Usage, from the repo root (keys in .env, see .env.example):
    set -a; source .env; set +a
    COLLECTWISE_AGENT_LLM=anthropic COLLECTWISE_SPEECH=deepgram python -m scripts.smoke_voice
"""

import base64
import logging
import os
import secrets
import sys
import tempfile
from pathlib import Path

if os.environ.get("COLLECTWISE_AGENT_LLM") != "anthropic" or os.environ.get("COLLECTWISE_SPEECH") != "deepgram":
    sys.exit(
        "Refusing to run: set COLLECTWISE_AGENT_LLM=anthropic and COLLECTWISE_SPEECH=deepgram "
        "to allow real API calls."
    )
if not os.environ.get("DEEPGRAM_API_KEY"):
    sys.exit("Refusing to run: DEEPGRAM_API_KEY is not set (load .env first).")

# Must be set before the app is imported.
_work_dir = Path(tempfile.mkdtemp(prefix="collectwise-voice-smoke-"))
os.environ["COLLECTWISE_DATABASE_URL"] = f"sqlite:///{_work_dir}/smoke.db"
os.environ.setdefault("COLLECTWISE_SESSION_SECRET", secrets.token_urlsafe(32))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from backend.app import config  # noqa: E402
from backend.app.api.agent import get_tts  # noqa: E402
from backend.app.auth.session_tokens import issue_session_token  # noqa: E402
from backend.app.db import models  # noqa: E402
from backend.app.db.session import SessionLocal  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.voice.audio import write_wav  # noqa: E402

CUSTOMER = "cust_voice_a"

CALLER_LINES = [
    "What's the balance on my main account?",
    "Please pay one hundred and twenty dollars from my main account.",
    "Yes, go ahead.",
    "Can I speak to a person, please?",
]

# Published pay-as-you-go prices (deepgram.com/pricing, checked 2026-10-30)
# and Claude Sonnet 5.5 prices, USD. Estimates only.
DEEPGRAM_STT_PER_MIN = 0.0043  # Nova-3 monolingual, pre-recorded
DEEPGRAM_TTS_PER_1K_CHARS = 0.030  # Aura-2
CLAUDE = {"input": 2.00, "cache_write": 2.50, "cache_read": 0.20, "output": 10.00}  # per MTok


class UsageCollector(logging.Handler):
    """Collects the per-call usage the Claude client logs."""

    def __init__(self) -> None:
        super().__init__()
        self.totals = {key: 0 for key in CLAUDE}

    def emit(self, record: logging.LogRecord) -> None:
        if record.msg.startswith("Claude call:"):
            _, inp, cache_read, cache_write, out = record.args
            self.totals["input"] += inp or 0
            self.totals["cache_read"] += cache_read or 0
            self.totals["cache_write"] += cache_write or 0
            self.totals["output"] += out or 0


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    usage = UsageCollector()
    claude_logger = logging.getLogger("backend.app.agent.claude")
    claude_logger.setLevel(logging.INFO)
    claude_logger.addHandler(usage)

    print(f"Claude: {config.AGENT_MODEL} | Deepgram STT: {config.STT_MODEL} | voice: {config.TTS_VOICE}")
    print(f"Work dir (database and audio): {_work_dir}")

    caller_voice = get_tts()  # the same Deepgram adapter the app uses
    tts_chars = 0

    with TestClient(app) as client:
        client.post("/customers", json={"customer_id": CUSTOMER, "display_name": "Synthetic Customer"})
        client.post("/accounts", json={
            "account_id": "acct_voice_a1", "customer_id": CUSTOMER,
            "balance": "500.00", "currency": "USD", "label": "main",
        })
        token = issue_session_token(
            CUSTOMER, secret=config.SESSION_TOKEN_SECRET.encode("utf-8"), ttl_seconds=600,
        )
        auth = {"Authorization": f"Bearer {token}"}
        session_id = client.post("/agent/sessions", headers=auth).json()["session_id"]

        for number, line in enumerate(CALLER_LINES, start=1):
            spoken = write_wav(caller_voice.synthesize(line))
            tts_chars += len(line)
            (_work_dir / f"turn{number}_caller.wav").write_bytes(spoken)

            response = client.post(
                f"/agent/sessions/{session_id}/voice-turns",
                content=spoken,
                headers={**auth, "Content-Type": "audio/wav"},
            )
            print(f"\n--- Turn {number}")
            print(f"CALLER SAID : {line}")
            if response.status_code != 200:
                print(f"HTTP {response.status_code}: {response.text}")
                return 1
            body = response.json()
            print(f"DEEPGRAM HEARD: {body['transcript']}")
            print(f"AGENT ({'handed off' if body['handed_off'] else 'active'}): {body['reply']}")
            tts_chars += len(body["reply"]) if body["audio"] else 0
            if body["audio"]:
                path = _work_dir / f"turn{number}_reply.wav"
                path.write_bytes(base64.b64decode(body["audio"]))
                print(f"reply audio : {path.name}")
            else:
                print("reply audio : none (text only)")

    with SessionLocal() as db:
        rows = db.scalars(select(models.VoiceTurnMetricRecord).order_by(models.VoiceTurnMetricRecord.metric_id)).all()
        records = db.scalars(select(models.ServicingRequestRecord)).all()

    print("\n--- Voice turn timings (ms)")
    print("  turn outcome           conf   audio_in  stt  agent   tts  total  audio_out")
    stt_seconds = 0.0
    for row in rows:
        stt_seconds += row.audio_in_ms / 1000
        conf = f"{row.confidence:.2f}" if row.confidence is not None else "  - "
        print(f"  {row.turn:>4} {row.outcome:16} {conf:>5} {row.audio_in_ms:>9} {row.stt_ms or 0:>5} "
              f"{row.agent_ms or 0:>6} {row.tts_ms or 0:>5} {row.total_ms:>6} {row.audio_out_ms or 0:>10}")

    print("\n--- Servicing records")
    for record in records:
        print(f"  {record.operation:16} {record.account_id:14} amount={record.amount} {record.decision}/{record.reason}")

    claude_cost = sum(usage.totals[key] * CLAUDE[key] for key in CLAUDE) / 1_000_000
    stt_cost = stt_seconds / 60 * DEEPGRAM_STT_PER_MIN
    tts_cost = tts_chars / 1000 * DEEPGRAM_TTS_PER_1K_CHARS
    print("\n--- Usage (estimates)")
    print(f"  Claude tokens: {usage.totals} -> ~${claude_cost:.4f}")
    print(f"  Deepgram STT: {stt_seconds:.1f} s of audio -> ~${stt_cost:.4f}")
    print(f"  Deepgram TTS: {tts_chars} characters (caller lines + replies) -> ~${tts_cost:.4f}")
    print(f"  Total: ~${claude_cost + stt_cost + tts_cost:.4f} (Deepgram part comes from the free credit)")
    print(f"\nListen to the turn*_reply.wav files in {_work_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
