from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def article_root(tmp_path, monkeypatch):
    root = tmp_path / "Article"
    (root / "template").mkdir(parents=True)
    monkeypatch.setenv("NOVEL_WRITER_ARTICLE_ROOT", str(root))
    monkeypatch.setenv("NOVEL_WRITER_TEMPLATE_DIR", str(root / "template"))
    return root
