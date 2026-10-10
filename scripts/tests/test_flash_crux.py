"""Runner contracts with tiny images and a mocked fastboot process boundary."""

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "flash_crux.py"
SPEC = importlib.util.spec_from_file_location("flash_crux_under_test", SCRIPT)
FLASH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FLASH)
SERIAL = "fixture-crux"
FINGERPRINT = "Xiaomi/crux/crux:13/TQ3A.230901.001.B1/2026100906:user/release-keys"


class FlashTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="crux-fastboot-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.bundle = Path(self.temporary.name).resolve()
        (self.bundle / "images").mkdir()
        self.manifest = {"schema_version": 1, "product": "crux", "fingerprint": FINGERPRINT,
                         "version": "2026100906", "images": {}}
        self.capacities = {}
        for partition in FLASH.FLASH_ORDER:
            data = b"fixture-" + partition.encode()
            expanded = len(data)
            if partition == "system":
                data = struct.pack("<IHHHHIIII", FLASH.SPARSE_MAGIC, 1, 0, 28, 12,
                                   4096, 2, 1, 0) + struct.pack("<HHII", 0xCAC3, 0, 2, 12)
                expanded = 8192
            name = "images/" + partition + ".img"
            (self.bundle / name).write_bytes(data)
            self.manifest["images"][partition] = {"file": name, "bytes": len(data),
                "expanded_bytes": expanded, "sha256": hashlib.sha256(data).hexdigest()}
            self.capacities[partition] = expanded + 4096
        (self.bundle / "manifest.json").write_text(json.dumps(self.manifest))
        self.unlocked = "yes"
        self.oem_unlocked = "true"
        self.userspace = "no"
        self.fail_partition = None
        self.fail_exit_code = 7
        self.calls = []
        self.log = self.bundle / "result.json"

    def process(self, command, **kwargs):
        self.calls.append(command)
        self.assertEqual(command[:3], ["fixture-fastboot", "-s", SERIAL])
        arguments = command[3:]
        exit_code, output = 0, ""
        if arguments == ["devices"]:
            output = SERIAL + "\tfastboot\n"
        elif arguments == ["oem", "device-info"]:
            output = "(bootloader) Device unlocked: " + self.oem_unlocked + "\nOKAY\n"
        elif arguments[0] == "getvar":
            variable = arguments[1]
            if variable == "product":
                value = "crux"
            elif variable == "unlocked":
                value = self.unlocked
            elif variable == "is-userspace":
                value = self.userspace
            else:
                self.assertTrue(variable.startswith("partition-size:"))
                value = hex(self.capacities[variable.split(":", 1)[1]])
            if value is None:
                exit_code, output = 1, "FAILED (remote: 'GetVar Variable Not found')\n"
            else:
                output = variable + ": " + value + "\nFinished. Total time: 0.001s\n"
        elif arguments[0] == "flash":
            self.assertIn(arguments[1], FLASH.FLASH_ORDER)
            self.assertEqual(Path(arguments[2]), self.bundle / "images" / (arguments[1] + ".img"))
            if arguments[1] == self.fail_partition:
                exit_code, output = self.fail_exit_code, "FAILED (remote: 'fixture write failure')\n"
            else:
                output = "OKAY\n"
        else:
            self.assertEqual(arguments, ["reboot"])
        return SimpleNamespace(returncode=exit_code, stdout="", stderr=output)

    def invoke(self, *flags):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(FLASH.subprocess, "run", side_effect=self.process), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = FLASH.main(["--serial", SERIAL, "--fastboot", "fixture-fastboot",
                               "--bundle-dir", str(self.bundle), "--log", str(self.log), *flags])
        report = json.loads(self.log.read_text())
        return code, report, stderr.getvalue()

    def test_valid_flow_preflights_every_partition_then_flashes_in_order(self):
        code, report, error = self.invoke()
        self.assertEqual(code, 0, error)
        flashes = [command for command in self.calls if command[3] == "flash"]
        self.assertEqual([command[4] for command in flashes], list(FLASH.FLASH_ORDER))
        first_flash = self.calls.index(flashes[0])
        sizes = [command[4] for command in self.calls[:first_flash]
                 if command[3] == "getvar" and command[4].startswith("partition-size:")]
        self.assertEqual(sizes, ["partition-size:" + p for p in FLASH.FLASH_ORDER])
        self.assertEqual(self.calls[-1][3:], ["reboot"])
        self.assertEqual(report["status"], "flashed")
        self.assertEqual(report["evidence"]["images"]["system"]["expanded_bytes"], 8192)
        self.assertEqual(len(report["commands"]), len(self.calls))
        self.assertTrue(all(row["exit_code"] == 0 for row in report["commands"]))

    def test_locked_and_contradictory_loader_evidence_rejects_before_writes(self):
        for unlocked, oem in [("no", "false"), ("yes", "false"), ("no", "true")]:
            with self.subTest(unlocked=unlocked, oem=oem):
                self.unlocked, self.oem_unlocked = unlocked, oem
                self.calls = []
                self.log = self.bundle / ("locked-" + unlocked + "-" + oem + ".json")
                code, report, _ = self.invoke()
                self.assertEqual(code, 1)
                self.assertEqual(report["status"], "failed")
                self.assertFalse(any(command[3] in ("flash", "reboot") for command in self.calls))

    def test_corrupt_image_rejects_before_any_device_command(self):
        path = self.bundle / "images/boot.img"
        original = path.read_bytes()
        path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        code, report, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("SHA256 mismatch", report["error"])
        self.assertEqual(self.calls, [])

    def test_insufficient_expanded_capacity_rejects_before_writes(self):
        self.capacities["system"] = 4096
        code, report, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("too small", report["error"])
        self.assertEqual(set(report["evidence"]["partition_sizes"]), set(FLASH.FLASH_ORDER))
        self.assertFalse(any(command[3] in ("flash", "reboot") for command in self.calls))

    def test_failed_flash_stops_without_later_images_or_reboot(self):
        self.fail_partition = "system"
        code, report, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual([command[4] for command in self.calls if command[3] == "flash"],
                         ["recovery", "system"])
        self.assertEqual(report["commands"][-1]["exit_code"], 7)
        self.assertIn("fixture write failure", report["commands"][-1]["stderr"])
        self.assertFalse(any(command[3] == "reboot" for command in self.calls))

    def test_oem_fallback_supports_stock_loader_missing_optional_variables(self):
        self.unlocked = self.userspace = None
        code, report, error = self.invoke()
        self.assertEqual(code, 0, error)
        self.assertIsNone(report["evidence"]["unlocked_getvar"])
        self.assertIsNone(report["evidence"]["is_userspace"])
        self.assertTrue(report["evidence"]["unlocked_oem"])

    def test_remote_failed_response_rejected_even_when_process_exits_zero(self):
        self.fail_partition = "system"
        self.fail_exit_code = 0
        code, report, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["commands"][-1]["exit_code"], 0)
        self.assertTrue(report["commands"][-1]["remote_failed"])
        self.assertEqual([command[4] for command in self.calls if command[3] == "flash"],
                         ["recovery", "system"])
        self.assertFalse(any(command[3] == "reboot" for command in self.calls))

    def test_userspace_fastboot_rejected_before_writes(self):
        self.userspace = "yes"
        code, report, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("Userspace fastboot", report["error"])
        self.assertFalse(any(command[3] in ("flash", "reboot") for command in self.calls))

    def test_dry_run_records_plan_without_flash_or_reboot(self):
        code, report, error = self.invoke("--dry-run")
        self.assertEqual(code, 0, error)
        self.assertEqual(report["status"], "dry_run_complete")
        self.assertEqual([row[4] for row in report["planned_commands"] if row[3] == "flash"],
                         list(FLASH.FLASH_ORDER))
        self.assertFalse(any(command[3] in ("flash", "reboot") for command in self.calls))

    def test_no_reboot_leaves_loader_after_all_six_images(self):
        code, report, error = self.invoke("--no-reboot")
        self.assertEqual(code, 0, error)
        self.assertEqual(len([command for command in self.calls if command[3] == "flash"]), 6)
        self.assertEqual(self.calls[-1][3:5], ["flash", "boot"])
        self.assertEqual(report["status"], "flashed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
