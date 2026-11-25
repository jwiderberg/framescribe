"""The measurement scripts must not be able to download the speech model: every one that loads it first goes through
the same offline guard `run` uses, which fails with the `framescribe setup` message when the model is not cached."""

import ast
import os
from pathlib import Path

import pytest

from framescribe import doctor

SCRIPTS = sorted((Path(__file__).parent.parent / "scripts").glob("*.py"))
GUARD = "require_cached_model"
LOADERS = {"from_pretrained", "snapshot_download", "hf_hub_download", "download_model"}


def called_names(tree: ast.AST) -> list[tuple[str, int]]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if name:
                out.append((name, node.lineno))
    return out


def test_there_are_scripts_to_check():
    assert {p.name for p in SCRIPTS} >= {"asr_memory.py", "asr_chunking.py", "asr_casing.py"}


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_a_script_that_loads_the_model_calls_the_offline_guard_first(script):
    calls = called_names(ast.parse(script.read_text()))
    loads = [line for name, line in calls if name in LOADERS]
    guards = [line for name, line in calls if name == GUARD]
    if loads:
        assert guards, f"{script.name} loads the model without the offline guard"
        assert min(guards) < min(loads), f"{script.name} calls the guard after the first loader call"


def test_every_script_that_names_a_loader_is_covered_by_the_check():
    assert any(name in LOADERS for p in SCRIPTS for name, _ in called_names(ast.parse(p.read_text())))


@pytest.fixture
def clean_offline_flag(monkeypatch):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    yield
    os.environ.pop("HF_HUB_OFFLINE", None)  # the guard sets it for the process; do not leak it into other tests


def test_the_guard_stops_with_the_setup_message_when_the_model_is_not_cached(monkeypatch, clean_offline_flag):
    monkeypatch.setattr(doctor, "_model_files", lambda repo: None)
    with pytest.raises(SystemExit) as stop:
        doctor.require_cached_model("owner/model")
    assert "framescribe setup" in str(stop.value) and "speech model" in str(stop.value)
    assert os.environ["HF_HUB_OFFLINE"] == "1"  # offline even when it stops


def test_the_guard_passes_and_stays_offline_when_the_model_is_cached(monkeypatch, tmp_path, clean_offline_flag):
    monkeypatch.setattr(doctor, "_model_files", lambda repo: {"model.safetensors": tmp_path / "model.safetensors"})
    assert doctor.require_cached_model("owner/model") is None
    assert os.environ["HF_HUB_OFFLINE"] == "1"
