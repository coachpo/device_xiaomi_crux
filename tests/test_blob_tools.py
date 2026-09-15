#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


DEVICE_DIR = Path(__file__).resolve().parents[1]


class BlobToolsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.device = self.root / "device/xiaomi/crux"
        self.device.mkdir(parents=True)
        self.blobs = self.root / "vendor/xiaomi/crux/proprietary"
        self.blobs.mkdir(parents=True)
        for name in ("extract-files.sh", "setup-makefiles.sh", "update-sha1sums.py"):
            shutil.copy2(DEVICE_DIR / name, self.device / name)

    def run_pins(self, *args):
        return subprocess.run(
            [sys.executable, str(self.device / "update-sha1sums.py"), *args],
            cwd=self.root, capture_output=True, text=True)

    def pin_fixture(self, content, pin):
        path = self.blobs / "vendor/etc/example.xml"
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
        listing = self.device / "proprietary-files.txt"
        listing.write_text(f"# Test - from donor\nvendor/etc/example.xml|{pin}\n")
        return listing

    def test_check_from_android_root_does_not_write(self):
        data = b"source bytes\n"
        listing = self.pin_fixture(data, hashlib.sha1(data).hexdigest())
        before = listing.read_bytes()
        result = self.run_pins("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1 blobs and 1 SHA-1 pins", result.stdout)
        self.assertEqual(listing.read_bytes(), before)

    def test_mismatched_blob_cannot_replace_source_pin(self):
        listing = self.pin_fixture(b"different bytes", "1" * 40)
        before = listing.read_bytes()
        result = self.run_pins()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHA-1 mismatch", result.stderr)
        self.assertEqual(listing.read_bytes(), before)

    def test_original_and_fixup_hashes_are_preserved(self):
        data = b"fixed-up bytes"
        listing = self.pin_fixture(data, "1" * 40 + "|" + hashlib.sha1(data).hexdigest())
        before = listing.read_bytes()
        result = self.run_pins()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(listing.read_bytes(), before)

    def test_check_rejects_unlisted_and_duplicate_files(self):
        data = b"source bytes"
        listing = self.pin_fixture(data, hashlib.sha1(data).hexdigest())
        listing.write_text(listing.read_text() + "vendor/etc/example.xml\n")
        (self.blobs / "unlisted").write_bytes(b"unexpected")
        result = self.run_pins("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Duplicate destination", result.stderr)
        self.assertIn("Unlisted blob", result.stderr)

    def extraction_fixture(self, failure=False):
        (self.device / "proprietary-files.txt").write_text("")
        helper = self.root / "tools/extract-utils/extract_utils.sh"
        helper.parent.mkdir(parents=True)
        helper.write_text('''setup_vendor() {
    PATCHELF="$ANDROID_ROOT/fake-patchelf"
}
extract() {
    blob_fixup vendor/lib64/hw/camera.qcom.so "$ANDROID_ROOT/blob"
    blob_fixup vendor/lib64/hw/camera.qcom.so "$ANDROID_ROOT/blob"
    blob_fixup vendor/lib64/camera/components/com.qti.node.watermark.so "$ANDROID_ROOT/blob"
    blob_fixup vendor/lib64/camera/components/com.qti.node.watermark.so "$ANDROID_ROOT/blob"
    touch "$ANDROID_ROOT/extraction-complete"
}
write_headers() { touch "$ANDROID_ROOT/build-files-generated"; }
write_makefiles() { :; }
write_footers() { :; }
''')
        patcher = self.root / "fake-patchelf"
        # The fake tool exposes the subprocess failure and dependency-list
        # contract without requiring Android ELF binaries on the host.
        patcher.write_text('''#!/bin/bash
case "$1" in
    --remove-needed) exit 0 ;;
    --print-needed) cat "$2" ;;
    --add-needed) printf '%s\\n' "$2" >> "$3" ;;
    *) exit 99 ;;
esac
''' if not failure else "#!/bin/bash\nexit 23\n")
        patcher.chmod(0o755)
        (self.root / "blob").write_text("libexisting.so\n")

    def run_extraction(self, *args):
        return subprocess.run(
            ["bash", str(self.device / "extract-files.sh"), *args],
            cwd=self.root, capture_output=True, text=True)

    def test_dependency_addition_is_idempotent(self):
        self.extraction_fixture()
        result = self.run_extraction("unused-source")
        self.assertEqual(result.returncode, 0, result.stderr)
        needed = (self.root / "blob").read_text().splitlines()
        self.assertEqual(needed.count("libshim_megvii.so"), 1)
        self.assertEqual(needed.count("libpiex_shim.so"), 1)

    def test_patchelf_failure_aborts_extraction(self):
        self.extraction_fixture(failure=True)
        result = self.run_extraction("unused-source")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "extraction-complete").exists())

    def test_section_requires_a_value(self):
        self.extraction_fixture()
        result = self.run_extraction("--section")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires a section name", result.stderr)
        self.assertFalse((self.root / "extraction-complete").exists())

    def test_missing_input_blocks_build_file_generation(self):
        self.extraction_fixture()
        (self.device / "proprietary-files.txt").write_text("vendor/bin/missing-daemon\n")
        result = self.run_extraction("unused-source")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing blob", result.stderr)
        self.assertFalse((self.root / "build-files-generated").exists())

    def test_widevine_fixup_preserves_hal_and_is_idempotent(self):
        self.extraction_fixture()
        helper = self.root / "tools/extract-utils/extract_utils.sh"
        with helper.open("a") as stream:
            stream.write('''
extract() {
    blob_fixup vendor/etc/init/android.hardware.drm@1.3-service.widevine.rc "$ANDROID_ROOT/widevine.rc"
    blob_fixup vendor/etc/init/android.hardware.drm@1.3-service.widevine.rc "$ANDROID_ROOT/widevine.rc"
}
''')
        rc = self.root / "widevine.rc"
        hal = "service vendor.drm-widevine-hal-1-3 /vendor/bin/hw/android.hardware.drm@1.3-service.widevine\n    class hal\n"
        action = "on property:init.svc.mediadrm=running\n    mkdir /data/vendor/mediadrm 0770 media mediadrm\n"
        rc.write_text(action + "    start vendor.move_data_sh\n\n"
                      "service vendor.move_data_sh /system/bin/move_widevine_data.sh\n"
                      "    class late_start\n    user media\n    disabled\n    oneshot\n\n" + hal)
        result = self.run_extraction("unused-source")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(rc.read_text(), action + "\n" + hal)

    def test_crux_sensor_filters_and_light_wiring(self):
        blobs = DEVICE_DIR.parents[2] / "vendor/xiaomi/crux/proprietary"
        if not blobs.is_dir():
            self.skipTest("Requires the companion vendor checkout")
        configs = blobs / "vendor/etc/sensors/config"
        for path in configs.glob("*.json"):
            config = json.loads(path.read_text())
            platforms = config["config"]["hw_platform"]
            # SHTW2 is a separate inherited optional sensor with no Crux stock
            # source. Its filters must remain disabled on this board.
            if path.name in ("shtw2_0.json", "sm8150_shtw2_0.json"):
                self.assertNotIn("CRUX", platforms, path.name)
            elif "CEPHEUS" in platforms:
                self.assertIn("CRUX", platforms, path.name)
        light = json.loads((configs / "sm8150_tcs3701.json").read_text())
        self.assertEqual(light["tcs3701_platform"][".config"]["dri_irq_num"]["data"], "119")


if __name__ == "__main__":
    unittest.main()
