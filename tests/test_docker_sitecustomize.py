"""deploy/docker/sitecustomize.py: secrets reach docker-exec scripts too."""

import os
import runpy
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "docker" / "sitecustomize.py"


def test_loads_env_file_like_the_app(tmp_path, monkeypatch):
    env = tmp_path / "ventoryn.env"
    env.write_text("# comment\nVT_A=plain\nVT_B=\"quoted value\"\nexport VT_C='single'\n"
                   "VT_D=from-file\n\nnot a line\n", encoding="utf-8")
    for k in ("VT_A", "VT_B", "VT_C"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VT_D", "real-env-wins")
    monkeypatch.setenv("ECOSYSTEM_ENV_FILE", str(env))
    runpy.run_path(str(SCRIPT))
    assert os.environ["VT_A"] == "plain"
    assert os.environ["VT_B"] == "quoted value"
    assert os.environ["VT_C"] == "single"
    assert os.environ["VT_D"] == "real-env-wins"


def test_missing_file_is_harmless(monkeypatch, tmp_path):
    monkeypatch.setenv("ECOSYSTEM_ENV_FILE", str(tmp_path / "nope.env"))
    runpy.run_path(str(SCRIPT))  # no exception
