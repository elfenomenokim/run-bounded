"""Python-edition regressions that only a real process can show.

Run with: python3 tests/test_python.py
"""

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))

from bounded_run import run  # noqa: E402


@unittest.skipUnless(os.name == "posix", "process-group check")
class NoReapTest(unittest.TestCase):
    def test_hung_lsof_returns_unknown_while_the_child_is_alive(self):
        # Use the incident fixture's sleeping lsof, delaying only delivery of
        # its KILL so the real wait stays blocked until test cleanup.
        with tempfile.TemporaryDirectory() as tmp:
            lsof = Path(tmp) / "lsof"
            lsof.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
            lsof.chmod(0o755)
            reap = subprocess.Popen.wait
            kill_group = os.killpg
            children = []
            reaped = threading.Event()

            def wait(child):
                children.append((child.pid, threading.current_thread().daemon))
                try:
                    return reap(child)
                finally:
                    reaped.set()

            with patch.object(subprocess.Popen, "wait", wait), patch.object(os, "killpg") as kill:
                try:
                    result = run([str(lsof)], timeout=0.1, idle=0, no_reap=True)
                    self.assertEqual((result.classification, result.limit), ("unknown", "timeout"))
                    self.assertIsNone(result.returncode)
                    self.assertEqual(children, [(result.abandoned_pid, True)])
                    kill.assert_called_once_with(result.abandoned_pid, signal.SIGKILL)
                    os.kill(result.abandoned_pid, 0)
                    self.assertFalse(reaped.is_set())
                    self.assertLess(result.duration, 0.4)
                finally:
                    for pid, _ in children:
                        kill_group(pid, signal.SIGKILL)
                    self.assertTrue(reaped.wait(2))


@unittest.skipUnless(os.name == "posix", "process-group check")
class DrainAfterKillTest(unittest.TestCase):
    def test_a_descendant_outside_the_group_cannot_hold_the_result(self):
        # The escapist leaves the process group but keeps stdout open, so a
        # runner that joins its readers waits for the escapist to exit.
        command = [
            "/bin/sh",
            "-c",
            "python3 -c 'import os, time; os.setsid(); time.sleep(10)' & "
            "while :; do sleep 0.05; done",
        ]
        started = time.monotonic()
        result = run(command, timeout=0.5, idle=0)
        elapsed = time.monotonic() - started
        self.assertEqual(result.classification, "timeout")
        self.assertLess(elapsed, 3.0, "the result waited for the escapist's stdout")


@unittest.skipUnless(os.name == "posix", "process-group check")
class TermResistantGroupTest(unittest.TestCase):
    def test_a_term_resistant_descendant_is_killed_without_raising(self):
        # Leader and child both ignore SIGTERM, so the group needs SIGKILL. On macOS the
        # cleanup after that once raised PermissionError (EPERM for a zombie-only group).
        import signal
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = os.path.join(tmp, "child.pid")
            program = (
                "import signal, subprocess, sys, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "child = subprocess.Popen([sys.executable, '-c', "
                "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)']); "
                "open(sys.argv[1], 'w').write(str(child.pid)); time.sleep(30)"
            )
            result = run([sys.executable, "-c", program, pid_file], timeout=0.5, idle=0)
            self.assertEqual(result.classification, "timeout")
            self.assertEqual(result.cleanup, "SIGTERM then SIGKILL")
            with open(pid_file) as fh:
                child = int(fh.read())
            for _ in range(40):
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.025)
            with os.popen(f"ps -p {child} -o stat=") as ps:
                state = ps.read().strip()
            self.assertTrue(not state or state.startswith("Z"), f"descendant {child} still running")

    def test_zero_term_grace_kills_without_sending_term(self):
        import signal
        program = (
            "import signal, time; "
            "signal.signal(signal.SIGTERM, lambda *_: print('term', flush=True)); "
            "print('ready', flush=True); time.sleep(30)"
        )
        result = run([sys.executable, "-c", program], timeout=0.5, idle=0, term_grace=0)
        self.assertEqual(result.classification, "timeout")
        self.assertEqual(result.returncode, -signal.SIGKILL)
        self.assertEqual(result.cleanup, "SIGKILL")
        self.assertEqual(result.stdout, b"ready\n")


class OptionsAndCapsTest(unittest.TestCase):
    def test_no_reap_returns_a_normal_result_for_a_completed_child(self):
        res = run([sys.executable, "-c", "print('done')"], no_reap=True)
        self.assertEqual((res.classification, res.returncode, res.stdout), ("ok", 0, b"done\n"))
        self.assertIsNone(res.abandoned_pid)
        self.assertIsNone(res.limit)

    def test_combined_cap_is_shared_by_both_streams(self):
        res = run(
            [sys.executable, "-c", "import sys; print('abcd',end=''); print('wxyz',end='',file=sys.stderr)"],
            max_total_output=6,
            kill_on_cap=True,
            capture_overflow_byte=True,
        )
        self.assertEqual(res.classification, "output_limit")
        self.assertEqual(len(res.stdout) + len(res.stderr), 6)
        self.assertTrue(b"abcd".startswith(res.stdout))
        self.assertTrue(b"wxyz".startswith(res.stderr))
        self.assertEqual((res.stdout_truncated, res.stderr_truncated), (len(res.stdout) < 4, len(res.stderr) < 4))

    def test_stdout_only_idle_ignores_a_stderr_heartbeat(self):
        program = """
import sys, time
for _ in range(15):
    print('heartbeat', file=sys.stderr, flush=True)
    time.sleep(0.1)
print('done', flush=True)
"""
        command = [sys.executable, "-c", program]
        default = run(command, timeout=5, idle=0.5)
        self.assertEqual(default.classification, "ok")
        self.assertEqual(default.stdout, b"done\n")

        result = run(command, timeout=5, idle=0.5, idle_stream="stdout")
        self.assertEqual(result.classification, "idle")
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"heartbeat\n", result.stderr)

        with self.assertRaises(ValueError):
            run(command, idle_stream="stderr")

    def test_exact_environment_does_not_overlay_the_base(self):
        program = (
            "import os; "
            "print(os.getenv('FENO_BOUND_TEST')); "
            "print(os.getenv('LANG'))"
        )
        command = [sys.executable, "-c", program]
        env = {"FENO_BOUND_TEST": "supplied=value"}
        if os.name == "nt":
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        with patch.dict(os.environ, {"LANG": "original"}):
            default = run(command, env=env)
            self.assertEqual(default.stdout.splitlines(), [b"supplied=value", b"original"])
            exact = run(command, env=env, env_exact=True)
            self.assertEqual(exact.classification, "ok")
            self.assertEqual(exact.stdout.splitlines(), [b"supplied=value", b"None"])

    def test_one_overflow_byte_still_triggers_the_original_cap(self):
        program = "import time; print('abcde', end='', flush=True); time.sleep(30)"
        result = run(
            [sys.executable, "-c", program], timeout=3, idle=0, max_stdout=4,
            kill_on_cap=True, capture_overflow_byte=True,
        )
        self.assertEqual(result.classification, "output_limit")
        self.assertEqual(result.stdout, b"abcde")
        self.assertTrue(result.stdout_truncated)

    def test_stdin_closed_by_default_and_input_or_inherit(self):
        res = run([sys.executable, "-c", "import sys; sys.stdout.write(repr(sys.stdin.read()))"], timeout=5)
        self.assertEqual(res.classification, "ok")
        self.assertEqual(res.stdout, b"''")

        res_str = run(["/bin/cat"], timeout=5, input="hello\n")
        self.assertEqual(res_str.classification, "ok")
        self.assertEqual(res_str.stdout, b"hello\n")

        res_bytes = run(["/bin/cat"], timeout=5, input=b"\x00\x01ok")
        self.assertEqual(res_bytes.classification, "ok")
        self.assertEqual(res_bytes.stdout, b"\x00\x01ok")

        res_ignored = run(["/usr/bin/true"], timeout=5, input="x" * 200_000)
        self.assertEqual(res_ignored.classification, "ok")

        # Verify stdin="inherit" passes parent's stdin through to the child.
        python_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python")
        probe = (
            f"import sys; sys.path.insert(0, {python_dir!r}); "
            "from bounded_run import run; "
            "r = run(['/bin/cat'], timeout=5, stdin='inherit'); "
            "sys.stdout.buffer.write(r.stdout)"
        )
        inherited = subprocess.run(
            [sys.executable, "-c", probe],
            input=b"inherited-from-parent\n",
            capture_output=True,
            check=True,
        )
        self.assertEqual(inherited.stdout, b"inherited-from-parent\n")

        with self.assertRaises(ValueError):
            run(["/usr/bin/true"], stdin="inherit", input="conflict")

    def test_cwd_sets_working_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = run([sys.executable, "-c", "import os; print(os.getcwd())"], timeout=5, cwd=tmp)
            self.assertEqual(res.classification, "ok")
            self.assertEqual(os.path.realpath(res.stdout.decode().strip()), os.path.realpath(tmp))

    def test_separate_stdout_and_stderr_caps(self):
        res = run(
            [sys.executable, "-c", "import sys; sys.stdout.write('12345678'); sys.stderr.write('abcdefgh')"],
            timeout=5,
            max_stdout=5,
            max_stderr=10,
        )
        self.assertEqual(res.classification, "ok")
        self.assertEqual(res.stdout, b"12345")
        self.assertEqual(res.stderr, b"abcdefgh")
        self.assertTrue(res.stdout_truncated)
        self.assertFalse(res.stderr_truncated)

    @unittest.skipUnless(os.name == "posix", "process-group check")
    def test_kill_on_cap_terminates_group_and_reports_output_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = os.path.join(tmp, "child.pid")
            program = (
                "import signal, subprocess, sys, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "child = subprocess.Popen([sys.executable, '-c', "
                "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)']); "
                "open(sys.argv[1], 'w').write(str(child.pid)); "
                "sys.stderr.write('0123456789abcdef'); sys.stderr.flush(); time.sleep(30)"
            )
            started = time.monotonic()
            res = run(
                [sys.executable, "-c", program, pid_file],
                timeout=10,
                idle=0,
                max_stdout=100,
                max_stderr=8,
                kill_on_cap=True,
            )
            elapsed = time.monotonic() - started
            self.assertEqual(res.classification, "output_limit")
            self.assertFalse(res.stdout_truncated)
            self.assertTrue(res.stderr_truncated)
            self.assertEqual(res.stderr, b"01234567")
            self.assertLess(elapsed, 3.0)
            with open(pid_file) as fh:
                child = int(fh.read())
            for _ in range(40):
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.025)
            with os.popen(f"ps -p {child} -o stat=") as ps:
                state = ps.read().strip()
            self.assertTrue(not state or state.startswith("Z"), f"descendant {child} still running")

    def test_kill_on_cap_reports_output_limit_even_if_child_exits_immediately(self):
        res = run(
            [sys.executable, "-c", "import sys; sys.stdout.write('0123456789')"],
            timeout=5,
            max_output=4,
            kill_on_cap=True,
        )
        self.assertEqual(res.classification, "output_limit")
        self.assertTrue(res.stdout_truncated)
        self.assertEqual(res.stdout, b"0123")


class SinksCallbacksAndMergeTest(unittest.TestCase):
    def test_file_sink_caps_delivery_reports_counts_and_stays_flat(self):
        # Design test 1: 50 MiB through a 1 MiB-capped file sink. The file
        # holds exactly the capped head, memory stays flat, and stdout_bytes
        # reports the full delivered count.
        import tracemalloc

        block = bytes(range(256)) * 32  # 8192 bytes
        total = 50 * 1024 * 1024
        cap = 1024 * 1024
        program = f"import sys; sys.stdout.buffer.write({block!r} * {total // len(block)})"
        with tempfile.TemporaryDirectory() as tmp:
            sink = Path(tmp) / "out.log"
            tracemalloc.start()
            before = tracemalloc.take_snapshot()
            result = run(
                [sys.executable, "-c", program],
                timeout=30,
                idle=0,
                max_stdout=cap,
                stdout_sink=sink,
            )
            after = tracemalloc.take_snapshot()
            tracemalloc.stop()
            self.assertEqual(result.classification, "ok")
            self.assertEqual(result.stdout, b"")
            self.assertEqual(result.stdout_bytes, total)
            self.assertTrue(result.stdout_truncated)
            self.assertFalse(result.stderr_truncated)
            self.assertEqual(sink.read_bytes(), block * (cap // len(block)))
            growth = sum(stat.size_diff for stat in after.compare_to(before, "filename"))
            self.assertLess(growth, 8 * 1024 * 1024, f"the sink held {growth} bytes in memory")

        with tempfile.TemporaryDirectory() as tmp:
            sink = Path(tmp) / "out.log"
            collected = []
            result = run(
                [sys.executable, "-c", program],
                timeout=30,
                idle=0,
                max_stdout=cap,
                stdout_sink=lambda chunk: collected.append(chunk) or True,
            )
            self.assertEqual(result.classification, "ok")
            self.assertEqual(b"".join(collected), block * (cap // len(block)))
            self.assertEqual(result.stdout_bytes, total)
            self.assertTrue(result.stdout_truncated)

    @unittest.skipUnless(os.name == "posix", "process-group check")
    def test_callback_false_stops_the_run_and_kills_the_tree(self):
        # Design test 2: a line consumer that returns False after line 3 stops
        # the run (classification "stopped") and the grandchild is gone.
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = os.path.join(tmp, "child.pid")
            program = (
                "import subprocess, sys, time; "
                "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                "open(sys.argv[1], 'w').write(str(g.pid)); "
                "import time as t; "
                "[print(f'line{i}', flush=True) or t.sleep(0.05) for i in range(1, 6)]; "
                "t.sleep(30)"
            )
            newline_count = []

            def consume(chunk):
                newline_count.append(chunk.count(b"\n"))
                return sum(newline_count) < 3

            result = run(
                [sys.executable, "-c", program, pid_file],
                stdout_sink=consume,
                timeout=10,
                idle=5,
            )
            self.assertEqual(result.classification, "stopped")
            self.assertEqual(result.stdout, b"")
            with open(pid_file) as fh:
                grandchild = int(fh.read())
            for _ in range(40):
                try:
                    os.kill(grandchild, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.025)
            with os.popen(f"ps -p {grandchild} -o stat=") as ps:
                state = ps.read().strip()
            self.assertTrue(not state or state.startswith("Z"), f"grandchild {grandchild} still running")

    @unittest.skipUnless(os.name == "posix", "process-group check")
    def test_callback_exception_is_reraised_after_cleanup(self):
        # Design test 3: a failing callback stops the run, the tree is cleaned
        # up, and the exception is re-raised from run() afterwards.
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = os.path.join(tmp, "child.pid")
            program = (
                "import subprocess, sys, time; "
                "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                "open(sys.argv[1], 'w').write(str(g.pid)); "
                "print('data', flush=True); time.sleep(30)"
            )

            def explode(chunk):
                raise RuntimeError("sink exploded")

            with self.assertRaises(RuntimeError):
                run([sys.executable, "-c", program, pid_file], stdout_sink=explode, timeout=10, idle=5)
            with open(pid_file) as fh:
                grandchild = int(fh.read())
            for _ in range(40):
                try:
                    os.kill(grandchild, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.025)
            with os.popen(f"ps -p {grandchild} -o stat=") as ps:
                state = ps.read().strip()
            self.assertTrue(not state or state.startswith("Z"), f"grandchild {grandchild} still running")

    def test_merge_stderr_preserves_arrival_order(self):
        # Design test 4: interleaved writes reach the one merged sink in order.
        program = (
            "import sys, time; "
            "sys.stdout.write('a'); sys.stdout.flush(); time.sleep(0.05); "
            "sys.stderr.write('b'); sys.stderr.flush(); time.sleep(0.05); "
            "sys.stdout.write('c'); sys.stdout.flush(); time.sleep(0.05); "
            "sys.stderr.write('d'); sys.stderr.flush()"
        )
        pieces = []
        result = run(
            [sys.executable, "-c", program],
            stdout_sink=lambda chunk: pieces.append(chunk) or True,
            merge_stderr=True,
            timeout=10,
        )
        self.assertEqual(result.classification, "ok")
        self.assertEqual(b"".join(pieces), b"abcd")
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")
        self.assertEqual(result.stdout_bytes, 2)
        self.assertEqual(result.stderr_bytes, 2)

        with self.assertRaises(ValueError):
            run([sys.executable, "-c", "print('x')"], merge_stderr=True, stderr_sink=pieces.append)
        with self.assertRaises(ValueError):
            run([sys.executable, "-c", "print('x')"], merge_stderr=True, max_stderr=10)


@unittest.skipUnless(os.name == "posix", "process-group check")
class StartCancelTest(unittest.TestCase):
    def test_cancel_from_another_thread_marks_cancelled_and_kills_the_tree(self):
        # Design test 5: start().cancel() mid-run from another thread gives a
        # cancelled result with the reason, an idempotent second cancel, and a
        # dead grandchild.
        from bounded_run import start

        with tempfile.TemporaryDirectory() as tmp:
            pid_file = os.path.join(tmp, "child.pid")
            program = (
                "import subprocess, sys, time; "
                "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                "open(sys.argv[1], 'w').write(str(g.pid)); time.sleep(30)"
            )
            handle = start([sys.executable, "-c", program, pid_file], timeout=30, idle=0)
            self.assertIsInstance(handle.pid, int)
            cancel_done = threading.Event()

            def cancel_later():
                handle.cancel(reason="owner shutdown")
                cancel_done.set()

            timer = threading.Timer(0.3, cancel_later)
            timer.start()
            result = handle.wait(10)
            timer.join(5)
            self.assertTrue(cancel_done.is_set())
            self.assertEqual(result.classification, "cancelled")
            self.assertEqual(result.cancel_reason, "owner shutdown")
            handle.cancel()  # idempotent: a finished run accepts a second cancel
            self.assertIs(handle.poll(), result)
            with open(pid_file) as fh:
                grandchild = int(fh.read())
            for _ in range(40):
                try:
                    os.kill(grandchild, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.025)
            with os.popen(f"ps -p {grandchild} -o stat=") as ps:
                state = ps.read().strip()
            self.assertTrue(not state or state.startswith("Z"), f"grandchild {grandchild} still running")

    def test_start_poll_and_context_manager(self):
        from bounded_run import start

        handle = start([sys.executable, "-c", "import time; time.sleep(2)"], timeout=10, idle=0)
        self.assertIsNone(handle.poll())
        result = handle.wait(5)
        self.assertEqual(result.classification, "ok")

        with start([sys.executable, "-c", "import time; time.sleep(30)"], timeout=30, idle=0) as handle:
            pass
        self.assertEqual(handle.poll().classification, "cancelled")


@unittest.skipUnless(os.name == "posix", "pass_fds is POSIX-only")
class PassFdsTest(unittest.TestCase):
    def test_pass_fds_forwards_the_descriptor_and_ebadf_without_it(self):
        # Design test 7: the child writes to an inherited fd; without pass_fds
        # the same write dies with EBADF.
        read_fd, write_fd = os.pipe()
        program = f"import os; os.write({write_fd}, b'via-fd'); os.close({write_fd})"
        try:
            ok = run([sys.executable, "-c", program], pass_fds=(write_fd,), timeout=5)
            self.assertEqual(ok.classification, "ok")
            os.close(write_fd)
            write_fd = None
            self.assertEqual(os.read(read_fd, 16), b"via-fd")
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)

        read_fd, write_fd = os.pipe()
        program = f"import os; os.write({write_fd}, b'x')"
        try:
            bad = run([sys.executable, "-c", program], timeout=5)
            self.assertEqual(bad.classification, "failed")
            self.assertTrue(b"EBADF" in bad.stderr or b"Bad file descriptor" in bad.stderr)
        finally:
            os.close(read_fd)
            os.close(write_fd)

        with self.assertRaises(ValueError):
            run([sys.executable, "-c", "print('x')"], pass_fds=(-1,))


class TailKeepTest(unittest.TestCase):
    def test_stderr_tail_keeps_exactly_the_newest_bytes(self):
        # Design test 8: 10 MiB of stderr with a 1 MiB cap in tail mode keeps
        # the last cap bytes exactly and never trips kill_on_cap.
        block = bytes(range(256)) * 32  # 8192 bytes
        total = 10 * 1024 * 1024
        cap = 1024 * 1024
        program = f"import sys; sys.stderr.buffer.write({block!r} * {total // len(block)})"
        result = run(
            [sys.executable, "-c", program],
            timeout=30,
            idle=10,
            max_stderr=cap,
            stderr_keep="tail",
            kill_on_cap=True,
        )
        self.assertEqual(result.classification, "ok", "a tail stream is bounded by construction")
        self.assertEqual(len(result.stderr), cap)
        self.assertEqual(result.stderr, block * (cap // len(block)))
        self.assertTrue(result.stderr_truncated)
        self.assertEqual(result.stderr_bytes, total)
        self.assertEqual(result.stdout, b"")

    def test_tail_never_kills_while_the_stdout_cap_still_does(self):
        program = (
            "import sys, time; "
            "sys.stderr.write('y' * 10000); sys.stderr.flush(); time.sleep(0.1); "
            "sys.stdout.write('x' * 100000); sys.stdout.flush(); time.sleep(30)"
        )
        result = run(
            [sys.executable, "-c", program],
            timeout=15,
            idle=0,
            max_stdout=1024,
            max_stderr=64,
            stderr_keep="tail",
            kill_on_cap=True,
        )
        self.assertEqual(result.classification, "output_limit", "the stdout cap still kills")
        self.assertTrue(result.stdout_truncated)
        self.assertEqual(result.stderr, b"y" * 64)
        self.assertTrue(result.stderr_truncated)

        with self.assertRaises(ValueError):
            run([sys.executable, "-c", "print('x')"], stderr_keep="tail", stderr_sink=Path(os.devnull))


@unittest.skipUnless(os.name == "posix", "main-thread stop-signal handlers")
class StopSignalTest(unittest.TestCase):
    """0.1.8 contract: run() stops the child tree when the parent itself is stopped."""

    def _launch_probe_and_signal(self, signum):
        # A parent Python runs run() on a sleeping child that records its pid;
        # a stop signal must kill the child tree and then kill the parent by
        # the same signal (the 0.1.8 re-raise contract).
        child_prog = (
            "import os, sys, time; "
            "open(sys.argv[1], 'w').write(str(os.getpid())); "
            "time.sleep(30)"
        )
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = os.path.join(tmp, "child.pid")
            python_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python")
            probe = (
                f"import sys; sys.path.insert(0, {os.path.abspath(python_dir)!r}); "
                "from bounded_run import run; "
                f"run([sys.executable, '-c', {child_prog!r}, {pid_file!r}], timeout=60, idle=0)"
            )
            parent = subprocess.Popen(
                [sys.executable, "-c", probe],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 10
            while not os.path.exists(pid_file):
                if parent.poll() is not None:
                    self.fail(f"the probe exited early: {parent.stderr.read()!r}")
                if time.monotonic() >= deadline:
                    parent.kill()
                    parent.wait()
                    self.fail("the child never wrote its pid")
                time.sleep(0.02)
            child = int(open(pid_file).read())
            time.sleep(0.2)  # let the probe reach run()'s wait
            parent.send_signal(signum)
            _, err = parent.communicate(timeout=10)
            self.assertEqual(
                parent.returncode,
                -signum,
                f"the probe must die by signal {signum}: {err!r}",
            )
            for _ in range(40):
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.025)
            with os.popen(f"ps -p {child} -o stat=") as ps:
                state = ps.read().strip()
            self.assertTrue(
                not state or state.startswith("Z"),
                f"child {child} outlived the parent's signal {signum}",
            )

    def test_sigterm_to_the_parent_kills_the_child_tree(self):
        self._launch_probe_and_signal(signal.SIGTERM)

    def test_sighup_to_the_parent_kills_the_child_tree(self):
        self._launch_probe_and_signal(signal.SIGHUP)

    def test_sigint_to_the_parent_kills_the_child_tree(self):
        self._launch_probe_and_signal(signal.SIGINT)

    def test_run_restores_the_previous_stop_handlers(self):
        received = []

        def custom(signum, _frame):
            received.append(signum)

        old_term = signal.signal(signal.SIGTERM, custom)
        try:
            result = run([sys.executable, "-c", "print('done')"], timeout=10)
            self.assertEqual(result.classification, "ok")
            self.assertIs(signal.getsignal(signal.SIGTERM), custom)
            os.kill(os.getpid(), signal.SIGTERM)
            self.assertEqual(received, [signal.SIGTERM])
        finally:
            signal.signal(signal.SIGTERM, old_term)


if __name__ == "__main__":
    unittest.main()
