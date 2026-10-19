"""Tests for the repo .env autoload: every entry point (dashboard,
worker, missions) builds its runtime through build_runtime, so config
in <repo>/.env reaches all of them — no more per-shell export roulette.
Shell environment always wins over the file."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ecosystem.runtime as runtime


@pytest.fixture(autouse=True)
def _restore_env():
    """The loader writes into the real os.environ by design (config is
    process-global). Undo that after each test so later suites keep
    building hermetic in-memory runtimes."""
    before = dict(os.environ)
    yield
    for key in set(os.environ) - set(before):
        os.environ.pop(key, None)
    for key, value in before.items():
        if os.environ.get(key) != value:
            os.environ[key] = value


def _fresh_loader(monkeypatch, tmp_path, content):
    env_file = tmp_path / ".env"
    env_file.write_text(content, encoding="utf-8")
    monkeypatch.setenv("ECOSYSTEM_ENV_FILE", str(env_file))
    monkeypatch.setattr(runtime, "_dotenv_loaded", False)
    return env_file


def test_dotenv_parses_and_fills_gaps(monkeypatch, tmp_path):
    for var in ("ECOSYSTEM_STATE_DB", "SOME_MARKER"):
        monkeypatch.delenv(var, raising=False)
    _fresh_loader(monkeypatch, tmp_path,
                  "# a comment\n"
                  "\n"
                  "ECOSYSTEM_STATE_DB=/tmp/some where.db\n"
                  "SOME_MARKER='quoted value'\n"
                  "NOT_A_PAIR\n")
    runtime._load_dotenv()

    assert os.environ["ECOSYSTEM_STATE_DB"] == "/tmp/some where.db"
    assert os.environ["SOME_MARKER"] == "quoted value"


def test_dotenv_never_overrides_the_shell(monkeypatch, tmp_path):
    monkeypatch.setenv("ECOSYSTEM_STATE_DB", "/shell/wins.db")
    _fresh_loader(monkeypatch, tmp_path,
                  "ECOSYSTEM_STATE_DB=/file/loses.db\n")
    runtime._load_dotenv()

    assert os.environ["ECOSYSTEM_STATE_DB"] == "/shell/wins.db"


def test_dotenv_missing_file_is_silent(monkeypatch, tmp_path):
    monkeypatch.setenv("ECOSYSTEM_ENV_FILE", str(tmp_path / "nope.env"))
    monkeypatch.setattr(runtime, "_dotenv_loaded", False)
    runtime._load_dotenv()  # must not raise


def test_build_runtime_picks_up_state_db_from_dotenv(monkeypatch, tmp_path):
    db = str(tmp_path / "shared.db")
    monkeypatch.delenv("ECOSYSTEM_STATE_DB", raising=False)
    _fresh_loader(monkeypatch, tmp_path, f"ECOSYSTEM_STATE_DB={db}\n")
    rt1 = runtime.build_runtime()
    biz = rt1.businesses.create("Farelune", "web_product")
    monkeypatch.setattr(runtime, "_dotenv_loaded", False)
    rt2 = runtime.build_runtime()
    assert rt2.businesses.get(biz.id) is not None
