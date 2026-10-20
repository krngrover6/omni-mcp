from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Offline by default: runtime then uses the fake scene and NullKnowledgeBackend and never probes 127.0.0.1:9910.
os.environ["OMNIVERSE_SCENE_MCP_TEST"] = "1"

try:
    import pytest
except ImportError:  # the stdlib runner does not load conftest; stay importable anyway
    pytest = None
else:

    @pytest.fixture(autouse=True)
    def _offline_runtime(monkeypatch):
        monkeypatch.setenv("OMNIVERSE_SCENE_MCP_TEST", "1")
