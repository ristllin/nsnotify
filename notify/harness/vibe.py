"""
Phase 4 — Mistral Vibe harness adapter.

Two surfaces:

(a) hooks.toml — fires before_tool / after_tool / post_agent_turn.
    These write stdin JSON like other harnesses; verb comes from CLI subcommand.
    Stdin common fields: session_id, parent_session_id, transcript_path, cwd,
    hook_event_name.
    before_tool adds: tool_name, tool_call_id, tool_input
    after_tool  adds: + tool_status, tool_output, tool_error, duration_ms

(b) Session file watcher — Vibe has NO session start/stop hook.
    The watcher (VibeWatcher below) monitors ~/.vibe/logs/session/ and detects
    new sessions by watching for new directories.  It runs as a background
    thread inside the broker and fires events directly on the broker.

Known limitations:
  - HITL (ask_user_question): not exposed by any hook.  The HITL inference
    heuristic is: if before_tool fires but after_tool does NOT arrive within
    HITL_TIMEOUT_S seconds, assume approval is pending → send "hitl_inferred".
  - Free-text question (ask_user_question tool): tail messages.jsonl and detect
    the tool_call record — flagged here but not yet implemented.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from notify.harness.base import HarnessEvent

log = logging.getLogger(__name__)

HITL_TIMEOUT_S = 120.0  # before_tool with no after_tool within this -> infer "awaiting
                        # approval". Was 30 s, which flipped every build/test/long-bash to
                        # a false amber CTA (a coding tool routinely runs minutes; vibe's
                        # own bash default_timeout is 300 s) — more false than true positives.
VIBE_HOME      = Path.home() / ".vibe"
VIBE_SESSIONS  = VIBE_HOME / "logs" / "session"


def parse_stdin() -> dict[str, Any]:
    try:
        return json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError):
        return {}


def build_event(verb: str) -> HarnessEvent:
    """Build a HarnessEvent from a CLI verb + Vibe hook stdin JSON."""
    body = parse_stdin()

    # Collapse after_tool verb: append the tool_status for the broker.
    if verb == "after_tool":
        status = body.get("tool_status", "success")
        verb   = f"after_tool:{status}"

    return HarnessEvent(
        harness=    "vibe",
        session_id= body.get("session_id", ""),
        cwd=        body.get("cwd", ""),
        verb=       verb,
    )


# ---------------------------------------------------------------------------
# Session file watcher (runs inside the broker process, not in led-report)
# ---------------------------------------------------------------------------

EventCallback = Callable[[dict], None]  # same shape as broker.handle_event()


class VibeWatcher:
    """Watches ~/.vibe/logs/session/ for new session directories.

    On new session: fires {"harness":"vibe","session_id":...,"cwd":...,"verb":"start"}.
    On session disappearance: fires verb="end".

    Also runs the HITL inference timer: if a before_tool event arrived but no
    after_tool follows within HITL_TIMEOUT_S, fires verb="hitl_inferred".

    Call start() from the broker setup; the watcher runs in a daemon thread.
    """

    def __init__(self, callback: EventCallback) -> None:
        self._cb      = callback
        self._known:  set[str] = set()  # session dir names we've processed
        self._ended:  set[str] = set()  # dirs we've already fired "end" for (never re-fire)
        self._id_by_dir: dict[str, str] = {}  # dir name -> meta session_id (fixed at first sight)
        self._primed  = False           # first scan seeds the baseline, doesn't light the ring
        self._thread: threading.Thread | None = None
        self._stop    = threading.Event()
        # HITL tracker: session_id → monotonic time of the last before_tool.
        # Touched from BOTH the socket thread (record_*) and the watcher thread
        # (_check_hitl_timeouts) — guard it, or an iteration race silently kills
        # the watcher and vibe detection dies for the broker's lifetime.
        self._lock = threading.Lock()
        self._pending_tool: dict[str, float] = {}

    def start(self) -> None:
        # Only skip when Vibe isn't installed at all.  When Vibe IS present but
        # hasn't created its session dir yet (fresh install, no session run),
        # start anyway — _scan_sessions tolerates the dir appearing later, so
        # the very first Vibe session is still detected.
        if not VIBE_HOME.exists():
            log.debug("vibe not installed (%s absent) — watcher idle", VIBE_HOME)
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="vibe-watcher")
        self._thread.start()
        log.info("VibeWatcher started, watching %s", VIBE_SESSIONS)

    def stop(self) -> None:
        self._stop.set()

    def record_before_tool(self, session_id: str) -> None:
        """Called by the broker when a before_tool event arrives."""
        with self._lock:
            self._pending_tool[session_id] = time.monotonic()

    def record_after_tool(self, session_id: str) -> None:
        """Called by the broker when an after_tool event arrives (or a session frees)."""
        with self._lock:
            self._pending_tool.pop(session_id, None)

    # ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._scan_sessions()
                self._check_hitl_timeouts()
            except Exception:  # a transient FS/JSON race must never kill the watcher
                log.exception("vibe watcher sweep failed (continuing)")
            time.sleep(2.0)

    def _meta_ended(self, name: str) -> bool:
        return bool(self._read_meta(name).get("end_time"))

    def _fire_start(self, name: str, meta: dict) -> None:
        self._cb({
            "harness":    "vibe",
            "session_id": self._id_by_dir.get(name, name),
            "cwd":        meta.get("working_directory", ""),
            "verb":       "start",
        })

    def _fire_end(self, name: str) -> None:
        sid = self._id_by_dir.get(name, name)
        self._cb({"harness": "vibe", "session_id": sid, "cwd": "", "verb": "end"})
        self._ended.add(name)
        self.record_after_tool(sid)

    def _scan_sessions(self) -> None:
        try:
            current = {d.name for d in VIBE_SESSIONS.iterdir()
                       if d.is_dir() and not d.is_symlink()}   # skip the .last_session symlink
        except OSError:
            return

        # New dirs. On the FIRST scan we seed a baseline: every pre-existing dir is
        # recorded as known WITHOUT lighting the ring UNLESS it's genuinely live
        # (no end_time — a session that outlived a broker restart). This is the fix
        # for the ring flooding with every historical session on each broker (re)start.
        for name in current - self._known:
            self._known.add(name)
            meta = self._read_meta(name)
            self._id_by_dir[name] = meta.get("session_id", name)
            if meta.get("end_time"):                # already finished (historical / backfilled)
                self._ended.add(name)
                continue
            self._fire_start(name, meta)            # live: light it (first scan or later)
        self._primed = True

        # End detection: dirs never disappear in practice, so drive "end" off
        # meta.json gaining a non-null end_time (keyed by the mapped session_id,
        # not the dir name — the old code freed the wrong key, so nothing cleared).
        for name in list(self._known - self._ended):
            if name not in current or self._meta_ended(name):
                self._fire_end(name)

    def _read_meta(self, dir_name: str) -> dict:
        try:
            text = (VIBE_SESSIONS / dir_name / "meta.json").read_text()
            return json.loads(text)
        except (OSError, json.JSONDecodeError):
            return {}

    def _check_hitl_timeouts(self) -> None:
        now = time.monotonic()
        with self._lock:
            expired = [sid for sid, t in self._pending_tool.items()
                       if now - t > HITL_TIMEOUT_S]
            for sid in expired:
                del self._pending_tool[sid]
        for sid in expired:
            log.debug("vibe HITL inferred for session %s", sid)
            self._cb({"harness": "vibe", "session_id": sid, "cwd": "", "verb": "hitl_inferred"})
