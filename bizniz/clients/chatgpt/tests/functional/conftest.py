import os
import pytest

from dotenv import load_dotenv

# Load .env from the examples directory or project root
load_dotenv()
load_dotenv(os.path.join(os.path.dirname(__file__), "../../../../..", "examples", ".env"))


def get_api_key():
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        pytest.skip("OPENAI_API_KEY not set — skipping functional tests")
    return key


@pytest.fixture
def api_key():
    return get_api_key()
