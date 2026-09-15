#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""Exercise the Crux OTA hooks with real target-files and output ZIP archives."""

import importlib.util
import io
from pathlib import Path
import re
import types
import unittest
from unittest import mock
import warnings
import zipfile


def load_hooks():
    common = types.ModuleType("common")
    common.ZipWriteStr = lambda output, name, data: output.writestr(name, data)
    path = Path(__file__).resolve().parents[1] / "releasetools.py"
    spec = importlib.util.spec_from_file_location("crux_releasetools", path)
    hooks = importlib.util.module_from_spec(spec)
    with mock.patch.dict("sys.modules", {"common": common}):
        spec.loader.exec_module(hooks)
    return hooks


class Script:
    def __init__(self):
        self.commands = []

    def Print(self, _message):
        pass

    def AppendExtra(self, command):
        self.commands.append(command)


class ReleaseToolsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hooks = load_hooks()

    def make_zip(self, entries):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as output:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                for name, contents in entries:
                    output.writestr(name, contents)
        buffer.seek(0)
        archive = zipfile.ZipFile(buffer)
        self.addCleanup(archive.close)
        return archive

    def run_hooks(self, mode, entries):
        target = self.make_zip(entries)
        output = zipfile.ZipFile(io.BytesIO(), "w")
        self.addCleanup(output.close)
        script = Script()
        info = types.SimpleNamespace(output_zip=output, script=script)
        if mode == "FullOTA":
            info.input_zip = target
        else:
            info.target_zip = target
            info.source_zip = self.make_zip([
                ("IMAGES/dtbo.img", b"old-dtbo"),
                ("IMAGES/vbmeta.img", b"old-vbmeta"),
                ("INSTALL/firmware-update/abl.elf", b"old-firmware"),
            ])
        getattr(self.hooks, mode + "_InstallBegin")(info)
        getattr(self.hooks, mode + "_InstallEnd")(info)
        return output, script

    def test_full_and_incremental_preserve_stock_firmware(self):
        entries = [("IMAGES/dtbo.img", b"new-dtbo"),
                   ("IMAGES/vbmeta.img", b"new-vbmeta"),
                   ("IMAGES/boot.img", b"handled-by-platform")]
        for mode in ("FullOTA", "IncrementalOTA"):
            with self.subTest(mode=mode):
                output, script = self.run_hooks(mode, entries)
                self.assertEqual(set(output.namelist()), {"dtbo.img", "vbmeta.img"})
                self.assertEqual(output.read("dtbo.img"), b"new-dtbo")
                self.assertEqual(output.read("vbmeta.img"), b"new-vbmeta")
                writes = []
                for command in script.commands:
                    match = re.fullmatch(
                        r'assert\(package_extract_file\("([^"]+)", '
                        r'"/dev/block/bootdevice/by-name/([^"]+)"\)\);', command)
                    self.assertIsNotNone(match, "Every partition write must abort on failure")
                    writes.append(match.groups())
                self.assertEqual(writes, [("dtbo.img", "dtbo"), ("vbmeta.img", "vbmeta")])

    def test_firmware_in_target_files_is_rejected(self):
        for mode in ("FullOTA", "IncrementalOTA"):
            for path in ("RADIO/abl.elf", "INSTALL/firmware-update/xbl.elf"):
                with self.subTest(mode=mode, path=path):
                    with self.assertRaisesRegex(ValueError, "must not bundle stock firmware"):
                        self.run_hooks(mode, [("IMAGES/dtbo.img", b"dtbo"),
                                              ("IMAGES/vbmeta.img", b"vbmeta"),
                                              (path, b"firmware")])

    def test_missing_empty_or_ambiguous_images_fail_before_emitting_writes(self):
        for image in ("dtbo.img", "vbmeta.img"):
            other = "vbmeta.img" if image == "dtbo.img" else "dtbo.img"
            cases = {
                "missing": [("IMAGES/" + other, b"image")],
                "empty": [("IMAGES/" + other, b"image"), ("IMAGES/" + image, b"")],
                "duplicate": [("IMAGES/" + other, b"image"),
                              ("IMAGES/" + image, b"first"),
                              ("IMAGES/" + image, b"second")],
            }
            for mode in ("FullOTA", "IncrementalOTA"):
                for reason, entries in cases.items():
                    with self.subTest(image=image, mode=mode, reason=reason):
                        target = self.make_zip(entries)
                        with zipfile.ZipFile(io.BytesIO(), "w") as output:
                            script = Script()
                            info = types.SimpleNamespace(
                                input_zip=target, target_zip=target,
                                output_zip=output, script=script)
                            with self.assertRaises(ValueError):
                                getattr(self.hooks, mode + "_InstallEnd")(info)
                            self.assertEqual(output.namelist(), [])
                            self.assertEqual(script.commands, [])


if __name__ == "__main__":
    unittest.main()
