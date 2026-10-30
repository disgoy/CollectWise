import os

# Settings come from environment variables so each environment
# (local, tests, later production) can differ without code changes.
DEFAULT_DATABASE_URL = "sqlite:///./collectwise.db"

DATABASE_URL = os.environ.get("COLLECTWISE_DATABASE_URL", DEFAULT_DATABASE_URL)

# Secret for signing caller session tokens (D39). No default: a missing secret
# must fail loudly, never fall back to a guessable value.
SESSION_TOKEN_SECRET = os.environ.get("COLLECTWISE_SESSION_SECRET")
SESSION_TOKEN_TTL_SECONDS = int(os.environ.get("COLLECTWISE_SESSION_TTL_SECONDS", "3600"))

# The agent's LLM. Off unless explicitly set to "anthropic", so nothing calls
# the API by accident (tests never set it).
AGENT_LLM = os.environ.get("COLLECTWISE_AGENT_LLM")
AGENT_MODEL = os.environ.get("COLLECTWISE_AGENT_MODEL", "claude-sonnet-5-5")
# Claude Sonnet 5.5 starting point for multi-step tool use; measure in evals.
AGENT_EFFORT = os.environ.get("COLLECTWISE_AGENT_EFFORT", "medium")

# Transcripts the speech service is less sure of than this get "say that
# again" instead of reaching the agent (D60). 0.6 from the 4.7 voice checks
# (D69): no correctly heard clip scored below 0.75, two mishearings did.
VOICE_MIN_CONFIDENCE = float(os.environ.get("COLLECTWISE_VOICE_MIN_CONFIDENCE", "0.6"))

# Speech services. Off unless explicitly set to "deepgram", so nothing calls
# Deepgram by accident (tests remove it). The key is DEEPGRAM_API_KEY.
SPEECH_PROVIDER = os.environ.get("COLLECTWISE_SPEECH")
STT_MODEL = os.environ.get("COLLECTWISE_STT_MODEL", "nova-3")
TTS_VOICE = os.environ.get("COLLECTWISE_TTS_VOICE", "aura-2-thalia-en")
