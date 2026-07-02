#!/usr/bin/env python3
"""
Vulnerability Research Shim
Keeps Claude Code running on a long task, waiting out transient errors and
usage limits (single account, no credential rotation).
"""

import subprocess
import time
import os
import re
import json
from pathlib import Path
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────

PROJECT_DIR = os.path.expanduser("~")

RESUME_PROMPT = (
    "Resume your vulnerability research. Search agent memory for context, check Linear for your current task, and continue. Feel free to use subagents, it will speed up the process!"
)

# Patterns checked against each line as it streams — process is killed immediately on match.
USAGE_LIMIT_PATTERNS = [
    r"You've hit your session limit",
]

# Genuine transient infrastructure errors that are safe to wait out and retry.
SERVER_ERROR_PATTERNS = [
    r"error 522",
    r"retry_after",
]

# How long to wait after a usage limit before retrying (seconds)
USAGE_LIMIT_WAIT = 60

# How long to wait after a non-limit exit before restarting (seconds)
RESTART_WAIT = 5

# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def format_stream_line(line: str) -> str | None:
    """Parse a stream-json line and return a human-readable string, or None to skip."""
    try:
        event = json.loads(line)
        etype = event.get("type", "")

        if etype == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text"):
                    return f"[claude] {block['text'].strip()}"
                if block.get("type") == "tool_use":
                    name = block.get("name", "unknown")
                    inp  = block.get("input", {})
                    summary = next(iter(inp.values()), "") if inp else ""
                    if isinstance(summary, str):
                        summary = summary[:120].replace("\n", " ")
                    return f"[tool:{name}] {summary}"

        if etype == "tool_result":
            content = event.get("content", "")
            if isinstance(content, str):
                preview = content[:120].replace("\n", " ")
                return f"[result] {preview}"

        if etype in ("system", "result"):
            msg = event.get("message") or event.get("result") or ""
            if msg:
                return f"[{etype}] {msg}"

    except (json.JSONDecodeError, KeyError):
        stripped = line.strip()
        if stripped:
            return stripped

    return None


def matches_any(line: str, patterns: list[str]) -> bool:
    lowered = line.lower()
    return any(re.search(p, lowered) for p in patterns)


def docker_cleanup() -> None:
    log("Cleaning up Docker containers...")
    result = subprocess.run(["docker", "ps", "-q"], capture_output=True, text=True)
    ids = result.stdout.strip().split()
    if ids:
        subprocess.run(["docker", "stop"] + ids, capture_output=True)
        subprocess.run(["docker", "rm"]   + ids, capture_output=True)
        log(f"Stopped and removed {len(ids)} container(s)")
    else:
        log("No containers running")


def run_claude() -> tuple[str, str]:
    """
    Returns (exit_reason, full_output) where exit_reason is one of:
      'usage_limit'  — rate/session limit detected in stream
      'server_error' — transient server error (522 etc.)
      'clean'        — process exited normally
    """
    log("Launching Claude Code...")
    env = os.environ.copy()

    process = subprocess.Popen(
        [
            "claude",
            "--print",
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
            "--verbose",
            RESUME_PROMPT,
        ],
        cwd=PROJECT_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    all_lines = []
    exit_reason = "clean"

    for line in process.stdout:
        all_lines.append(line)
        formatted = format_stream_line(line)
        if formatted:
            print(formatted, flush=True)

        # Check for usage limit in the raw stream — kill immediately, don't wait for exit
        if matches_any(line, USAGE_LIMIT_PATTERNS):
            log("Usage limit detected in stream — killing process...")
            exit_reason = "usage_limit"
            process.kill()
            break

        if matches_any(line, SERVER_ERROR_PATTERNS):
            log("Server error detected in stream — killing process...")
            exit_reason = "server_error"
            process.kill()
            break

    process.wait()
    return exit_reason, "".join(all_lines)

# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    log("=== Vulnerability research shim starting ===")

    while True:
        docker_cleanup()

        exit_reason, _ = run_claude()
        log(f"Claude Code stopped — reason: {exit_reason}")

        if exit_reason == "usage_limit":
            log(f"Usage limit hit. Waiting {USAGE_LIMIT_WAIT}s before resuming...")
            time.sleep(USAGE_LIMIT_WAIT)

        elif exit_reason == "server_error":
            wait = 120
            log(f"Transient server error. Waiting {wait}s before retrying...")
            time.sleep(wait)

        else:
            log(f"Clean exit. Restarting in {RESTART_WAIT}s...")
            time.sleep(RESTART_WAIT)


if __name__ == "__main__":
    main()
