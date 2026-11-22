# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Run a tool so that a timeout or cancel stops everything it started.

subprocess.run(timeout=...) kills only the immediate child. Tools that spawn
their own workers outlive it: sby starts solver processes in separate process
groups, and on Windows `vivado` is vivado.bat, so the child is cmd.exe and
Vivado itself is a grandchild. Those survivors keep computing, and because they
hold the captured stdout/stderr pipes open, run() itself blocks until they
finish -- a 300 s timeout can turn into an hour.

Here the tool runs in its own process group (POSIX session / Windows process
group) and the whole tree is terminated.
(sys.platform, not os.name, throughout -- mypy narrows the former, so each CI
platform only type-checks its own branch.)
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

# How long the tree gets to exit after the polite signal before SIGKILL.
_GRACE_S = 5.0


def group_popen_kwargs() -> dict:
    """Popen kwargs that put the child in its own process group/session."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def terminate_tree(proc: subprocess.Popen) -> None:
    """Terminate a process started with group_popen_kwargs() and all of its
    descendants.

    On Windows, taskkill /T takes down the tree. On POSIX the process group is
    signalled as a whole; members still alive after the grace period get
    SIGKILL even when the leader itself already exited -- a descendant that
    ignores SIGTERM must not keep running (and writing to a log the caller now
    believes is complete).
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
        )
        try:
            proc.wait(timeout=_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=_GRACE_S)
        return

    import signal

    pgid = proc.pid  # start_new_session: the child leads its own group
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()

    deadline = time.monotonic() + _GRACE_S
    while time.monotonic() < deadline:
        proc.poll()  # reap the leader so it doesn't linger as a zombie
        try:
            os.killpg(pgid, 0)  # does any member remain?
        except (ProcessLookupError, PermissionError):
            break
        time.sleep(0.05)
    else:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=_GRACE_S)


def run_tree(
    cmd: list[str],
    *,
    timeout: float,
    cwd: str | None = None,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    """subprocess.run(cmd, capture_output=True, text=True, errors="replace",
    timeout=timeout) -- except that on timeout the whole process tree is
    terminated before TimeoutExpired is raised.

    FileNotFoundError propagates as with subprocess.run.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        cwd=cwd,
        env=env,
        **group_popen_kwargs(),
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate_tree(proc)
        # The tree is gone, so the pipes reach EOF; collect what was written.
        try:
            stdout, stderr = proc.communicate(timeout=_GRACE_S)
        except (subprocess.TimeoutExpired, ValueError):
            stdout, stderr = "", ""
        raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr)
    except BaseException:
        terminate_tree(proc)
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
