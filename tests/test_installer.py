"""Tests for `nimbus-notify install-hooks` — the onboarding installer.

The installer embeds the canonical hook wiring (so a pip user needs no files on
disk); these tests assert it (a) reproduces the reference hooks/*.json exactly (no
drift vs the plugin path), (b) preserves the user's pre-existing hooks, and (c) is
idempotent.
"""
import json
from pathlib import Path

from notify.cli import install

REPO = Path(__file__).resolve().parents[1]


def test_claude_matches_reference():
    ref = json.loads((REPO / "hooks" / "claude" / "settings.json").read_text())["hooks"]
    assert install.build_claude_hooks() == ref


def test_codex_matches_reference():
    ref = json.loads((REPO / "hooks" / "codex" / "hooks.json").read_text())["hooks"]
    assert install.build_codex_hooks()["hooks"] == ref


def test_merge_preserves_unrelated_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": "my-existing.sh"}]}]}}))

    install.install_claude(dry_run=False)

    d = json.loads(settings.read_text())
    start = d["hooks"]["SessionStart"]
    cmds = [h["command"] for g in start for h in g["hooks"]]
    assert "my-existing.sh" in cmds                       # unrelated hook survived
    assert "led-report claude start --pid $PPID" in cmds  # ours appended
    assert set(d["hooks"]) >= {"SessionStart", "Stop", "SessionEnd", "Notification"}
    assert (tmp_path / ".claude" / "settings.json.bak").exists()  # backed up


def test_idempotent_reinstall(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    install.install_claude(dry_run=False)
    first = (tmp_path / ".claude" / "settings.json").read_text()
    install.install_claude(dry_run=False)   # re-run
    second = (tmp_path / ".claude" / "settings.json").read_text()
    assert first == second                  # no duplication on re-run


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    install.install_claude(dry_run=True)
    assert not (tmp_path / ".claude" / "settings.json").exists()
