import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import pytest

@pytest.fixture(autouse=True)
def _isolate_wave14_15_paths(tmp_path, monkeypatch):
    import quota_gate
    import orch_ops
    monkeypatch.setattr(quota_gate, "ANTIGRAVITY_COOLDOWN_PATH", tmp_path / "test-agy-cooldown.json")
    monkeypatch.setattr(quota_gate, "ORCHESTRA_DB_PATH", tmp_path / "test-orchestra-missing.db")
    monkeypatch.setattr(orch_ops, "ORCHESTRA_DB_PATH", tmp_path / "test-orchestra-missing.db")
