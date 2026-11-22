"""Run a child command with wall-time, idle-time, output, and tree limits.

``run()`` keeps the 0.1.8 stop-signal behavior: in the main POSIX thread it
temporarily turns SIGINT, SIGTERM and SIGHUP into a stop that kills the child
tree, restores the previous handlers and re-raises the signal. Handles from
``start()`` never install signal handlers; stop a run early with
``Run.cancel()`` (or by returning ``False`` from a chunk callback).
"""
from __future__ import annotations

import math
import os
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Sequence

__version__ = "0.2.0"


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
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    cancel_reason: str | None = None


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


class _Tail:
    """Newest-`cap`-bytes ring for ``keep="tail"`` captures."""

    __slots__ = ("cap", "parts", "total")

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.parts: deque[bytes] = deque()
        self.total = 0

    def append(self, data: bytes) -> None:
        if not data:
            return
        self.parts.append(data)
        self.total += len(data)
        while self.total > self.cap:
            excess = self.total - self.cap
            head = self.parts[0]
            if len(head) <= excess:
                self.total -= len(head)
                self.parts.popleft()
            else:
                self.parts[0] = head[excess:]
                self.total -= excess

    def bytes(self) -> bytes:
        return b"".join(self.parts)


def _open_sink(sink: object, append: bool):
    """Normalize a sink argument to (kind, target, owned) or None."""
    if sink is None:
        return None
    if isinstance(sink, (str, os.PathLike)):
        return ("path", open(os.fspath(sink), "ab" if append else "wb"), True)
    if callable(sink):
        return ("callable", sink, False)
    write = getattr(sink, "write", None)
    if callable(write):
        return ("file", sink, False)
    raise ValueError(
        f"invalid sink: {sink!r} (expected a path, a binary file object, or a callable)"
    )


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


class Run:
    """Handle for a started child: poll it, wait for it, or cancel it.

    Created with :func:`start`; ``run()`` is ``start(...).wait()``. The context
    manager cancels the run on exit when it has not finished yet.
    """

    def __init__(
        self,
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
        stdout_sink: object = None,
        stderr_sink: object = None,
        sink_append: bool = False,
        merge_stderr: bool = False,
        stdout_keep: str = "head",
        stderr_keep: str = "head",
        pass_fds: Sequence[int] = (),
    ) -> None:
        if not command:
            raise ValueError("command must not be empty")
        if idle_stream not in ("either", "stdout"):
            raise ValueError("idle_stream must be 'either' or 'stdout'")
        if stdout_keep not in ("head", "tail") or stderr_keep not in ("head", "tail"):
            raise ValueError("keep modes must be 'head' or 'tail'")
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
        if merge_stderr and stderr_sink is not None:
            raise ValueError("merge_stderr cannot be combined with stderr_sink")
        if merge_stderr and stderr_keep != "head":
            raise ValueError("stderr_keep has no effect with merge_stderr")
        if merge_stderr and max_stderr is not None:
            raise ValueError("max_stderr cannot be combined with merge_stderr")
        if stdout_keep == "tail" and stdout_sink is not None:
            raise ValueError("stdout_keep='tail' cannot be combined with stdout_sink")
        if stderr_keep == "tail" and stderr_sink is not None:
            raise ValueError("stderr_keep='tail' cannot be combined with stderr_sink")
        if pass_fds:
            if os.name == "nt":
                raise ValueError("pass_fds is only supported on POSIX")
            for fd in pass_fds:
                if not isinstance(fd, int) or isinstance(fd, bool) or fd < 0:
                    raise ValueError(f"invalid pass_fds entry: {fd!r}")
        self._timeout = timeout
        self._idle = idle
        self._kill_on_cap = kill_on_cap
        self._capture_overflow_byte = capture_overflow_byte
        self._term_grace = term_grace
        self._no_reap = no_reap
        self._merge_stderr = merge_stderr
        self._caps = (
            max_output if max_stdout is None else max_stdout,
            max_output if max_stderr is None else max_stderr,
        )
        self._keeps = (stdout_keep, stderr_keep)
        out_sink = _open_sink(stdout_sink, sink_append)
        err_sink = None if merge_stderr else _open_sink(stderr_sink, sink_append)
        self._owned_sinks = [s for s in (out_sink, err_sink) if s is not None and s[2]]
        if out_sink is not None:
            self._endpoints = [("sink", out_sink), ("drop", None)]
        else:
            self._endpoints = [("capture", None), ("drop", None)]
        if merge_stderr:
            # stderr bytes are delivered to the stdout sink in read order; with
            # no stdout sink they are counted and discarded.
            self._endpoints[1] = ("sink", out_sink) if out_sink is not None else ("drop", None)
        elif err_sink is not None:
            self._endpoints[1] = ("sink", err_sink)
        else:
            self._endpoints[1] = ("capture", None)
        # With merge_stderr the merged stream shares one budget: max_total_output
        # when given, otherwise the stdout cap.
        if merge_stderr:
            self._total_room: int | None = (
                max_total_output
                if max_total_output is not None
                else self._caps[0]
            )
        else:
            self._total_room = max_total_output
        popen_stdin, input_bytes = _resolve_stdin(stdin, input)
        popen_cwd = os.fspath(cwd) if cwd is not None else None
        self._start = time.monotonic()
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
        popen_kwargs: dict[str, object] = {}
        if pass_fds:
            popen_kwargs["pass_fds"] = tuple(pass_fds)
        try:
            proc = subprocess.Popen(
                command,
                stdin=popen_stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=popen_cwd,
                env=child_env,
                start_new_session=(os.name == "posix"),
                **popen_kwargs,
            )
        except BaseException:
            for sink in self._owned_sinks:
                try:
                    sink[1].close()
                except OSError:
                    pass
            if job is not None:
                _close_windows_job(job)
            raise
        self._proc = proc
        self._job = job
        if job is not None:
            try:
                job = _assign_windows_job(job, proc)
                self._job = job
            except OSError:
                proc.kill()
                proc.wait()
                for pipe in (proc.stdin, proc.stdout, proc.stderr):
                    if pipe is not None:
                        pipe.close()
                for sink in self._owned_sinks:
                    try:
                        sink[1].close()
                    except OSError:
                        pass
                raise
        self._feeder = None
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
            self._feeder = threading.Thread(target=feed_stdin, daemon=True)
            self._feeder.start()
        self._last_output = [self._start]
        self._observed = [0, 0]
        self._delivered = [0, 0]
        self._cap_exceeded = [False]
        self._truncated = [False, False]
        self._buffers = [bytearray(), bytearray()]
        self._rings = [_Tail(self._caps[0]), _Tail(self._caps[1])]
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._child_done = threading.Event()
        self._reader_done = [False, False]
        self._stop_reading = False
        self._cancel_event = threading.Event()
        self._cancel_reason: str | None = None
        self._stop_event = threading.Event()
        self._callback_error: BaseException | None = None
        self._classification = "ok"
        self._cleanup: str | None = None
        self._killed = False
        self._finished = False
        self._abandon = False
        self._drain_until: float | None = None
        self._result: RunResult | None = None
        self._drain_threads = [
            threading.Thread(target=self._drain, args=(proc.stdout, 0, True), daemon=True),
            threading.Thread(
                target=self._drain,
                args=(proc.stderr, 1, idle_stream == "either"),
                daemon=True,
            ),
        ]
        for t in self._drain_threads:
            t.start()
        def wait_child():
            proc.wait()
            self._child_done.set()
            self._wake.set()
        self._waiter = threading.Thread(target=wait_child, daemon=True)
        self._waiter.start()

    @property
    def pid(self) -> int:
        """The direct child pid."""
        return self._proc.pid

    def __enter__(self) -> "Run":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._result is None and not self._finished:
            self.cancel()
        if self._result is None:
            self.wait()

    def cancel(self, reason: str = "cancelled", term_grace: float | None = None) -> None:
        """Stop the run: TERM then KILL to the group or Job, classification ``cancelled``.

        Idempotent; the first reason wins. ``term_grace`` overrides the run's own
        grace for this cancellation's cleanup.
        """
        if self._finished or self._cancel_event.is_set():
            return
        if term_grace is not None:
            if not math.isfinite(term_grace) or term_grace < 0:
                raise ValueError("term_grace must be finite and nonnegative")
            self._cancel_grace = term_grace
        self._cancel_reason = "cancelled" if reason is None else str(reason)
        self._cancel_event.set()
        self._wake.set()

    def poll(self) -> RunResult | None:
        """Return the finished result, or None while the run is still going."""
        if self._result is not None:
            return self._result
        return self._monitor(block=False)

    def wait(self, timeout: float | None = None) -> RunResult:
        """Wait for the run and return its result.

        With ``timeout``, raise ``TimeoutError`` when the result is not ready;
        the run keeps going and a later ``wait()`` or ``poll()`` still works.
        A chunk-callback exception is re-raised here after cleanup.
        """
        if timeout is not None:
            if not math.isfinite(timeout) or timeout < 0:
                raise ValueError("wait timeout must be finite and nonnegative")
            deadline = time.monotonic() + timeout
        else:
            deadline = None
        return self._monitor(block=True, deadline=deadline)

    def _abort_for_signal(self) -> None:
        """0.1.8 parity: a stop signal kills the tree before the signal re-raises."""
        if not self._child_done.is_set():
            _terminate(self._proc, self._job, term_grace=self._term_grace)
        try:
            self._finalize()
        except Exception:
            pass

    def _write_sink(self, sink, piece: bytes) -> None:
        kind, target = sink[0], sink[1]
        if kind == "callable":
            if target(piece) is False:
                self._stop_event.set()
                self._wake.set()
        else:
            target.write(piece)
            target.flush()

    def _drain(self, pipe, index: int, counts_for_idle: bool) -> None:
        kind = self._endpoints[index][0]
        keep = self._keeps[index]
        cap = self._caps[index]
        try:
            while True:
                chunk = os.read(pipe.fileno(), 8192)
                if not chunk:
                    return
                n = len(chunk)
                with self._lock:
                    if self._stop_reading:
                        return
                    if counts_for_idle:
                        self._last_output[0] = time.monotonic()
                    self._observed[index] += n
                    shared = self._total_room
                    if shared is not None:
                        available = shared
                        self._total_room = max(0, shared - n)
                    else:
                        available = None
                    if kind == "drop":
                        if available is not None and n > available:
                            self._truncated[index] = True
                    elif kind == "capture" and keep == "tail":
                        # A tail capture is bounded by construction: it never
                        # trips kill_on_cap for its own stream.
                        room = cap if available is None else min(cap, available)
                        self._rings[index].append(chunk[:room])
                        if n > room or self._observed[index] > cap:
                            self._truncated[index] = True
                    elif kind == "capture":
                        buf = self._buffers[index]
                        room = cap - len(buf)
                        capture_room = room + (1 if self._capture_overflow_byte else 0)
                        if available is not None:
                            room = min(room, available)
                            capture_room = min(capture_room, available)
                        if capture_room > 0:
                            buf.extend(chunk[:capture_room])
                        if n > room and not self._truncated[index]:
                            self._truncated[index] = True
                            if self._kill_on_cap:
                                self._cap_exceeded[0] = True
                    else:
                        # Sink delivery: capped by bytes delivered to the sink.
                        # Under merge_stderr both streams share the merged budget.
                        sink = self._endpoints[index][1]
                        if self._merge_stderr:
                            room = available
                        else:
                            room = cap - self._delivered[index]
                            if available is not None:
                                room = min(room, available)
                        if n > room and not self._truncated[index]:
                            self._truncated[index] = True
                            if self._kill_on_cap and not self._cap_exceeded[0]:
                                self._cap_exceeded[0] = True
                        if self._merge_stderr:
                            keep_len = max(0, min(room, n))
                        else:
                            capture_room = room + (1 if self._capture_overflow_byte else 0)
                            keep_len = max(0, min(capture_room, n))
                            self._delivered[index] += keep_len
                        if keep_len > 0:
                            self._write_sink(sink, chunk[:keep_len])
                    self._wake.set()
        except Exception as exc:
            # A failing callback (or sink write) stops the run; wait() re-raises
            # it after the tree is gone.
            if self._callback_error is None:
                self._callback_error = exc
            self._stop_event.set()
            self._wake.set()
        finally:
            try:
                pipe.close()
            except OSError:
                pass
            with self._lock:
                self._reader_done[index] = True
            self._wake.set()

    def _monitor(self, block: bool, deadline: float | None = None) -> RunResult | None:
        while True:
            self._wake.clear()
            with self._lock:
                complete = self._child_done.is_set() and all(self._reader_done)
                last = self._last_output[0]
                hit_cap = self._cap_exceeded[0]
            if not self._finished:
                if self._cancel_event.is_set():
                    self._finished = True
                    self._killed = True
                    self._classification = "cancelled"
                    grace = getattr(self, "_cancel_grace", None)
                    self._cleanup = _terminate(
                        self._proc, self._job,
                        term_grace=self._term_grace if grace is None else grace,
                    )
                    self._drain_until = time.monotonic() + DRAIN_GRACE
                elif self._stop_event.is_set():
                    self._finished = True
                    self._killed = True
                    self._classification = "stopped"
                    self._cleanup = _terminate(
                        self._proc, self._job,
                        term_grace=0 if self._no_reap else self._term_grace,
                    )
                    if self._no_reap:
                        self._abandon = True
                        break
                    self._drain_until = time.monotonic() + DRAIN_GRACE
            if complete:
                code = self._proc.returncode
                if not self._killed:
                    if self._kill_on_cap and hit_cap:
                        self._classification = "output_limit"
                    else:
                        self._classification = "ok" if code == 0 else ("signal" if code < 0 else "failed")
                self._finished = True
                break
            now = time.monotonic()
            if not self._killed:
                if self._kill_on_cap and hit_cap:
                    self._classification = "output_limit"
                elif self._timeout and now - self._start >= self._timeout:
                    self._classification = "timeout"
                elif self._idle and now - last >= self._idle:
                    self._classification = "idle"
                if self._classification != "ok":
                    self._cleanup = _terminate(
                        self._proc, self._job,
                        term_grace=0 if self._no_reap else self._term_grace,
                    )
                    self._killed = True
                    self._finished = True
                    if self._no_reap:
                        self._abandon = True
                        break
                    self._drain_until = now + DRAIN_GRACE
            if self._killed:
                if self._drain_until is None:
                    self._drain_until = time.monotonic() + DRAIN_GRACE
                if time.monotonic() >= self._drain_until:
                    self._finished = True
                    break
                if not block:
                    return None
                self._wake.wait(max(0, self._drain_until - time.monotonic()))
            else:
                if not block:
                    return None
                remaining = []
                if self._timeout: remaining.append(self._start + self._timeout - now)
                if self._idle: remaining.append(last + self._idle - now)
                wait_s = max(0, min(remaining)) if remaining else None
                if deadline is not None:
                    till = deadline - time.monotonic()
                    if till <= 0:
                        raise TimeoutError("the run did not finish within the wait timeout")
                    wait_s = till if wait_s is None else min(wait_s, till)
                self._wake.wait(wait_s)
        return self._finalize()

    def _finalize(self) -> RunResult:
        if self._result is not None:
            return self._result
        if not self._abandon:
            grace_end = time.monotonic() + DRAIN_GRACE
            if self._feeder is not None:
                self._feeder.join(max(0, grace_end - time.monotonic()))
            for t in self._drain_threads:
                t.join(max(0, grace_end - time.monotonic()))
            self._waiter.join(DRAIN_GRACE)
        with self._lock:
            self._stop_reading = self._abandon
            if self._endpoints[0][0] == "capture":
                stdout = self._rings[0].bytes() if self._keeps[0] == "tail" else bytes(self._buffers[0])
            else:
                stdout = b""
            if self._endpoints[1][0] == "capture":
                stderr = self._rings[1].bytes() if self._keeps[1] == "tail" else bytes(self._buffers[1])
            else:
                stderr = b""
            out_truncated, err_truncated = self._truncated
        for sink in self._owned_sinks:
            try:
                sink[1].close()
            except OSError:
                pass
        if self._job is not None:
            _close_windows_job(self._job)
        error = self._callback_error
        code = self._proc.returncode
        limit = self._classification if self._abandon and code is None else None
        result = RunResult(
            "unknown" if limit else self._classification,
            time.monotonic() - self._start,
            code,
            stdout,
            stderr,
            out_truncated,
            err_truncated,
            self._cleanup,
            abandoned_pid=self._proc.pid if limit else None,
            limit=limit,
            stdout_bytes=self._observed[0],
            stderr_bytes=self._observed[1],
            cancel_reason=self._cancel_reason if self._classification == "cancelled" else None,
        )
        self._result = result
        if error is not None:
            raise error
        return result


def start(command: Sequence[str], **kwargs) -> Run:
    """Start ``command`` and return a :class:`Run` handle (see its docs)."""
    return Run(command, **kwargs)


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
    stdout_sink: object = None,
    stderr_sink: object = None,
    sink_append: bool = False,
    merge_stderr: bool = False,
    stdout_keep: str = "head",
    stderr_keep: str = "head",
    pass_fds: Sequence[int] = (),
) -> RunResult:
    """Execute an absolute or PATH-resolved command with optional shared limits.

    ``stdout_sink``/``stderr_sink`` accept a path (opened ``wb``, or ``ab`` with
    ``sink_append=True``), a binary file object, or a callable receiving each
    chunk in order; returning ``False`` from the callable stops the run with
    classification ``"stopped"``. ``merge_stderr=True`` delivers both streams to
    the stdout sink in arrival order. ``stdout_keep``/``stderr_keep="tail"``
    keep the newest capped bytes of a captured stream instead of the first ones.
    ``pass_fds`` forwards extra descriptors on POSIX. See :class:`Run` for
    ``start()``, ``poll()``, ``wait()`` and ``cancel()``.

    In the main POSIX thread, ``run()`` keeps the 0.1.8 stop-signal behavior:
    SIGINT, SIGTERM and SIGHUP temporarily become a stop that kills the child
    tree, restores the previous handlers and re-raises the signal.
    """
    handle = Run(
        command,
        timeout=timeout,
        idle=idle,
        idle_stream=idle_stream,
        max_output=max_output,
        max_stdout=max_stdout,
        max_stderr=max_stderr,
        max_total_output=max_total_output,
        kill_on_cap=kill_on_cap,
        capture_overflow_byte=capture_overflow_byte,
        term_grace=term_grace,
        no_reap=no_reap,
        cwd=cwd,
        stdin=stdin,
        input=input,
        env=env,
        env_exact=env_exact,
        stdout_sink=stdout_sink,
        stderr_sink=stderr_sink,
        sink_append=sink_append,
        merge_stderr=merge_stderr,
        stdout_keep=stdout_keep,
        stderr_keep=stderr_keep,
        pass_fds=pass_fds,
    )
    handlers = _install_stop_handlers()
    stopped = None
    try:
        result = handle.wait()
    except _Stopped as interrupt:
        stopped = interrupt
        result = None
    finally:
        _restore_stop_handlers(handlers)
        if stopped is not None:
            handle._abort_for_signal()
    if stopped is not None:
        signal.signal(stopped.signum, signal.SIG_DFL)
        os.kill(os.getpid(), stopped.signum)
        raise stopped
    return result


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

__all__ = ["RunResult", "Run", "run", "start"]
