import os
import sys
import tempfile
from pathlib import Path

# Tests never call OpenAI: force the local pseudo model before anything reads the settings.
os.environ["LLM_BACKEND"] = "pseudo"
os.environ["OPENAI_API_KEY"] = ""
os.environ["QUEUE_IMMEDIATE"] = "1"  # Huey runs each task inside the request: no consumer needed
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="pdf-translator-tests-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


@pytest.fixture
def samples() -> Path:
    return SAMPLES

from app.ai.setup import configure_sdk  # noqa: E402
from app.config import Settings  # noqa: E402

configure_sdk(Settings())
