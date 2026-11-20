# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""
Background build manager for long-running EDA tool invocations.

Starts builds as subprocesses, streams output to log files, and provides
status/progress queries so the caller can poll periodically.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from fpgazero_mcp.paths import data_root

if TYPE_CHECKING:
    from fpgazero_mcp.tools.build_parser import BuildLogParser


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

    # The log is parsed incrementally: each status poll parses only the bytes
    # written since the previous poll, so a poll costs the same at minute 1 and
    # hour 3 of a Vivado run, and counts/tool detection cover the whole log.
    _parser: BuildLogParser | None = field(default=None, init=False, repr=False)
    _parse_offset: int = field(default=0, init=False, repr=False)
    _parse_skipped: int = field(default=0, init=False, repr=False)
    _parse_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    # Read granularity for catching up on new log output.
    _PARSE_CHUNK_BYTES = 4 * 1024 * 1024
    # Most unparsed backlog one poll will work through (~4 s of parsing). If a
    # build is first polled after it has already written more than this, the
    # middle of the log is skipped: the head (tool banner) and the most recent
    # output are what the status needs. Reported as log_bytes_skipped.
    _PARSE_MAX_CATCHUP_BYTES = 8 * 1024 * 1024
    _PARSE_HEAD_BYTES = 64 * 1024

    def build_info(self) -> dict:
        """Parse any new log output and return the structured build info."""
        from fpgazero_mcp.tools.build_parser import BuildLogParser

        with self._parse_lock:
            if self._parser is None:
                self._parser = BuildLogParser()
            try:
                self._advance_parser(self._parser)
            except FileNotFoundError:
                pass
            info = self._parser.result()
        if self._parse_skipped:
            info["log_bytes_skipped"] = self._parse_skipped
        return info

    def _advance_parser(self, parser: BuildLogParser) -> None:
        # The monitor thread sets returncode only after the process has exited
        # and its log is closed, so once it is set the file is complete and a
        # final unterminated line can be parsed too.
        finished = self.returncode is not None
        size = os.path.getsize(self.log_path)
        with open(self.log_path, "rb") as f:
            if size - self._parse_offset > self._PARSE_MAX_CATCHUP_BYTES:
                if self._parse_offset == 0:
                    head = f.read(self._PARSE_HEAD_BYTES)
                    head = head[: head.rfind(b"\n") + 1]
                    parser.feed(head.decode("utf-8", errors="replace"))
                    self._parse_offset = len(head)
                resume = size - self._PARSE_MAX_CATCHUP_BYTES
                # Resume at the start of the next full line.
                f.seek(resume)
                f.readline()
                self._parse_skipped += f.tell() - self._parse_offset
                self._parse_offset = f.tell()
            f.seek(self._parse_offset)
            while True:
                chunk = f.read(self._PARSE_CHUNK_BYTES)
                if not chunk:
                    break
                at_eof = len(chunk) < self._PARSE_CHUNK_BYTES
                if not (at_eof and finished):
                    # Parse up to the last complete line; the rest is picked
                    # up once its newline has been written.
                    cut = chunk.rfind(b"\n") + 1
                    if cut == 0 and at_eof:
                        break
                    if cut:  # else: a chunk-sized line — parse it as is
                        chunk = chunk[:cut]
                    f.seek(self._parse_offset + len(chunk))
                parser.feed(chunk.decode("utf-8", errors="replace"))
                self._parse_offset += len(chunk)

    def to_dict(self, tail_lines: int = 20, parse: bool = True) -> dict:
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
            result["build_info"] = self.build_info()
        return result


# Allowed command prefixes for start_build (security allowlist).
# Only EDA-related tools are permitted to prevent arbitrary command execution.
_ALLOWED_COMMANDS = {
    # Synthesis / PnR
    "yosys",
    "nextpnr-ice40",
    "nextpnr-ecp5",
    "nextpnr-nexus",
    "nextpnr-gowin",
    # Simulation / lint
    "ghdl",
    "iverilog",
    "vvp",
    "verilator",
    "verible-verilog-lint",
    # Formal verification
    "sby",
    # Programming
    "iceprog",
    "openFPGALoader",
    "ecpprog",
    # Vendor flows (batch mode; typically driven by a TCL script)
    "vivado",
}

# Python is handled separately via prefix match to avoid version pinning
_PYTHON_PREFIX = "python"


def _allowed_work_roots() -> list[Path]:
    roots = [Path.cwd().resolve(), Path.home().resolve()]
    env_dirs = os.environ.get("FPGAZERO_ALLOWED_DIRS", "").strip()
    if env_dirs:
        for dirname in env_dirs.split(os.pathsep):
            dirname = dirname.strip()
            if dirname:
                roots.append(Path(dirname).resolve())
    return roots


def _validate_build_cmd(cmd: list[str]) -> str | None:
    """Return an error string if cmd is not in the allowlist, else None."""
    if not cmd:
        return "Empty command."
    binary = os.path.basename(cmd[0])
    stem, ext = os.path.splitext(binary)
    if ext.lower() in _WIN_EXE_SUFFIXES:
        binary = stem
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


# Executable suffixes a Windows command may carry; Vivado is vivado.bat there.
_WIN_EXE_SUFFIXES = (".exe", ".bat", ".cmd")
# cmd.exe parses a batch file's command line itself, so these characters in
# an argument could start a second command (Python's list quoting only
# protects whitespace). Refused for .bat/.cmd launchers.
_CMD_METACHARS = frozenset('&|<>^%!"\r\n')


def _resolve_executable(
    cmd: list[str], path: str | None = None
) -> tuple[list[str], str | None]:
    """Resolve cmd[0] on PATH; returns (command to run, error or None).

    Popen doesn't apply PATHEXT on Windows, so a bare "vivado" (vivado.bat)
    would not be found; shutil.which does. Unresolvable commands are passed
    through unchanged so Popen reports them as not found.
    """
    exe = shutil.which(cmd[0], path=path) or cmd[0]
    if exe.lower().endswith((".bat", ".cmd")):
        for arg in cmd[1:]:
            if _CMD_METACHARS.intersection(arg):
                return cmd, (
                    f"Argument {arg!r} contains a character cmd.exe treats "
                    f"specially, which isn't allowed when the command is a "
                    f"batch file ({os.path.basename(exe)}): "
                    + " ".join(sorted(_CMD_METACHARS - {"\r", "\n"}))
                )
    return [exe, *cmd[1:]], None


def _validate_work_dir(work_dir: str | None) -> str | None:
    """Return an error string if work_dir is outside allowed filesystem roots."""
    if not work_dir:
        return None
    try:
        resolved = Path(work_dir).resolve()
        if not resolved.is_dir():
            return f"work_dir does not exist or is not a directory: {work_dir}"
        for root in _allowed_work_roots():
            if resolved.is_relative_to(root):
                return None
        return (
            f"work_dir is outside allowed directories. "
            f"Got: {resolved}. Set FPGAZERO_ALLOWED_DIRS to add more roots."
        )
    except (OSError, ValueError) as exc:
        return f"Invalid work_dir: {exc}"


def _terminate_tree(proc: subprocess.Popen) -> None:
    """Terminate a build process and all of its children.

    On Windows, proc.terminate() kills only the immediate child, leaving
    grandchildren (yosys/nextpnr spawned by a LiteX build) running; taskkill /T
    takes down the tree. On POSIX the build runs in its own session, so the
    process group can be signalled as a whole.
    (sys.platform, not os.name — mypy narrows the former, so each CI
    platform only type-checks its own branch.)
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
        )
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        return

    import signal

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.wait(timeout=5)


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
            return {"success": False, "error": err, "error_code": "not_allowed"}
        err = _validate_work_dir(work_dir)
        if err:
            return {"success": False, "error": err, "error_code": "path_security"}

        build_id = uuid4().hex[:8]
        log_dir = data_root() / "builds"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{build_id}.log"

        cwd = work_dir or os.getcwd()

        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)

        run_cmd, err = _resolve_executable(cmd, proc_env.get("PATH"))
        if err:
            return {"success": False, "error": err, "error_code": "not_allowed"}

        # Run the build in its own process group/session so cancel() can
        # terminate the whole tree (e.g. LiteX spawning yosys/nextpnr), not
        # just the immediate child.
        popen_kwargs: dict = {}
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        try:
            log_file = open(log_path, "w", encoding="utf-8")
            proc = subprocess.Popen(
                run_cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                env=proc_env,
                **popen_kwargs,
            )
        except FileNotFoundError:
            log_file.close()
            return {
                "success": False,
                "error": f"Command not found: {cmd[0]}",
                "error_code": "tool_not_found",
            }
        except Exception as e:
            log_file.close()
            return {"success": False, "error": str(e), "error_code": "internal_error"}

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
            return {
                "error": f"Unknown build_id: '{build_id}'",
                "error_code": "build_not_found",
            }
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
            return {
                "error": f"Unknown build_id: '{build_id}'",
                "error_code": "build_not_found",
            }
        if record.status != "running":
            return {
                "error": f"Build '{build_id}' is not running (status: {record.status}).",
                "error_code": "invalid_input",
            }

        proc = record._process
        if proc is None:
            return {
                "error": f"Build '{build_id}' has no associated process.",
                "error_code": "internal_error",
            }
        _terminate_tree(proc)
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
        """Delete old build logs and bitstreams to reclaim disk space.

        For each artifact directory (builds/, bitstreams/ under the data
        root): removes files older than max_age_days, then trims by size
        (oldest first) until total size is under max_total_mb.
        """
        deleted = 0
        freed = 0
        for subdir, pattern in (("builds", "*.log"), ("bitstreams", "*")):
            d, f = BuildManager._cleanup_dir(
                data_root() / subdir, pattern, max_age_days, max_total_mb
            )
            deleted += d
            freed += f

        return {
            "deleted": deleted,
            "freed_kb": round(freed / 1024, 1),
        }

    @staticmethod
    def _cleanup_dir(
        target_dir: Path, pattern: str, max_age_days: int, max_total_mb: int
    ) -> tuple[int, int]:
        """Apply the age/size cleanup policy to one directory. Returns (deleted, freed_bytes)."""
        if not target_dir.exists():
            return 0, 0

        import time as _time

        cutoff = _time.time() - max_age_days * 86400
        entries = sorted(
            (p for p in target_dir.glob(pattern) if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )

        deleted = 0
        freed = 0

        # Phase 1: delete old files
        for entry in entries:
            try:
                st = entry.stat()
                if st.st_mtime < cutoff:
                    freed += st.st_size
                    entry.unlink()
                    deleted += 1
            except OSError:
                continue

        # Phase 2: trim by total size
        remaining = sorted(
            (p for p in target_dir.glob(pattern) if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        total = sum(p.stat().st_size for p in remaining)
        max_bytes = max_total_mb * 1024 * 1024
        for entry in remaining:
            if total <= max_bytes:
                break
            try:
                sz = entry.stat().st_size
                entry.unlink()
                total -= sz
                freed += sz
                deleted += 1
            except OSError:
                continue

        return deleted, freed
