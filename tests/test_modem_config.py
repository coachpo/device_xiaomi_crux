"""Exercise modem-cache updates with isolated paths and host command adapters."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "rootdir/bin/init.qcom.sh"


class ModemConfigTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / "data/vendor/modem_config"
        self.cache.mkdir(parents=True)
        (self.cache / "ver_info.txt").write_text("old version\n")
        (self.cache / "old_profile").mkdir()
        (self.cache / "old_profile/carrier.mbn").write_bytes(b"working cache")
        self.firmware = self.root / "vendor/firmware_mnt"
        self.configs = self.firmware / "image/modem_pr/mcfg/configs"
        self.configs.mkdir(parents=True)
        (self.configs / "new_profile").mkdir()
        (self.configs / "new_profile/carrier.mbn").write_bytes(b"new modem config")
        (self.firmware / "verinfo").mkdir()
        self.version = self.firmware / "verinfo/ver_info.txt"
        self.version.write_text("new version\n")
        self.ota = self.firmware / "image/modem_pr/mbn_ota.txt"
        self.ota.write_text("ota metadata\n")
        self.log = self.root / "commands"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin", LOG=str(self.log))
        # GNU/Android cp options differ from BSD cp. Adapt only the options;
        # copying and renaming still exercise real host filesystem operations.
        adapter = '''#!/bin/sh
name=${0##*/}
printf '%s' "$name" >> "$LOG"
printf ' <%s>' "$@" >> "$LOG"
printf '\\n' >> "$LOG"
case "$name" in
    cp)
        for arg in "$@"; do
            if [ -n "$FAIL_COPY" ]; then
                case "$arg" in *"$FAIL_COPY"*) exit 23 ;; esac
            fi
        done
        [ "$1" = --preserve=m ] || exit 99
        shift
        case "$1" in
            -dr) shift; exec /bin/cp -pRP "$@" ;;
            -d) shift; exec /bin/cp -pP "$@" ;;
            *) exit 99 ;;
        esac ;;
    mv)
        case "$1" in
            */.staging.*/new_profile)
                [ "$FAIL_INSTALL" = 1 ] && exit 24 ;;
        esac
        exec /bin/mv "$@" ;;
    chown) [ "$FAIL_CHOWN" != 1 ] ;;
    setprop) [ "$FAIL_SETPROP" != 1 ] ;;
esac
'''
        for name in ("cp", "mv", "chown", "setprop"):
            command = self.bin / name
            command.write_text(adapter)
            command.chmod(0o755)
        # Keep production paths literal in the script; only this test copy is
        # redirected into the temporary tree.
        self.script = self.root / "init.qcom.sh"
        self.script.write_text(SCRIPT.read_text().replace(
            "/data/vendor/modem_config", str(self.cache)).replace(
            "/vendor/firmware_mnt", str(self.firmware)))

    def run_script(self, **env):
        return subprocess.run(
            ["/bin/sh", str(self.script)], env=dict(self.env, **env),
            capture_output=True, text=True,
        )

    def commands(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def assert_not_ready(self):
        self.assertFalse(any(line.startswith("setprop ") for line in self.commands()))

    def assert_old_cache(self):
        self.assertEqual((self.cache / "ver_info.txt").read_text(), "old version\n")
        self.assertEqual((self.cache / "old_profile/carrier.mbn").read_bytes(),
                         b"working cache")
        self.assertEqual(sorted(p.name for p in self.cache.iterdir()),
                         ["old_profile", "ver_info.txt"])

    def test_missing_firmware_preserves_cache(self):
        self.version.unlink()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assert_old_cache()
        self.assert_not_ready()

    def test_empty_firmware_version_preserves_cache(self):
        self.version.write_text("")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assert_old_cache()
        self.assert_not_ready()

    def test_missing_ota_metadata_preserves_cache(self):
        self.ota.unlink()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assert_old_cache()
        self.assert_not_ready()

    def test_missing_config_directory_preserves_cache(self):
        (self.configs / "new_profile/carrier.mbn").unlink()
        (self.configs / "new_profile").rmdir()
        self.configs.rmdir()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assert_old_cache()
        self.assert_not_ready()

    def test_empty_config_directory_preserves_cache(self):
        (self.configs / "new_profile/carrier.mbn").unlink()
        (self.configs / "new_profile").rmdir()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assert_old_cache()
        self.assert_not_ready()

    def test_each_copy_failure_preserves_cache(self):
        for source in ("configs/new_profile", "mbn_ota.txt", "verinfo/ver_info.txt"):
            with self.subTest(source=source):
                self.log.unlink(missing_ok=True)
                result = self.run_script(FAIL_COPY=source)
                self.assertNotEqual(result.returncode, 0)
                self.assert_old_cache()
                self.assert_not_ready()

    def test_ownership_failure_preserves_cache(self):
        result = self.run_script(FAIL_CHOWN="1")
        self.assertNotEqual(result.returncode, 0)
        self.assert_old_cache()
        self.assert_not_ready()

    def test_failed_install_restores_cache(self):
        result = self.run_script(FAIL_INSTALL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assert_old_cache()
        self.assert_not_ready()

    def test_success_replaces_cache_and_then_publishes_readiness(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.cache / "new_profile/carrier.mbn").read_bytes(),
                         b"new modem config")
        self.assertEqual((self.cache / "ver_info.txt").read_text(), "new version\n")
        self.assertEqual((self.cache / "mbn_ota.txt").read_text(), "ota metadata\n")
        self.assertFalse((self.cache / "old_profile").exists())
        self.assertFalse(any(p.name.startswith(".") for p in self.cache.iterdir()))
        self.assertFalse(self.cache.stat().st_mode & 0o020)
        self.assertEqual(self.commands()[-1],
                         "setprop <ro.vendor.ril.mbn_copy_completed> <1>")

    def test_unchanged_version_does_not_replace_cache(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        profile = self.cache / "new_profile/carrier.mbn"
        original_mtime = profile.stat().st_mtime_ns
        self.log.unlink()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(profile.stat().st_mtime_ns, original_mtime)
        self.assertEqual(self.commands(),
                         ["setprop <ro.vendor.ril.mbn_copy_completed> <1>"])

    def test_matching_version_with_incomplete_cache_is_rebuilt(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        for missing in ("mbn_ota.txt", "new_profile/carrier.mbn"):
            with self.subTest(missing=missing):
                (self.cache / missing).unlink()
                self.log.unlink()
                result = self.run_script()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((self.cache / missing).is_file())
                self.assertTrue(any(line.startswith("cp ") for line in self.commands()))
                self.assertEqual(self.commands()[-1],
                                 "setprop <ro.vendor.ril.mbn_copy_completed> <1>")

    def test_matching_version_with_empty_config_file_is_rebuilt(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        profile = self.cache / "new_profile/carrier.mbn"
        profile.write_bytes(b"")
        self.log.unlink()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(profile.read_bytes(), b"new modem config")
        self.assertTrue(any(line.startswith("cp ") for line in self.commands()))

    def test_failed_readiness_property_is_reported(self):
        result = self.run_script(FAIL_SETPROP="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.cache / "ver_info.txt").read_text(), "new version\n")


if __name__ == "__main__":
    unittest.main()
