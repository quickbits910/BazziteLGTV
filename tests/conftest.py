import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch, tmp_path):
    """Point CONFIG_FILE at a nonexistent path so tests never read a real
    /etc/lgtvcontrol/config on the developer's machine."""
    import lgtv
    monkeypatch.setattr(lgtv, "CONFIG_FILE", tmp_path / "no-such-config")
