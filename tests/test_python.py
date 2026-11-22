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


if __name__ == "__main__":
    unittest.main()
