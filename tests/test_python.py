"""Python-edition regressions that only a real process can show.

Run with: python3 tests/test_python.py
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))

from bounded_run import run  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
