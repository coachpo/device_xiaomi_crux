"""Exercise module/device readiness without accessing host kernel interfaces."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "rootdir/bin/init.insmod.sh"


class InitInsmodTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "commands"
        self.env = dict(os.environ, PATH=f"{self.root}:/usr/bin:/bin", LOG=str(self.log))
        for name in ("insmod", "modprobe", "setprop"):
            command = self.root / name
            command.write_text(
                '#!/bin/sh\n'
                'name=${0##*/}\n'
                'printf "%s" "$name" >> "$LOG"\n'
                'printf " <%s>" "$@" >> "$LOG"\n'
                'printf "\\n" >> "$LOG"\n'
                '[ "$name" != "$FAIL_COMMAND" ]\n'
            )
            command.chmod(0o755)

    def run_config(self, text, fail_command=""):
        config = self.root / "init.insmod.cfg"
        config.write_text(text)
        return subprocess.run(
            ["/bin/sh", str(SCRIPT), str(config)],
            env=dict(self.env, FAIL_COMMAND=fail_command),
            capture_output=True,
            text=True,
        )

    def commands(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_successful_stages_and_final_line_without_newline(self):
        boot = self.root / "boot"
        result = self.run_config(
            "# Modules\n\nmodprobe|audio_a audio_b\n"
            "setprop|vendor.all.modules.ready\n"
            f"enable|{boot}\n"
            "setprop|vendor.all.devices.ready"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(boot.read_text(), "1\n")
        self.assertEqual(self.commands(), [
            "modprobe <-a> <-d> </vendor/lib/modules> <audio_a> <audio_b>",
            "setprop <vendor.all.modules.ready> <1>",
            "setprop <vendor.all.devices.ready> <1>",
        ])

    def test_failed_module_does_not_publish_ready(self):
        for action in ("insmod", "modprobe"):
            with self.subTest(action=action):
                self.log.unlink(missing_ok=True)
                result = self.run_config(
                    f"{action}|audio.ko\nsetprop|vendor.all.modules.ready\n",
                    fail_command=action,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(self.commands()), 1)
                self.assertTrue(self.commands()[0].startswith(action))

    def test_failed_device_does_not_publish_devices_ready(self):
        result = self.run_config(
            "setprop|vendor.all.modules.ready\n"
            f"enable|{self.root / 'missing' / 'boot'}\n"
            "setprop|vendor.all.devices.ready\n"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.commands(), ["setprop <vendor.all.modules.ready> <1>"])

    def test_failed_property_does_not_continue_to_device(self):
        boot = self.root / "boot"
        result = self.run_config(
            f"setprop|vendor.all.modules.ready\nenable|{boot}\n",
            fail_command="setprop",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(boot.exists())

    def test_missing_config_does_not_publish_ready(self):
        result = subprocess.run(
            ["/bin/sh", str(SCRIPT), str(self.root / "missing.cfg")],
            env=self.env,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.commands(), [])

    def test_unknown_action_does_not_publish_ready(self):
        result = self.run_config("modproeb|audio\nsetprop|vendor.all.modules.ready\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.commands(), [])


if __name__ == "__main__":
    unittest.main()
