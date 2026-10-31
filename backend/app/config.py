import os

# Settings come from environment variables so each environment
# (local, tests, later production) can differ without code changes.
DEFAULT_DATABASE_URL = "sqlite:///./collectwise.db"

DATABASE_URL = os.environ.get("COLLECTWISE_DATABASE_URL", DEFAULT_DATABASE_URL)

# Secret for signing caller session tokens (D39). No default: a missing secret
# must fail loudly, never fall back to a guessable value.
SESSION_TOKEN_SECRET = os.environ.get("COLLECTWISE_SESSION_SECRET")
SESSION_TOKEN_TTL_SECONDS = int(os.environ.get("COLLECTWISE_SESSION_TTL_SECONDS", "3600"))

# Demo sign-in (D84) for trying live calls locally: off unless set to "1",
# and only for these synthetic customers. Anyone who can reach a server with
# it on can act as them, so never switch it on anywhere public.
DEMO_LOGIN = os.environ.get("COLLECTWISE_DEMO_LOGIN") == "1"
DEMO_CUSTOMERS = tuple(
    customer.strip()
    for customer in os.environ.get("COLLECTWISE_DEMO_CUSTOMERS", "cust_demo").split(",")
    if customer.strip()
)

# Key for the back-office API: /customers, /accounts, /servicing (D70). No
# default: unset means those routes refuse every request.
ADMIN_API_KEY = os.environ.get("COLLECTWISE_ADMIN_KEY")

# Conversation lifetime and limits (D72). Conversations live in memory:
# idle ones expire, each customer and the server hold only so many, a long
# conversation is handed to a person, and each customer's turns and new
# conversations are rate limited so nobody can run up model and speech costs.
SESSION_IDLE_MINUTES = float(os.environ.get("COLLECTWISE_SESSION_IDLE_MINUTES", "15"))
MAX_SESSIONS_PER_CUSTOMER = int(os.environ.get("COLLECTWISE_MAX_SESSIONS_PER_CUSTOMER", "3"))
MAX_SESSIONS = int(os.environ.get("COLLECTWISE_MAX_SESSIONS", "1000"))
MAX_CALLER_TURNS = int(os.environ.get("COLLECTWISE_MAX_CALLER_TURNS", "30"))
TURNS_PER_MINUTE = int(os.environ.get("COLLECTWISE_TURNS_PER_MINUTE", "10"))
SESSION_STARTS_PER_MINUTE = int(os.environ.get("COLLECTWISE_SESSION_STARTS_PER_MINUTE", "5"))

# A proposed payment can be confirmed only while it's fresh (D73): within this
# many further caller turns and minutes. After that the agent proposes it
# again, which reads it back again.
PAYMENT_CONFIRM_TURNS = int(os.environ.get("COLLECTWISE_PAYMENT_CONFIRM_TURNS", "2"))
PAYMENT_CONFIRM_MINUTES = float(os.environ.get("COLLECTWISE_PAYMENT_CONFIRM_MINUTES", "5"))

# The agent's LLM. Off unless explicitly set to "anthropic", so nothing calls
# the API by accident (tests never set it).
AGENT_LLM = os.environ.get("COLLECTWISE_AGENT_LLM")
AGENT_MODEL = os.environ.get("COLLECTWISE_AGENT_MODEL", "claude-sonnet-5-5")
# Claude Sonnet 5.5 starting point for multi-step tool use; measure in evals.
AGENT_EFFORT = os.environ.get("COLLECTWISE_AGENT_EFFORT", "medium")
# The longest the agent waits on Claude for the next piece of a reply, and how
# often a failed call is retried (M6.1). The SDK's defaults (10 minutes, 2
# retries) could leave a caller in silence for minutes; this way the caller
# hears "having trouble" after about 30 seconds at most.
AGENT_TIMEOUT_S = float(os.environ.get("COLLECTWISE_AGENT_TIMEOUT_S", "15"))
AGENT_MAX_RETRIES = int(os.environ.get("COLLECTWISE_AGENT_MAX_RETRIES", "1"))
# Logs (M6.3): "text" (default) or "json", one object per line.
LOG_FORMAT = os.environ.get("COLLECTWISE_LOG_FORMAT", "text")
# Conversation events and voice/live timing rows older than this are deleted
# by scripts/purge_old_data.py (M6.3). Servicing records are never purged.
TELEMETRY_RETENTION_DAYS = int(os.environ.get("COLLECTWISE_TELEMETRY_RETENTION_DAYS", "30"))
# Tracing (M6.2): unset = off, "console" = spans printed, "otlp" = sent to
# OTEL_EXPORTER_OTLP_ENDPOINT (default http://localhost:4318).
TRACING = os.environ.get("COLLECTWISE_TRACING")
# How long a typed message waits for the conversation's previous turn to
# finish before it is refused with 409 (M6.1).
TURN_LOCK_WAIT_S = float(os.environ.get("COLLECTWISE_TURN_LOCK_WAIT_S", "5"))

# Transcripts the speech service is less sure of than this get "say that
# again" instead of reaching the agent (D60). 0.6 from the 4.7 voice checks
# (D69): no correctly heard clip scored below 0.75, two mishearings did.
VOICE_MIN_CONFIDENCE = float(os.environ.get("COLLECTWISE_VOICE_MIN_CONFIDENCE", "0.6"))

# Speech services. Off unless explicitly set to "deepgram", so nothing calls
# Deepgram by accident (tests remove it). The key is DEEPGRAM_API_KEY.
SPEECH_PROVIDER = os.environ.get("COLLECTWISE_SPEECH")
STT_MODEL = os.environ.get("COLLECTWISE_STT_MODEL", "nova-3")
TTS_VOICE = os.environ.get("COLLECTWISE_TTS_VOICE", "aura-2-thalia-en")

# Live (streaming) listening for real-time calls (M5): Deepgram Flux. The
# turn ends when Flux is this confident the caller has finished, or after
# this much silence.
LIVE_STT_MODEL = os.environ.get("COLLECTWISE_LIVE_STT_MODEL", "flux-general-en")
EOT_THRESHOLD = float(os.environ.get("COLLECTWISE_EOT_THRESHOLD", "0.7"))
EOT_TIMEOUT_MS = int(os.environ.get("COLLECTWISE_EOT_TIMEOUT_MS", "5000"))
# Live turns heard with a lower mean word confidence get "say that again".
# Provisional: D69's 0.6 was measured on nova-3's per-utterance confidence,
# not Flux's per-word scores; re-measure with a real voice (5.7).
LIVE_MIN_CONFIDENCE = float(os.environ.get("COLLECTWISE_LIVE_MIN_CONFIDENCE", "0.6"))
# Live calls end after this long, or after this long without the caller's
# words (D82): streaming speech-to-text bills by the minute, silences included.
LIVE_MAX_CALL_MINUTES = float(os.environ.get("COLLECTWISE_LIVE_MAX_CALL_MINUTES", "15"))
LIVE_SILENCE_MINUTES = float(os.environ.get("COLLECTWISE_LIVE_SILENCE_MINUTES", "2"))
