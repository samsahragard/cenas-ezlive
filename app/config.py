import os
from dotenv import load_dotenv

load_dotenv(override=True)


class Config:
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
    ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    ANTHROPIC_MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS", "4096"))


STORE_ADDRESSES = {
    "store_1": "15650 FM 529 Rd, Houston, TX 77095",
    "store_2": "27727 Tomball Pkwy, Tomball, TX 77375",
}
