import os

# Settings come from environment variables so each environment
# (local, tests, later production) can differ without code changes.
DEFAULT_DATABASE_URL = "sqlite:///./collectwise.db"

DATABASE_URL = os.environ.get("COLLECTWISE_DATABASE_URL", DEFAULT_DATABASE_URL)
