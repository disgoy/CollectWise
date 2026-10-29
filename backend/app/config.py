import os

# Settings come from environment variables so each environment
# (local, tests, later production) can differ without code changes.
DEFAULT_DATABASE_URL = "sqlite:///./collectwise.db"

DATABASE_URL = os.environ.get("COLLECTWISE_DATABASE_URL", DEFAULT_DATABASE_URL)

# Secret for signing caller session tokens (D39). No default: a missing secret
# must fail loudly, never fall back to a guessable value.
SESSION_TOKEN_SECRET = os.environ.get("COLLECTWISE_SESSION_SECRET")
SESSION_TOKEN_TTL_SECONDS = int(os.environ.get("COLLECTWISE_SESSION_TTL_SECONDS", "3600"))
