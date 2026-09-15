"""Run the actual fingerprint wrapper against host HAL and device-interface fakes."""
from pathlib import Path
import subprocess
import sys
import unittest


class FingerprintHostTest(unittest.TestCase):
    def test_hal_lifetime_and_fod_state(self):
        runner = Path(__file__).resolve().parent / "fingerprint_host" / "run.py"
        result = subprocess.run(
            [sys.executable, str(runner)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=90,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("30 host regression cases passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
