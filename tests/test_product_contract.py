"""Evaluate actual Crux product/device/board inputs using host make."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


DEVICE = Path(__file__).resolve().parents[1]
ANDROID = DEVICE.parents[2]
PRODUCTS = ("aosp_crux", "aosp_crux_release")
VARIANTS = ("user", "userdebug", "eng")
VARIABLES = (
    "PRODUCT_MAKEFILES", "COMMON_LUNCH_CHOICES", "PRODUCT_NAME", "PRODUCT_DEVICE",
    "PRODUCT_BRAND", "PRODUCT_MODEL", "PRODUCT_MANUFACTURER",
    "TARGET_OTA_ASSERT_DEVICE", "TARGET_USES_AOSP_RECOVERY",
    "TARGET_KERNEL_SOURCE", "TARGET_KERNEL_CONFIG", "BOARD_BOOT_HEADER_VERSION",
    "BOARD_MKBOOTIMG_ARGS", "BOARD_KERNEL_CMDLINE",
    "BOARD_KERNEL_SEPARATED_DTBO", "BOARD_INCLUDE_RECOVERY_DTBO",
    "BOARD_USES_FULL_RECOVERY_IMAGE", "BOARD_PREBUILT_DTBOIMAGE",
    "BOARD_PREBUILT_RECOVERY_DTBOIMAGE", "TARGET_KERNEL_DTBO",
    "BOARD_BOOTIMAGE_PARTITION_SIZE", "BOARD_RECOVERYIMAGE_PARTITION_SIZE",
    "BOARD_CACHEIMAGE_PARTITION_SIZE", "BOARD_DTBOIMG_PARTITION_SIZE",
    "BOARD_VBMETAIMAGE_PARTITION_SIZE", "BOARD_SYSTEMIMAGE_PARTITION_SIZE",
    "BOARD_VENDORIMAGE_PARTITION_SIZE", "PRODUCT_BUILD_USERDATA_IMAGE",
    "BOARD_USERDATAIMAGE_PARTITION_SIZE", "BOARD_SUPER_PARTITION_SIZE",
    "AB_OTA_UPDATER", "BOARD_AVB_MAKE_VBMETA_IMAGE_ARGS", "BOARD_USES_METADATA_PARTITION",
    "TARGET_RECOVERY_FSTAB", "PRODUCT_COPY_FILES", "PRODUCT_PACKAGES",
    "PRODUCT_VENDOR_PROPERTIES", "PRODUCT_SYSTEM_DEFAULT_PROPERTIES",
)


def evaluate_contract(product, variant):
    make = shutil.which("make")
    if not make:
        raise unittest.SkipTest("Host make is required")
    with tempfile.TemporaryDirectory(prefix="crux-product-") as temporary:
        root = Path(temporary)
        for relative, source in (
            ("device/xiaomi/crux", DEVICE),
            ("vendor/xiaomi/crux", ANDROID / "vendor/xiaomi/crux"),
            ("vendor/aosp", ANDROID / "vendor/aosp"),
            ("kernel/xiaomi/crux-pe-cepheus", ANDROID / "kernel/xiaomi/crux-pe-cepheus"),
            ("build/make/target/product/non_ab_device.mk",
             ANDROID / "build/make/target/product/non_ab_device.mk"),
        ):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(source, target_is_directory=source.is_dir())
        policy = root / "device/qcom/sepolicy_vndr-legacy-um/SEPolicy.mk"
        policy.parent.mkdir(parents=True)
        policy.write_text("")
        harness = root / "contract.mk"
        # Evaluate local inheritance and the explicit non-A/B package contract.
        # Other platform products and merged policy remain image-check boundaries.
        harness.write_text(
            "inherit-product = $(if $(filter device/xiaomi/crux/% "
            "$(SRC_TARGET_DIR)/product/non_ab_device.mk,$(1)),$(eval include $(1)))\n"
            "SRC_TARGET_DIR := build/make/target\n"
            "LOCAL_DIR := device/xiaomi/crux\nLOCAL_PATH := device/xiaomi/crux\n"
            "TARGET_COPY_OUT_SYSTEM := system\nTARGET_COPY_OUT_SYSTEM_EXT := system_ext\n"
            "TARGET_COPY_OUT_VENDOR := vendor\nTARGET_COPY_OUT_PRODUCT := product\n"
            "TARGET_COPY_OUT_RAMDISK := root\nTARGET_COPY_OUT_RECOVERY := recovery\n"
            "TARGET_DEVICE_DIR := device/xiaomi/crux\n"
            "TARGET_OUT_INTERMEDIATES := out/target/product/crux/obj\n"
            "OUT_DIR := out\nHOST_PREBUILT_TAG := linux-x86\n"
            "include device/xiaomi/crux/AndroidProducts.mk\n"
            f"include device/xiaomi/crux/{product}.mk\n"
            "include device/xiaomi/crux/BoardConfig.mk\n"
            "include vendor/aosp/config/BoardConfigKernel.mk\n" +
            "\n".join(f"$(info {key}=$({key}))" for key in VARIABLES) +
            "\n.PHONY: check\ncheck:\n\t@:\n")
        result = subprocess.run(
            [make, "--no-print-directory", "-f", str(harness), "check",
             f"TARGET_PRODUCT={product}", f"TARGET_BUILD_VARIANT={variant}"],
            cwd=root, text=True, capture_output=True)
        if result.returncode:
            raise AssertionError(f"make exit {result.returncode}:\n{result.stdout}{result.stderr}")
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def read_fstab(relative):
    entries = {}
    for line in (ANDROID / relative).read_text().splitlines():
        fields = line.split("#", 1)[0].split()
        if fields:
            if len(fields) != 5:
                raise AssertionError(f"Invalid fstab entry: {line}")
            source, mount, kind, options, flags = fields
            if mount in entries:
                raise AssertionError(f"Duplicate mount point: {mount}")
            entries[mount] = (source, kind, set(options.split(",")), set(flags.split(",")))
    return entries


class ProductContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contracts = {(product, variant): evaluate_contract(product, variant)
                         for product in PRODUCTS for variant in VARIANTS}

    def test_identity_boot_and_static_image_contract(self):
        common = {
            "PRODUCT_DEVICE": "crux", "PRODUCT_BRAND": "Xiaomi",
            "PRODUCT_MODEL": "Mi9 Pro 5G", "PRODUCT_MANUFACTURER": "Xiaomi",
            "TARGET_OTA_ASSERT_DEVICE": "crux", "TARGET_USES_AOSP_RECOVERY": "true",
            "TARGET_KERNEL_SOURCE": "kernel/xiaomi/crux-pe-cepheus",
            "TARGET_KERNEL_CONFIG": "crux_defconfig", "BOARD_BOOT_HEADER_VERSION": "1",
            "BOARD_BOOTIMAGE_PARTITION_SIZE": "134217728",
            "BOARD_DTBOIMG_PARTITION_SIZE": "33554432",
            "BOARD_VBMETAIMAGE_PARTITION_SIZE": "131072",
            "BOARD_SYSTEMIMAGE_PARTITION_SIZE": "3758096384",
            "BOARD_VENDORIMAGE_PARTITION_SIZE": "1610612736",
            "PRODUCT_BUILD_USERDATA_IMAGE": "false", "BOARD_USERDATAIMAGE_PARTITION_SIZE": "",
            "BOARD_SUPER_PARTITION_SIZE": "", "BOARD_USES_METADATA_PARTITION": "true",
        }
        for (product, variant), values in self.contracts.items():
            with self.subTest(product=product, variant=variant):
                for key, expected in common.items():
                    self.assertEqual(values[key], expected, key)
                self.assertEqual(values["PRODUCT_NAME"], product)
                release = product == "aosp_crux_release"
                self.assertEqual(values["BOARD_RECOVERYIMAGE_PARTITION_SIZE"],
                                 "134217728" if release else "67108864")
                self.assertEqual(values["BOARD_CACHEIMAGE_PARTITION_SIZE"],
                                 "402653184" if release else "268435456")
                self.assertIn("--header_version 1", values["BOARD_MKBOOTIMG_ARGS"])
                self.assertIn("--flags 3", values["BOARD_AVB_MAKE_VBMETA_IMAGE_ARGS"])
                self.assertNotEqual(values["AB_OTA_UPDATER"], "true")
                self.assertIn(f"device/xiaomi/crux/{product}.mk",
                              values["PRODUCT_MAKEFILES"].split())
                self.assertIn(f"{product}-{variant}", values["COMMON_LUNCH_CHOICES"].split())

    def test_public_recovery_image_contract(self):
        for (product, variant), values in self.contracts.items():
            with self.subTest(product=product, variant=variant):
                self.assertEqual(values["BOARD_KERNEL_SEPARATED_DTBO"], "true")
                for key in ("BOARD_INCLUDE_RECOVERY_DTBO", "BOARD_USES_FULL_RECOVERY_IMAGE"):
                    self.assertEqual(values[key],
                                     "true" if product == "aosp_crux_release" else "")
                self.assertEqual(values["BOARD_PREBUILT_RECOVERY_DTBOIMAGE"], "")
                self.assertEqual(values["BOARD_PREBUILT_DTBOIMAGE"],
                                 "out/target/product/crux/obj/DTBO_OBJ/arch/arm64/boot/dtbo.img")
                self.assertEqual(values["TARGET_KERNEL_DTBO"], "dtbo.img")
                release = product == "aosp_crux_release"
                self.assertEqual("applypatch" in values["PRODUCT_PACKAGES"].split(), release)
                defaults = [prop for prop in values["PRODUCT_SYSTEM_DEFAULT_PROPERTIES"].split()
                            if prop.startswith("persist.sys.recovery_update=")]
                self.assertEqual(defaults, ["persist.sys.recovery_update=true"] if release else [])

    def test_storage_and_early_adb_are_selected_by_product(self):
        init = ("device/xiaomi/crux/rootdir/etc/init.crux-development-adb.rc:"
                "system/etc/init/init.crux-development-adb.rc")
        for (product, variant), values in self.contracts.items():
            with self.subTest(product=product, variant=variant):
                release = product == "aosp_crux_release"
                fstab = "fstab.qcom" if release else "fstab.crux_uboot"
                source = f"device/xiaomi/crux/rootdir/etc/{fstab}"
                expected = {f"{source}:vendor/etc/fstab.qcom", f"{source}:root/fstab.qcom"}
                if not release:
                    expected.update({f"{source}:vendor/etc/fstab.crux_uboot",
                                     f"{source}:root/fstab.crux_uboot",
                                     f"{source}:recovery/root/fstab.crux_uboot"})
                copies = values["PRODUCT_COPY_FILES"].split()
                self.assertEqual({copy for copy in copies if "/fstab." in copy}, expected)
                self.assertEqual(values["TARGET_RECOVERY_FSTAB"], source)
                development = not release and variant in ("userdebug", "eng")
                self.assertEqual("adbd_crux" in values["PRODUCT_PACKAGES"].split(), development)
                self.assertEqual(init in copies, development)
                self.assertEqual([prop for prop in values["PRODUCT_VENDOR_PROPERTIES"].split()
                                  if prop.startswith("ro.vendor.crux.early_adb=")],
                                 [f"ro.vendor.crux.early_adb={int(development)}"])

    def test_public_cmdline_omits_development_settings(self):
        common = {"androidboot.hardware=qcom", "androidboot.memcg=1",
                  "androidboot.usbcontroller=a600000.dwc3", "service_locator.enable=1",
                  "swiotlb=2048", "loop.max_part=7",
                  "androidboot.boot_devices=soc/1d84000.ufshc"}
        development = {"console=ttyMSM0,115200n8", "earlycon=msm_geni_serial,0xa90000",
                       "androidboot.console=ttyMSM0", "lpm_levels.sleep_disabled=1",
                       "video=vfb:640x400,bpp=32,memsize=3072000", "msm_rtb.filter=0x237",
                       "androidboot.init_fatal_reboot_target=bootloader",
                       "androidboot.fstab_suffix=crux_uboot", "kpti=off"}
        for (product, variant), values in self.contracts.items():
            with self.subTest(product=product, variant=variant):
                arguments = set(values["BOARD_KERNEL_CMDLINE"].split())
                self.assertTrue(common <= arguments)
                if product == "aosp_crux_release":
                    keys = {argument.split("=", 1)[0] for argument in arguments}
                    forbidden = {argument.split("=", 1)[0] for argument in development}
                    self.assertTrue(keys.isdisjoint(forbidden), keys & forbidden)
                else:
                    self.assertTrue(development <= arguments)

    def test_public_blank_userdata_initialization_contract(self):
        for (product, variant), values in self.contracts.items():
            with self.subTest(product=product, variant=variant):
                entries = read_fstab(values["TARGET_RECOVERY_FSTAB"])
                # Recovery raw-wipes metadata-encrypted data; init must format blank public data.
                self.assertEqual("formattable" in entries["/data"][3],
                                 product == "aosp_crux_release")

    def test_selected_fstab_preserves_encryption_firmware_and_recovery_contracts(self):
        for (product, variant), values in self.contracts.items():
            entries = read_fstab(values["TARGET_RECOVERY_FSTAB"])
            with self.subTest(product=product, variant=variant):
                if product == "aosp_crux_release":
                    # Coldboot supplies direct by-name aliases before second-stage init.
                    first_stage = {mount: entry[0] for mount, entry in entries.items()
                                   if "first_stage_mount" in entry[3]}
                    self.assertEqual(first_stage, {
                        "/system": "/dev/block/by-name/system",
                        "/vendor": "/dev/block/by-name/vendor",
                        "/metadata": "/dev/block/by-name/metadata",
                        "/vbmeta": "/dev/block/by-name/vbmeta",
                    })
                prefix = "" if product == "aosp_crux_release" else "pe"
                for mount, partition in (("/system", "system"), ("/vendor", "vendor"),
                                         ("/metadata", "metadata"), ("/data", "userdata")):
                    source, kind, options, flags = entries[mount]
                    self.assertEqual(source.rsplit("/", 1)[-1], prefix + partition)
                    self.assertEqual(kind, "ext4")
                    if mount in ("/system", "/vendor"):
                        self.assertIn("ro", options)
                    if mount != "/data":
                        self.assertIn("first_stage_mount", flags)
                self.assertTrue({"check", "formattable"} <= entries["/metadata"][3])
                self.assertIn("inlinecrypt", entries["/data"][2])
                self.assertEqual({flag for flag in entries["/data"][3]
                                  if flag.startswith("fileencryption=")},
                                 {"fileencryption=ice::v2"} if product == "aosp_crux_release"
                                 else {"fileencryption=ice"})
                self.assertTrue({"latemount", "wait", "check",
                                 "keydirectory=/metadata/vold/metadata_encryption", "quota",
                                 "reservedsize=128M"} <= entries["/data"][3])
                for mount, partition in (("/vendor/firmware_mnt", "modem"),
                                         ("/vendor/dsp", "dsp"),
                                         ("/vendor/bt_firmware", "bluetooth")):
                    self.assertEqual(entries[mount][0].rsplit("/", 1)[-1], partition)
                    self.assertIn("ro", entries[mount][2])
                self.assertEqual(entries["/mnt/vendor/persist"][0].rsplit("/", 1)[-1], "persist")
                if product == "aosp_crux_release":
                    for mount in ("/boot", "/recovery"):
                        self.assertEqual(entries[mount][0].rsplit("/", 1)[-1], mount[1:])
                        self.assertEqual(entries[mount][1], "emmc")
                        self.assertIn("recoveryonly", entries[mount][3])
                    self.assertEqual(entries["/misc"][0].rsplit("/", 1)[-1], "misc")
                    self.assertEqual(entries["/misc"][1], "emmc")
                    self.assertEqual(entries["/cache"][0].rsplit("/", 1)[-1], "cache")
                    self.assertEqual(entries["/cache"][1], "ext4")
                    self.assertIn("wait", entries["/cache"][3])
                else:
                    self.assertTrue({"/boot", "/recovery", "/misc", "/cache"}.isdisjoint(entries))


if __name__ == "__main__":
    if sys.argv[1:] == ["--dump-contracts"]:
        for product in PRODUCTS:
            for variant in VARIANTS:
                values = evaluate_contract(product, variant)
                copies = values.pop("PRODUCT_COPY_FILES").split()
                packages = values.pop("PRODUCT_PACKAGES").split()
                values["FSTAB_COPY_FILES"] = [copy for copy in copies if "/fstab." in copy]
                values["DEVELOPMENT_ADB_INIT_FILES"] = [copy for copy in copies
                                                      if "init.crux-development-adb.rc" in copy]
                values["ADBD_CRUX_PACKAGED"] = "adbd_crux" in packages
                values["APPLYPATCH_PACKAGED"] = "applypatch" in packages
                print(json.dumps({"product": product, "variant": variant, "values": values},
                                 indent=2, sort_keys=True))
    else:
        unittest.main()
