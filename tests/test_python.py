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


if __name__ == "__main__":
    unittest.main()
