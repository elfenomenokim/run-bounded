"""Run a child command with wall-time, idle-time, output, and tree limits."""
from __future__ import annotations

import math
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Sequence

__version__ = "0.1.7"


@dataclass(frozen=True)
class RunResult:
    classification: str
    duration: float
    returncode: int | None
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    cleanup: str | None = None
    abandoned_pid: int | None = None
    limit: str | None = None


# After a deadline kill, a descendant outside the process group may still hold a
# stream open. Forward for this long, then report the limit instead of waiting.
DRAIN_GRACE = 0.1


class _Stopped(BaseException):
    """Raised in the main thread when the process receives SIGINT, SIGTERM or SIGHUP."""

    def __init__(self, signum):
        self.signum = signum


def _install_stop_handlers():
    """Temporarily turn stop signals into an exception so the child tree can be killed."""
    if os.name != "posix" or threading.current_thread() is not threading.main_thread():
        return {}
    previous = {}

    def stop(signum, _frame):
        raise _Stopped(signum)

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            previous[signum] = signal.signal(signum, stop)
        except (OSError, ValueError):
            pass
    return previous


def _restore_stop_handlers(previous) -> None:
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except (OSError, ValueError):
            pass


def _resolve_stdin(stdin: object, input_data: bytes | str | None) -> tuple[object, bytes | None]:
    """Resolve Popen stdin target and optional bytes to feed before closing."""
    if input_data is not None:
        if stdin is not None and stdin not in ("null", subprocess.DEVNULL):
            raise ValueError("cannot pass both stdin and input")
        if isinstance(input_data, str):
            raw = input_data.encode("utf-8")
        elif isinstance(input_data, (bytes, bytearray, memoryview)):
            raw = bytes(input_data)
        else:
            raise TypeError("input must be bytes or str")
        if not raw:
            return subprocess.DEVNULL, None
        return subprocess.PIPE, raw
    if stdin is None or stdin == "null" or stdin == subprocess.DEVNULL:
        return subprocess.DEVNULL, None
    if stdin == "inherit":
        return None, None
    if isinstance(stdin, int) and not isinstance(stdin, bool) and stdin >= 0:
        return stdin, None
    if hasattr(stdin, "fileno"):
        return stdin, None
    raise ValueError(f"invalid stdin mode: {stdin!r}")


def run(
    command: Sequence[str],
    *,
    timeout: float = 60,
    idle: float = 20,
    idle_stream: str = "either",
    max_output: int = 1_048_576,
    max_stdout: int | None = None,
    max_stderr: int | None = None,
    max_total_output: int | None = None,
    kill_on_cap: bool = False,
    capture_overflow_byte: bool = False,
    term_grace: float = 0.2,
    no_reap: bool = False,
    cwd: str | os.PathLike[str] | None = None,
    stdin: object = None,
    input: bytes | str | None = None,
    env: dict[str, str] | None = None,
    env_exact: bool = False,
) -> RunResult:
    """Execute an absolute or PATH-resolved command with optional shared limits."""
    if not command:
        raise ValueError("command must not be empty")
    if idle_stream not in ("either", "stdout"):
        raise ValueError("idle_stream must be 'either' or 'stdout'")
    if not math.isfinite(term_grace) or term_grace < 0:
        raise ValueError("term_grace must be finite and nonnegative")
    if (
        timeout < 0
        or idle < 0
        or max_output < 0
        or (max_stdout is not None and max_stdout < 0)
        or (max_stderr is not None and max_stderr < 0)
        or (max_total_output is not None and max_total_output < 0)
    ):
        raise ValueError("limits must be nonnegative")
    caps = (
        max_output if max_stdout is None else max_stdout,
        max_output if max_stderr is None else max_stderr,
    )
    popen_stdin, input_bytes = _resolve_stdin(stdin, input)
    popen_cwd = os.fspath(cwd) if cwd is not None else None
    start = time.monotonic()
    child_env = {}
    if not env_exact:
        child_env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    if env is not None:
        child_env.update(env)
    executable = command[0]
    if not os.path.isabs(executable):
        if "/" in executable or "\\" in executable:
            raise ValueError("relative command paths with a separator are not accepted; use a bare PATH name or an absolute path")
        search_path = child_env.get("PATH", os.defpath if env_exact else None)
        executable = shutil.which(executable, path=search_path)
        if executable is None:
            raise FileNotFoundError(f"command not found on PATH: {command[0]}")
        command = (executable, *command[1:])
    job = _new_windows_job() if os.name == "nt" else None
    try:
        proc = subprocess.Popen(
            command,
            stdin=popen_stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=popen_cwd,
            env=child_env,
            start_new_session=(os.name == "posix"),
        )
    except BaseException:
        if job is not None:
            _close_windows_job(job)
        raise
    if job is not None:
        try:
            job = _assign_windows_job(job, proc)
        except OSError:
            proc.kill()
            proc.wait()
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
            raise
    feeder = None
    if input_bytes is not None and proc.stdin is not None:
        def feed_stdin(pipe=proc.stdin, data=input_bytes):
            try:
                pipe.write(data)
                pipe.flush()
            except OSError:
                pass
            finally:
                try:
                    pipe.close()
                except OSError:
                    pass
        feeder = threading.Thread(target=feed_stdin, daemon=True)
        feeder.start()
    last_output = [start]
    cap_exceeded = [False]
    lock = threading.Lock()
    wake = threading.Event()
    child_done = threading.Event()
    reader_done = [False, False]
    total_room = max_total_output
    stop_reading = False
    streams: list[tuple[bytearray, bool]] = [(bytearray(), False), (bytearray(), False)]
    def drain(pipe, index, cap, counts_for_idle):
        nonlocal total_room
        try:
            while True:
                chunk = os.read(pipe.fileno(), 8192)
                if not chunk:
                    return
                with lock:
                    if stop_reading:
                        return
                    if counts_for_idle:
                        last_output[0] = time.monotonic()
                    buf, truncated = streams[index]
                    room = cap - len(buf)
                    capture_room = room + capture_overflow_byte
                    if total_room is not None:
                        room = min(room, total_room)
                        capture_room = min(capture_room, total_room)
                        total_room = max(0, total_room - len(chunk))
                    if capture_room > 0:
                        buf.extend(chunk[:capture_room])
                    if len(chunk) > room and not truncated:
                        streams[index] = (buf, True)
                        if kill_on_cap:
                            cap_exceeded[0] = True
                wake.set()
        finally:
            try:
                pipe.close()
            except OSError:
                pass
            with lock:
                reader_done[index] = True
            wake.set()
    threads = [
        threading.Thread(target=drain, args=(proc.stdout, 0, caps[0], True), daemon=True),
        threading.Thread(target=drain, args=(proc.stderr, 1, caps[1], idle_stream == "either"), daemon=True),
    ]
    for t in threads: t.start()
    def wait_child():
        proc.wait()
        child_done.set()
        wake.set()
    waiter = threading.Thread(target=wait_child, daemon=True)
    waiter.start()
    classification = "ok"
    killed = False
    cleanup = None
    drain_until = None
    stopped = None
    handlers = _install_stop_handlers()
    try:
        while True:
            wake.clear()
            with lock:
                complete = child_done.is_set() and all(reader_done)
                last = last_output[0]
                hit_cap = cap_exceeded[0]
            if complete:
                code = proc.returncode
                if not killed:
                    if kill_on_cap and hit_cap:
                        classification = "output_limit"
                    else:
                        classification = "ok" if code == 0 else ("signal" if code < 0 else "failed")
                break
            now = time.monotonic()
            if not killed:
                if kill_on_cap and hit_cap:
                    classification = "output_limit"
                elif timeout and now - start >= timeout:
                    classification = "timeout"
                elif idle and now - last >= idle:
                    classification = "idle"
                if classification != "ok":
                    cleanup = _terminate(proc, job, term_grace=0 if no_reap else term_grace)
                    killed = True
                    if no_reap:
                        break
                    drain_until = now + DRAIN_GRACE
            if killed:
                if drain_until is None:
                    drain_until = time.monotonic() + DRAIN_GRACE
                if time.monotonic() >= drain_until:
                    break
                wake.wait(drain_until - time.monotonic())
            else:
                remaining = []
                if timeout: remaining.append(start + timeout - now)
                if idle: remaining.append(last + idle - now)
                wake.wait(max(0, min(remaining)) if remaining else None)
    except _Stopped as interrupt:
        stopped = interrupt
    finally:
        _restore_stop_handlers(handlers)
        if not child_done.is_set() and not (no_reap and killed):
            _terminate(proc, job, term_grace=term_grace)
    if not (no_reap and killed):
        grace_end = time.monotonic() + DRAIN_GRACE
        if feeder is not None:
            feeder.join(max(0, grace_end - time.monotonic()))
        for t in threads:
            t.join(max(0, grace_end - time.monotonic()))
        waiter.join(DRAIN_GRACE)
    with lock:
        stop_reading = no_reap and killed
        out, out_truncated = streams[0]
        err, err_truncated = streams[1]
        stdout, stderr = bytes(out), bytes(err)
    if job is not None: _close_windows_job(job)
    if stopped is not None:
        signal.signal(stopped.signum, signal.SIG_DFL)
        os.kill(os.getpid(), stopped.signum)
        raise stopped
    code = proc.returncode
    limit = classification if no_reap and killed and code is None else None
    return RunResult(
        "unknown" if limit else classification, time.monotonic() - start, code,
        stdout, stderr, out_truncated, err_truncated, cleanup,
        abandoned_pid=proc.pid if limit else None, limit=limit,
    )


_ERROR_ACCESS_DENIED = 5
_KERNEL32 = None


def _windows_api():
    global _KERNEL32
    if _KERNEL32 is not None:
        return _KERNEL32
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    _KERNEL32 = kernel32
    return kernel32


def _job_limit_information(ctypes):
    """JOBOBJECT_EXTENDED_LIMIT_INFORMATION with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE."""
    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    information = ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = 0x2000
    return information


def _new_windows_job():
    import ctypes
    kernel32 = _windows_api()
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    information = _job_limit_information(ctypes)
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(information), ctypes.sizeof(information)):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel32.CloseHandle(job)
        raise error
    return job


def _assign_windows_job(job, proc: subprocess.Popen):
    import ctypes
    kernel32 = _windows_api()
    if not kernel32.AssignProcessToJobObject(job, ctypes.c_void_p(proc._handle)):
        err = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        # Windows returns ERROR_ACCESS_DENIED when the child has already exited
        # before assignment; treat that race as harmless and keep fail-closed
        # behavior whenever the child is still alive.
        if err == _ERROR_ACCESS_DENIED and proc.poll() is not None:
            return None
        raise ctypes.WinError(err)
    return job


def _close_windows_job(job) -> None:
    _windows_api().CloseHandle(job)


def _terminate(proc: subprocess.Popen, job=None, *, term_grace: float) -> str | None:
    if os.name == "posix":
        # macOS answers EPERM, not ESRCH, for a group left with only zombies (the leader
        # was already reaped). Treat both as "nothing left to signal", as the Rust edition does.
        gone = (ProcessLookupError, PermissionError)
        phase = None
        if term_grace:
            try: os.killpg(proc.pid, signal.SIGTERM)
            except gone: return phase
            phase = "SIGTERM"
            deadline = time.monotonic() + term_grace
            while time.monotonic() < deadline:
                try: os.killpg(proc.pid, 0)
                except gone: return phase
                time.sleep(.005)
        try: os.killpg(proc.pid, signal.SIGKILL)
        except gone: return phase
        return "SIGTERM then SIGKILL" if phase else "SIGKILL"
    else:
        if job is None:
            proc.kill()
            return "process kill"
        else:
            _windows_api().TerminateJobObject(job, 1)
            return "TerminateJobObject"

__all__ = ["RunResult", "run"]
