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
