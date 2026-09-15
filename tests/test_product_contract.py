"""Evaluate the Crux product/board inputs with make, without a platform checkout."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


DEVICE = Path(__file__).resolve().parents[1]
ANDROID = DEVICE.parents[2]


class ProductContractTest(unittest.TestCase):
    def test_crux_identity_boot_and_storage_contract(self):
        make = shutil.which("make")
        if not make:
            self.skipTest("Host make is required")
        with tempfile.TemporaryDirectory(prefix="crux-product-") as temporary:
            root = Path(temporary)
            for relative, source in (
                ("device/xiaomi/crux", DEVICE),
                ("vendor/xiaomi/crux", ANDROID / "vendor/xiaomi/crux"),
            ):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(source, target_is_directory=True)
            # These are platform boundaries. This test evaluates the actual local
            # product/board files, not PE's inherited product or merged policy.
            policy = root / "device/qcom/sepolicy_vndr-legacy-um/SEPolicy.mk"
            policy.parent.mkdir(parents=True)
            policy.write_text("")
            harness = root / "contract.mk"
            variables = (
                "PRODUCT_NAME", "PRODUCT_DEVICE", "TARGET_OTA_ASSERT_DEVICE",
                "TARGET_KERNEL_SOURCE", "TARGET_KERNEL_CONFIG",
                "BOARD_BOOT_HEADER_VERSION", "BOARD_MKBOOTIMG_ARGS",
                "BOARD_SYSTEMIMAGE_PARTITION_SIZE", "BOARD_VENDORIMAGE_PARTITION_SIZE",
                "PRODUCT_BUILD_USERDATA_IMAGE", "BOARD_USERDATAIMAGE_PARTITION_SIZE",
                "BOARD_SUPER_PARTITION_SIZE", "AB_OTA_UPDATER",
                "BOARD_AVB_MAKE_VBMETA_IMAGE_ARGS", "TARGET_RECOVERY_FSTAB",
            )
            harness.write_text(
                "inherit-product =\n"
                "include device/xiaomi/crux/aosp_crux.mk\n"
                "include device/xiaomi/crux/BoardConfig.mk\n" +
                "\n".join(f"$(info {key}=$({key}))" for key in variables) +
                "\n.PHONY: check\ncheck:\n\t@:\n")
            result = subprocess.run([make, "--no-print-directory", "-f", str(harness), "check"],
                                    cwd=root, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            self.assertEqual(values["PRODUCT_NAME"], "aosp_crux")
            self.assertEqual(values["PRODUCT_DEVICE"], "crux")
            self.assertEqual(values["TARGET_OTA_ASSERT_DEVICE"], "crux")
            self.assertEqual(values["TARGET_KERNEL_SOURCE"], "kernel/xiaomi/crux")
            self.assertEqual(values["TARGET_KERNEL_CONFIG"], "crux_defconfig")
            self.assertEqual(values["BOARD_BOOT_HEADER_VERSION"], "1")
            self.assertIn("--header_version 1", values["BOARD_MKBOOTIMG_ARGS"])
            self.assertGreater(int(values["BOARD_SYSTEMIMAGE_PARTITION_SIZE"]), 0)
            self.assertGreater(int(values["BOARD_VENDORIMAGE_PARTITION_SIZE"]), 0)
            self.assertEqual(values["PRODUCT_BUILD_USERDATA_IMAGE"], "false")
            self.assertEqual(values["BOARD_USERDATAIMAGE_PARTITION_SIZE"], "")
            self.assertEqual(values["BOARD_SUPER_PARTITION_SIZE"], "")
            self.assertNotEqual(values["AB_OTA_UPDATER"], "true")
            self.assertIn("--flags 3", values["BOARD_AVB_MAKE_VBMETA_IMAGE_ARGS"])
            self.assertEqual(values["TARGET_RECOVERY_FSTAB"],
                             "device/xiaomi/crux/rootdir/etc/fstab.qcom")


if __name__ == "__main__":
    unittest.main()
