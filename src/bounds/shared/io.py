"""Shared I/O helpers for commands that read user input — never hang, never silently swallow.

The single home for the ``read_stdin_json`` contract used by the harness hook entry point
(``bounds agent-hook``) and any future command that ingests piped JSON. Centralized so:

* **Never hang.** A blocking stdin read on a pipe that the writer keeps open — the Claude
  Code hook protocol — would pin the process indefinitely. A wall-clock cap via a
  worker thread + ``queue.Queue`` makes the read bounded; on timeout we return ``None``
  so the caller can fail open (a hook must NEVER break the agent's turn).
* **Loud on a real I/O error.** A genuine stdin read error (broken pipe, closed handle)
  is surfaced via stderr so a user running the command directly sees what happened, not
  a silent exit-0.
* **JSON parse failures are the caller's contract.** A stdin that's empty / not JSON
  returns ``{}`` so the hook can treat it as a no-op event (the documented behavior).
  Anything more interesting (parse error) is left to the caller's ``try/except``.

The companion :func:`emit_loud` helper gives every CLI command a one-liner for
"write this warning to stderr, never swallow it" — the fail-loud companion to the
existing ``_run`` catch-all. Use it for recoverable-but-noteworthy conditions a
human should see in a terminal (a malformed hook payload, a network degraded,
etc.) that still want to keep the JSON-first stdout contract clean.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
from typing import TextIO

# Wall-clock cap for reading a single JSON event from stdin. The Claude Code hook
# protocol sends one event per invocation and keeps the pipe open until the child
# exits; a process that waits forever for EOF is a hang, not a read. 5s is well above
# the time a well-formed event needs on any sane machine and well below the harness's
# own 30s hook-timeout default — bounded on both sides.
_STDIN_READ_TIMEOUT_SECONDS = 5


def _read_one_json(stream: TextIO) -> object:
    """Read one complete JSON value without waiting for EOF after that value.

    ``json.load(stream)`` is not sufficient here: it calls ``stream.read()`` and
    therefore waits for EOF. Hook harnesses may keep the pipe open while waiting
    for the child process to exit. Reading one character at a time lets the
    decoder return as soon as one complete top-level value is available.
    """
    decoder = json.JSONDecoder()
    buffer = ""
    while True:
        char = stream.read(1)
        if char == "":
            if not buffer.strip():
                return {}
            value, end = decoder.raw_decode(buffer.lstrip())
            if buffer.lstrip()[end:].strip():
                raise json.JSONDecodeError("extra data", buffer, end)
            return value
        buffer += char
        stripped = buffer.lstrip()
        if not stripped:
            continue
        try:
            value, end = decoder.raw_decode(stripped)
        except json.JSONDecodeError:
            continue
        if stripped[end:].strip():
            raise json.JSONDecodeError("extra data", stripped, end)
        return value


def read_stdin_json(timeout_seconds: float = _STDIN_READ_TIMEOUT_SECONDS) -> object | None:
    """Read one JSON value from stdin, bounded by ``timeout_seconds``.

    Returns ``{}`` for empty or malformed input and ``None`` for timeout or an
    I/O error. The worker is a daemon because Python cannot cancel a blocked
    file-descriptor read portably; process exit cleans it up after a timeout.
    """
    q: queue.Queue[object] = queue.Queue(maxsize=1)

    def _read() -> None:
        try:
            q.put(_read_one_json(sys.stdin))
        except (ValueError, OSError) as exc:
            q.put(exc)

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    try:
        result = q.get(timeout=timeout_seconds)
    except queue.Empty:
        # Loud: a human running this directly deserves to know the hook is wedged.
        # The hook is a hidden internal command, but its fail-open contract is
        # about the *successful* read path — a real hang is a different category
        # of bug, and silencing it would mask the agent-loop problem.
        emit_loud(
            f"stdin read timed out after {timeout_seconds}s "
            "(expected one JSON event; check the hook harness wiring)"
        )
        return None
    if isinstance(result, BaseException):
        if isinstance(result, ValueError):
            emit_loud(f"stdin was not valid JSON ({result}); treating as no event")
        elif isinstance(result, OSError):
            emit_loud(f"could not read stdin ({result}); treating as no event")
        return {}
    return result


def emit_loud(message: str) -> None:
    """Write a recoverable-but-noteworthy message to stderr without touching stdout.

    The JSON-first stdout contract is sacred (one object per command, no interleaved
    prose). Stderr is the only safe place for human-facing warnings that shouldn't
    corrupt the agent-consumable payload — a malformed hook payload, a network
    degraded to slow-mode, a cache write that failed but didn't block the run.

    Single-line, prefix ``bounds:`` so a human tailing stderr can grep for the source.
    The companion to the ``_run`` catch-all, which only fires on an actual crash —
    this is for the "still succeeded, but you should know" cases the current code
    silently drops. Never raises; never writes to stdout.
    """
    if not message:
        return
    print(f"bounds: {message}", file=sys.stderr)


__all__ = ["emit_loud", "read_stdin_json"]
