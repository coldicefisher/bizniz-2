import os
import shutil
import uuid
import pytest
from pathlib import Path
from dotenv import load_dotenv

# Search for .env in examples/ and project root
_project_root = Path(__file__).resolve().parents[4]
load_dotenv(_project_root / "examples" / ".env")
load_dotenv(_project_root / ".env")
load_dotenv()

_BIZNIZ_TMP = Path.home() / ".bizniz" / "tmp"


@pytest.fixture
def api_key():
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        pytest.skip("OPENAI_API_KEY not set — skipping functional tests")
    return key


@pytest.fixture
def workspace_path():
    ws = _BIZNIZ_TMP / uuid.uuid4().hex[:12]
    ws.mkdir(parents=True, exist_ok=True)
    yield ws
    shutil.rmtree(ws, ignore_errors=True)
