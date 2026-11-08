# Xiaomi Mi 9 Pro 5G (crux)

Crux device configuration ported from the PixelExperience **Cepheus** reference
for PixelExperience Plus, Android 13. Shared SM8150 code and compatible donor
blobs are retained intentionally; product identity, hardware-specific inputs and
kernel interfaces target **Crux**. Device and vendor use `thirteen-plus`; the
maintained Cepheus-derived Crux kernel uses `thirteen`.

The product boots through the U-Boot development installation and uses the PE
partitions. See [PORTING.md](PORTING.md) for the port references.

## Source layout

Place these independent Git repositories in a complete Android source checkout:

```text
device/xiaomi/crux     coachpo/device_xiaomi_crux
vendor/xiaomi/crux     coachpo/vendor_xiaomi_crux
kernel/xiaomi/crux-pe-cepheus     coachpo/kernel_xiaomi_crux
```

The target uses the SM8150 platform, ARM64 plus 32-bit userspace, a standalone
recovery, and non-A/B static system/vendor partitions. The kernel builds
`Image-dtb` and the Crux DTBO from `crux_defconfig`.
No fixed-size userdata image is generated: Crux has multiple storage capacities,
and recovery formats the actual partition on the device.

`aosp_crux` loads through the U-Boot development menu and uses `pesystem`,
`pevendor`, `peuserdata` and `pemetadata`. The canonical `fstab.crux_uboot`
supplies its ramdisk/vendor aliases and the Recovery root alias, with the
`crux_uboot` suffix; early development ADB is packaged only for `userdebug` and
`eng`. Installed modem, DSP, Bluetooth firmware and persist keep their existing
contracts. Recovery and cache keep the 64 MiB/256 MiB development limits, and
the system/vendor image limits are 3.5 GiB/1.5 GiB. The ROM never carries the
vendor Recovery updater: the physical Recovery partition holds the U-Boot
Recovery container.

## Dependencies and build

Use a Linux build host with a case-sensitive filesystem and the Android 13 build
prerequisites. The three device repositories alone are not a complete ROM tree.
The PE `thirteen-plus` manifest is the platform baseline; its legacy upstream
services may require separately maintained mirrors.

`aosp.dependencies` explicitly selects the two companion `coachpo` repositories.
It pins `hardware/xiaomi` and Prelude Clang to reviewed revisions. The equivalent
`local_manifest.xml` can be placed at `.repo/local_manifests/pixel.xml` in a fresh
platform checkout, matching PE roomservice's dependency lookup. If that file
already contains other devices, merge these entries into it. Do not declare the
same project path in multiple manifests; update existing entries instead.

After synchronizing the complete tree and applying the local changes to all
three repositories:

```sh
source build/envsetup.sh
lunch aosp_crux-userdebug
m bootimage recoveryimage systemimage vendorimage
```

`aosp_crux-user` and `aosp_crux-eng` are also declared. The images are U-Boot
menu inputs; they are packaged and installed outside this tree. Record the
selected lunch target, the platform manifest and exact revisions:

```sh
repo manifest -r -o crux-build-manifest.xml
repo status
```

A revision manifest does not include uncommitted changes; retain their patches
alongside it. An unchanged branch name does not identify a reproducible build.

## Firmware contract

Install compatible official **Crux** firmware separately before testing the ROM.
The exact minimum stock firmware version has not been established by this tree.
The ROM retains the installed bootloader, modem and other stock firmware. It
does not depend on `vendor/xiaomi-firmware` and has no partition conversion step.

## Proprietary files

`proprietary-files.txt` is the authoritative extraction list. See the companion
vendor tree's `PROVENANCE.md` for the recorded sources and the restored XML files.
Crux-specific sensor registry inputs and the external-modem loading helpers are
recorded separately from the retained Android 13 compatibility donors.
Use the pinned PE extract-utils version documented there when regenerating vendor
makefiles; the generated files should not need manual edits.

```sh
python3 device/xiaomi/crux/update-sha1sums.py --check
```

Existing source/fixup hashes are preserved. Investigate a mismatch against its
recorded source instead of updating hashes to accept unrelated blobs.

## Validation

The local regression suite uses Python 3 and a host C/C++ compiler for the
compiled HAL/driver test harnesses:

```sh
python3 -B -m unittest discover -s device/xiaomi/crux/tests -v
python3 -B device/xiaomi/crux/tests/test_product_contract.py --dump-contracts
python3 -B kernel/xiaomi/crux-pe-cepheus/tools/testing/selftests/drivers/gpu/crux_fod_test.py
python3 -B kernel/xiaomi/crux-pe-cepheus/tools/testing/selftests/kconfig/crux_config_test.py
python3 -B kernel/xiaomi/crux-pe-cepheus/tools/testing/selftests/cpufreq/crux_input_boost_test.py
```

These checks cover local contracts and failure paths. Before distributing a ROM,
also build the complete target, run its VINTF/SELinux/ELF checks, inspect the final
boot and DTBO images, and verify the installer's partition writes. Actual
fingerprint illumination, touch gestures, temperature reporting, radio and
display behavior require device testing. Keep logcat, tombstones and pstore logs
with the source and build records.

The inherited Oreo-to-Pie Widevine data migration hook is removed for this
Pie-launched target. The Widevine HAL and its vendor data directory remain
packaged; the extraction fixup reproduces that init change.
