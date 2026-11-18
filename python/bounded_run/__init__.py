"""Run a child command with wall-time, idle-time, output, and tree limits."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Sequence

@dataclass(frozen=True)
class RunResult:
    classification: str
    duration: float
    returncode: int | None
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool


def run(command: Sequence[str], *, timeout: float = 60, idle: float = 20,
        max_output: int = 1_048_576, env: dict[str, str] | None = None) -> RunResult:
    """Execute an absolute or PATH-resolved command with independent stream caps."""
    if not command:
        raise ValueError("command must not be empty")
    if timeout < 0 or idle < 0 or max_output < 0:
        raise ValueError("limits must be nonnegative")
    start = time.monotonic()
    minimal = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    if env is not None: minimal.update(env)
    executable = command[0]
    if not os.path.isabs(executable):
        if "/" in executable or "\\" in executable:
            raise ValueError("relative command paths with a separator are not accepted; use a bare PATH name or an absolute path")
        executable = shutil.which(executable, path=minimal.get("PATH"))
        if executable is None:
            raise FileNotFoundError(f"command not found on PATH: {command[0]}")
        command = (executable, *command[1:])
    proc = subprocess.Popen(command, stdin=None, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=minimal, start_new_session=(os.name == "posix"))
    job = None
    if os.name == "nt":
        try:
            job = _create_windows_job(proc)
        except OSError:
            proc.kill()
            proc.wait()
            raise
    last_output = [start]
    lock = threading.Lock()
    wake = threading.Event()
    child_done = threading.Event()
    reader_done = [False, False]
    streams: list[tuple[bytearray, bool]] = [(bytearray(), False), (bytearray(), False)]
    def drain(pipe, index):
        try:
            while True:
                chunk = os.read(pipe.fileno(), 8192)
                if not chunk:
                    return
                with lock:
                    last_output[0] = time.monotonic()
                    buf, _ = streams[index]
                    room = max_output - len(buf)
                    buf.extend(chunk[:max(room, 0)])
                    if len(chunk) > room:
                        streams[index] = (buf, True)
                wake.set()
        finally:
            with lock:
                reader_done[index] = True
            wake.set()
    threads = [threading.Thread(target=drain, args=(proc.stdout, 0), daemon=True),
               threading.Thread(target=drain, args=(proc.stderr, 1), daemon=True)]
    for t in threads: t.start()
    def wait_child():
        proc.wait()
        child_done.set()
        wake.set()
    waiter = threading.Thread(target=wait_child, daemon=True)
    waiter.start()
    classification = "ok"
    killed = False
    while True:
        wake.clear()
        with lock:
            complete = child_done.is_set() and all(reader_done)
            last = last_output[0]
        if complete:
            code = proc.returncode
            if not killed:
                classification = "ok" if code == 0 else ("signal" if code < 0 else "failed")
            break
        now = time.monotonic()
        if not killed and timeout and now - start >= timeout:
            classification = "timeout"; _terminate(proc, job); killed = True
        elif not killed and idle and now - last >= idle:
            classification = "idle"; _terminate(proc, job); killed = True
        if not killed:
            remaining = []
            if timeout: remaining.append(start + timeout - now)
            if idle: remaining.append(last + idle - now)
            wake.wait(max(0, min(remaining)) if remaining else None)
        else:
            wake.wait()
    for t in threads: t.join()
    waiter.join()
    with lock: out, out_truncated = streams[0]; err, err_truncated = streams[1]
    if job is not None: _close_windows_job(job)
    return RunResult(classification, time.monotonic()-start, proc.returncode, bytes(out), bytes(err), out_truncated, err_truncated)


def _windows_api():
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _create_windows_job(proc: subprocess.Popen):
    import ctypes
    kernel32 = _windows_api()
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel32.AssignProcessToJobObject(job, ctypes.c_void_p(proc._handle)):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel32.CloseHandle(job)
        raise error
    return job


def _close_windows_job(job) -> None:
    _windows_api().CloseHandle(job)


def _terminate(proc: subprocess.Popen, job=None) -> None:
    if os.name == "posix":
        try: os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError: return
        deadline = time.monotonic() + .2
        while time.monotonic() < deadline:
            try: os.killpg(proc.pid, 0)
            except ProcessLookupError: return
            time.sleep(.005)
        try: os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError: pass
    else:
        if job is None:
            proc.kill()
        else:
            _windows_api().TerminateJobObject(job, 1)

__all__ = ["RunResult", "run"]
