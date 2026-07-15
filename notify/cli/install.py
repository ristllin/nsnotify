"""nimbus-notify installer — wire `led-report` hooks into your AI coding harness.

The #1 onboarding failure: a `pip install` user got neither the plugin slash
command nor the `hooks/` config files on disk, so nothing ever wired `led-report`
into their harness and the device stayed dark. This command fixes that WITHOUT the
plugin: it merges the correct hooks into your harness config idempotently, keeping
any hooks you already have.

Usage:
    nimbus-notify install-hooks [--harness claude|codex|vibe|all] [--dry-run]
    nimbus-notify doctor

`install-hooks` fully automates the JSON surfaces (Claude `settings.json`, Codex
`hooks.json`) — it APPENDS our hook groups, never replaces your arrays, and skips
events already wired (safe to re-run). TOML toggles (Codex `config.toml`, Vibe) are
printed for you to paste, because the stdlib has no comment-preserving TOML writer.
`doctor` reports whether the broker is running, hooks are wired, and the device is
reachable — read-only.
"""
from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Canonical hook wiring — THE source of truth for the installer.
# tests/test_installer.py asserts this reproduces hooks/claude/settings.json and
# hooks/codex/hooks.json byte-for-byte, so the plugin path and the pip path can't
# drift. (verb list is authoritative: notify/broker/session.py::_VERB_TO_STATE.)
# ---------------------------------------------------------------------------

# (event, matcher-or-None, verb)
CLAUDE_HOOKS = [
    ("SessionStart", None, "start"),
    ("UserPromptSubmit", None, "running"),
    ("PreToolUse", "*", "running"),
    ("Notification", "*", "notify"),   # NOT PermissionRequest — Claude Code never emits that
    ("Stop", None, "done"),
    ("StopFailure", None, "error"),
    ("SessionEnd", None, "end"),
]

# (event, matcher-or-None, verb) — Codex uses the same nested group shape as Claude,
# but commands carry `timeout` (not async/--pid). PermissionRequest -> approval is a
# real Codex event (unlike Claude, which never emits it).
CODEX_HOOKS = [
    ("SessionStart", "startup", "start"),
    ("UserPromptSubmit", None, "running"),
    ("PreToolUse", "*", "running"),
    ("PermissionRequest", None, "approval"),
    ("Stop", None, "done"),
    ("SessionEnd", None, "end"),
]


def _claude_group(verb: str, matcher: str | None) -> dict:
    grp: dict = {}
    if matcher is not None:
        grp["matcher"] = matcher
    grp["hooks"] = [{
        "type": "command",
        "command": f"led-report claude {verb} --pid $PPID",
        "async": True,
    }]
    return grp


def build_claude_hooks() -> dict:
    """The `hooks` block we merge into ~/.claude/settings.json."""
    hooks: dict = {}
    for event, matcher, verb in CLAUDE_HOOKS:
        hooks.setdefault(event, []).append(_claude_group(verb, matcher))
    return hooks


def _codex_group(verb: str, matcher: str | None) -> dict:
    grp: dict = {}
    if matcher is not None:
        grp["matcher"] = matcher
    grp["hooks"] = [{
        "type": "command",
        "command": f"led-report codex {verb}",
        "timeout": 5,
    }]
    return grp


def build_codex_hooks() -> dict:
    """The hook map we merge into ~/.codex/hooks.json."""
    hooks: dict = {}
    for event, matcher, verb in CODEX_HOOKS:
        hooks.setdefault(event, []).append(_codex_group(verb, matcher))
    return {"hooks": hooks}


# ---------------------------------------------------------------------------
# Idempotent JSON merge
# ---------------------------------------------------------------------------

def _group_is_ours(group: dict, harness: str) -> bool:
    if not isinstance(group, dict):
        return False
    for h in group.get("hooks", []):
        if isinstance(h, dict) and str(h.get("command", "")).startswith(f"led-report {harness}"):
            return True
    return False


def _merge_hooks_block(existing: dict, ours: dict, harness: str):
    """Append our per-event groups into `existing['hooks']`, preserving unrelated
    hooks and skipping any event already wired to led-report. Returns (added,
    skipped) event-name lists. Mutates `existing`."""
    dst = existing.setdefault("hooks", {})
    added, skipped = [], []
    for event, groups in ours.items():
        arr = dst.setdefault(event, [])
        if not isinstance(arr, list):
            skipped.append(event)
            continue
        if any(_group_is_ours(g, harness) for g in arr):
            skipped.append(event)
            continue
        arr.extend(groups)
        added.append(event)
    return added, skipped


def _write_json_config(path: Path, ours_hooks: dict, harness: str, dry_run: bool) -> bool:
    """Merge `ours_hooks` (an {event: [group,...]} map) into the JSON config at
    `path`. Returns True if a write happened (or would, under --dry-run)."""
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text() or "{}")
        except json.JSONDecodeError as e:
            print(f"  ! {path} is not valid JSON ({e}); refusing to touch it.")
            return False
    before = json.dumps(existing, indent=2, sort_keys=True)
    added, skipped = _merge_hooks_block(existing, ours_hooks, harness)
    after = json.dumps(existing, indent=2, sort_keys=True)

    if not added:
        print(f"  = {path}: already wired ({', '.join(skipped) or 'nothing to do'}).")
        return False
    print(f"  + {path}: adding {', '.join(added)}"
          + (f"  (kept {', '.join(skipped)})" if skipped else ""))
    if dry_run:
        diff = difflib.unified_diff(before.splitlines(), after.splitlines(),
                                    fromfile=str(path), tofile=str(path) + " (new)", lineterm="")
        print("\n".join("      " + ln for ln in diff))
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        bak.write_text(path.read_text())
        print(f"    (backed up -> {bak})")
    path.write_text(json.dumps(existing, indent=2) + "\n")
    return True


# ---------------------------------------------------------------------------
# Per-harness install
# ---------------------------------------------------------------------------

def install_claude(dry_run: bool) -> None:
    print("Claude Code (~/.claude/settings.json):")
    _write_json_config(Path.home() / ".claude" / "settings.json",
                       build_claude_hooks(), "claude", dry_run)


def install_codex(dry_run: bool) -> None:
    print("Codex (~/.codex/hooks.json):")
    _write_json_config(Path.home() / ".codex" / "hooks.json",
                       build_codex_hooks()["hooks"], "codex", dry_run)
    print("Codex — add this to ~/.codex/config.toml (paste; stdlib can't safely edit TOML):\n")
    print("    [features]")
    print("    hooks = true\n")
    print("  (Do NOT also set `notify = [\"led-report\", \"codex-notify\"]` — the legacy notify")
    print("   program keys sessions by the per-turn turn-id, so with hooks on it spawns a")
    print("   SECOND, duplicate ring segment every turn. hooks.json already covers Codex.)\n")


def install_vibe(dry_run: bool) -> None:
    print("Mistral Vibe — paste into ~/.vibe/config.toml (needs Vibe v2.15.0+):\n")
    print("    enable_experimental_hooks = true\n")
    print("  and merge hooks/vibe/hooks.toml (before_tool/after_tool/post_agent_turn).")
    print("  Vibe has no start/stop hook — the broker's session watcher supplies those.")


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def _hooks_wired(path: Path, harness: str) -> bool:
    if not path.exists():
        return False
    try:
        return f"led-report {harness}" in path.read_text()
    except OSError:
        return False


def doctor() -> int:
    print("nimbus-notify doctor\n" + "-" * 20)
    ok = True

    # Broker socket
    try:
        from notify.broker.server import SOCKET_PATH, _socket_alive
        if SOCKET_PATH.exists() and _socket_alive(SOCKET_PATH):
            print(f"  [ok]   broker socket live: {SOCKET_PATH}")
        elif SOCKET_PATH.exists():
            print(f"  [warn] stale socket (broker not running): {SOCKET_PATH}")
            ok = False
        else:
            print("  [warn] broker not running — start it: nimbus-notify-broker "
                  "(USB) / --transport ble (Bluetooth)")
            ok = False
        status = SOCKET_PATH.parent / "status.json"
        print(f"  [{'ok' if status.exists() else '..'}]   status.json: "
              f"{status if status.exists() else '(none yet — no events seen)'}")
    except Exception as e:  # pragma: no cover - defensive
        print(f"  [warn] could not inspect broker: {e}")
        ok = False

    # Hooks wired?
    checks = [
        ("claude", Path.home() / ".claude" / "settings.json"),
        ("codex", Path.home() / ".codex" / "hooks.json"),
        ("vibe", Path.home() / ".vibe" / "hooks.toml"),
    ]
    any_wired = False
    for harness, path in checks:
        wired = _hooks_wired(path, harness)
        any_wired = any_wired or wired
        if path.exists():
            print(f"  [{'ok' if wired else 'no'}]   {harness} hooks "
                  f"{'wired' if wired else 'NOT wired'}: {path}")
    if not any_wired:
        print("  [warn] no harness has led-report hooks — run: nimbus-notify install-hooks")
        ok = False

    print("-" * 20)
    print("All good — start a session and watch the device." if ok
          else "Some checks need attention (see above).")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="nimbus-notify",
                                description="Wire led-report hooks + check your setup.")
    sub = p.add_subparsers(dest="cmd")

    ih = sub.add_parser("install-hooks", help="merge led-report hooks into a harness config")
    ih.add_argument("--harness", choices=["claude", "codex", "vibe", "all"], default="all")
    ih.add_argument("--dry-run", action="store_true", help="print the changes, write nothing")

    sub.add_parser("doctor", help="check broker + hooks + device")

    args = p.parse_args(argv)
    if args.cmd == "doctor":
        return doctor()
    if args.cmd == "install-hooks":
        which = args.harness
        if which in ("claude", "all"):
            install_claude(args.dry_run)
        if which in ("codex", "all"):
            install_codex(args.dry_run)
        if which in ("vibe", "all"):
            install_vibe(args.dry_run)
        if not args.dry_run:
            print("\nDone. Verify:  nimbus-notify doctor")
        return 0
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
