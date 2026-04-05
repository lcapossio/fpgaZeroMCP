# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""
Background build manager for long-running EDA tool invocations.

Starts builds as subprocesses, streams output to log files, and provides
status/progress queries so the caller can poll periodically.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from tools.workspace import temporary_workspace


@dataclass
class BuildRecord:
    build_id: str
    label: str
    cmd: list[str]
    log_path: str
    work_dir: str
    start_time: float
    end_time: float | None = None
    returncode: int | None = None
    _process: subprocess.Popen | None = field(default=None, repr=False)

    @property
    def elapsed_s(self) -> float:
        end = self.end_time or time.time()
        return round(end - self.start_time, 1)

    @property
    def status(self) -> str:
        if self._process is None:
            return "unknown"
        if self.returncode is not None:
            return "success" if self.returncode == 0 else "failed"
        return "running"

    def tail(self, lines: int = 30) -> str:
        """Return the last N lines of the build log (efficient seek from end)."""
        try:
            size = os.path.getsize(self.log_path)
            # For small files, just read all
            if size < 64 * 1024:
                with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                    return "".join(f.readlines()[-lines:])
            # For large files, seek from end
            chunk_size = min(size, lines * 200)  # ~200 bytes per line estimate
            with open(self.log_path, "rb") as f:
                f.seek(max(0, size - chunk_size))
                data = f.read().decode("utf-8", errors="replace")
            return "".join(data.splitlines(keepends=True)[-lines:])
        except FileNotFoundError:
            return ""

    def log_size_kb(self) -> float:
        try:
            return round(os.path.getsize(self.log_path) / 1024, 1)
        except FileNotFoundError:
            return 0.0

    def full_log(self) -> str:
        """Return the entire build log."""
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def to_dict(self, tail_lines: int = 20, parse: bool = True) -> dict:
        from tools.build_parser import parse_build_log

        result = {
            "build_id": self.build_id,
            "label": self.label,
            "status": self.status,
            "elapsed_s": self.elapsed_s,
            "returncode": self.returncode,
            "log_path": self.log_path,
            "log_size_kb": self.log_size_kb(),
            "tail": self.tail(tail_lines),
        }
        if parse:
            result["build_info"] = parse_build_log(self.full_log())
        return result


# Allowed command prefixes for start_build (security allowlist).
# Only EDA-related tools are permitted to prevent arbitrary command execution.
_ALLOWED_COMMANDS = {
    # Synthesis / PnR
    "yosys", "nextpnr-ice40", "nextpnr-ecp5", "nextpnr-nexus", "nextpnr-gowin",
    # Simulation / lint
    "ghdl", "iverilog", "vvp", "verilator", "verible-verilog-lint",
    # Formal verification
    "sby",
    # Programming
    "iceprog", "openFPGALoader", "ecpprog",
}

# Python is handled separately via prefix match to avoid version pinning
_PYTHON_PREFIX = "python"


def _validate_build_cmd(cmd: list[str]) -> str | None:
    """Return an error string if cmd is not in the allowlist, else None."""
    if not cmd:
        return "Empty command."
    binary = os.path.basename(cmd[0]).removesuffix(".exe").removesuffix(".EXE")
    is_python = binary == _PYTHON_PREFIX or binary.startswith(_PYTHON_PREFIX + "3")
    if binary not in _ALLOWED_COMMANDS and not is_python:
        return (
            f"Command '{cmd[0]}' is not in the allowed list. "
            f"Permitted: {sorted(_ALLOWED_COMMANDS)}"
        )
    # Python: restricted to -m with litex modules only
    if is_python:
        if len(cmd) < 3 or cmd[1] != "-m":
            return "Python commands must use '-m <module>' invocation."
        module = cmd[2]
        if not (module.startswith("litex_boards.") or module.startswith("litex.")):
            return f"Python module '{module}' is not allowed. Only litex_boards.* and litex.* are permitted."
    return None


class BuildManager:
    """Manages background builds with log capture and status queries."""

    def __init__(self) -> None:
        self._builds: dict[str, BuildRecord] = {}
        self._lock = threading.Lock()

    def start(
        self,
        cmd: list[str],
        label: str = "",
        work_dir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> dict:
        """Start a build subprocess in the background. Returns build info."""
        err = _validate_build_cmd(cmd)
        if err:
            return {"success": False, "error": err}

        build_id = uuid4().hex[:8]
        log_dir = Path("no_commit") / "builds"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{build_id}.log"

        cwd = work_dir or os.getcwd()

        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)

        try:
            log_file = open(log_path, "w", encoding="utf-8")
            proc = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                env=proc_env,
            )
        except FileNotFoundError:
            log_file.close()
            return {"success": False, "error": f"Command not found: {cmd[0]}"}
        except Exception as e:
            log_file.close()
            return {"success": False, "error": str(e)}

        record = BuildRecord(
            build_id=build_id,
            label=label or " ".join(cmd[:3]),
            cmd=cmd,
            log_path=str(log_path),
            work_dir=cwd,
            start_time=time.time(),
            _process=proc,
        )

        # Monitor thread — waits for process exit and records result
        def _monitor() -> None:
            proc.wait()
            log_file.close()
            record.returncode = proc.returncode
            record.end_time = time.time()

        t = threading.Thread(target=_monitor, daemon=True)
        t.start()

        with self._lock:
            self._builds[build_id] = record

        return {
            "success": True,
            "build_id": build_id,
            "label": record.label,
            "log_path": str(log_path),
            "message": f"Build started. Check with build_status(build_id='{build_id}').",
        }

    def status(self, build_id: str, tail_lines: int = 30, parse: bool = True) -> dict:
        """Return current status and recent log output for a build.

        Set parse=False to skip full-log parsing (faster for frequent polling).
        """
        with self._lock:
            record = self._builds.get(build_id)
        if not record:
            return {"error": f"Unknown build_id: '{build_id}'"}
        return record.to_dict(tail_lines, parse=parse)

    def list_builds(self) -> list[dict]:
        """Return summary of all tracked builds (no full log parsing for speed)."""
        with self._lock:
            records = list(self._builds.values())
        return [r.to_dict(tail_lines=5, parse=False) for r in records]

    def cancel(self, build_id: str) -> dict:
        """Kill a running build."""
        with self._lock:
            record = self._builds.get(build_id)
        if not record:
            return {"error": f"Unknown build_id: '{build_id}'"}
        if record.status != "running":
            return {"error": f"Build '{build_id}' is not running (status: {record.status})."}

        proc = record._process
        if proc is None:
            return {"error": f"Build '{build_id}' has no associated process."}
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        record.returncode = proc.returncode
        record.end_time = time.time()

        return {
            "success": True,
            "build_id": build_id,
            "message": "Build cancelled.",
            "elapsed_s": record.elapsed_s,
        }

    def clear_finished(self) -> dict:
        """Remove completed builds from the tracker."""
        with self._lock:
            finished = [bid for bid, r in self._builds.items() if r.status != "running"]
            for bid in finished:
                del self._builds[bid]
        return {"cleared": len(finished), "remaining": len(self._builds)}

    @staticmethod
    def cleanup_logs(max_age_days: int = 7, max_total_mb: int = 500) -> dict:
        """Delete old build logs to reclaim disk space.

        Removes logs older than max_age_days, then trims by size
        (oldest first) until total size is under max_total_mb.
        """
        log_dir = Path("no_commit") / "builds"
        if not log_dir.exists():
            return {"deleted": 0, "freed_kb": 0}

        import time as _time
        cutoff = _time.time() - max_age_days * 86400
        logs = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime)

        deleted = 0
        freed = 0

        # Phase 1: delete old logs
        for log in logs:
            try:
                st = log.stat()
                if st.st_mtime < cutoff:
                    freed += st.st_size
                    log.unlink()
                    deleted += 1
            except OSError:
                continue

        # Phase 2: trim by total size
        remaining = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in remaining)
        max_bytes = max_total_mb * 1024 * 1024
        for log in remaining:
            if total <= max_bytes:
                break
            try:
                sz = log.stat().st_size
                log.unlink()
                total -= sz
                freed += sz
                deleted += 1
            except OSError:
                continue

        return {
            "deleted": deleted,
            "freed_kb": round(freed / 1024, 1),
        }
