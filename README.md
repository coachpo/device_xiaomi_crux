# Xiaomi Mi 9 Pro 5G (crux)

Crux device configuration ported from the PixelExperience **Cepheus** reference
for PixelExperience Plus, Android 13. Shared SM8150 code and compatible donor
blobs are retained intentionally; product identity, hardware-specific inputs and
kernel interfaces target **Crux**. `thirteen-plus` is the only working branch in
all three repositories.

This is a source bring-up baseline without a completed ROM build or hardware
qualification. See [PORTING.md](PORTING.md) for the source contracts, references
and the checks to run when a build host and device become available.

## Source layout

Place these independent Git repositories in a complete Android source checkout:

```text
device/xiaomi/crux     coachpo/device_xiaomi_crux
vendor/xiaomi/crux     coachpo/vendor_xiaomi_crux
kernel/xiaomi/crux     coachpo/kernel_xiaomi_crux
```

The target uses the SM8150 platform, ARM64 plus 32-bit userspace, a standalone
recovery, and non-A/B static system/vendor partitions. The kernel builds
`Image-dtb` and the Crux DTBO from `crux_defconfig`. First-stage mounts are
specified by `rootdir/etc/fstab.qcom`; the legacy DT fstab is disabled by the
Crux overlay.
No fixed-size userdata image is generated: Crux has multiple storage capacities,
and recovery formats the actual partition on the device.

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
m target-files-package otapackage
```

`aosp_crux-user` and `aosp_crux-eng` are also declared. Record the selected lunch
target, the platform manifest and the exact source revisions with every build:

```sh
repo manifest -r -o crux-build-manifest.xml
repo status
```

A revision manifest does not include uncommitted changes; retain their patches
alongside it. An unchanged branch name does not identify a reproducible build.

## Firmware and OTA contract

Install compatible official **Crux** firmware separately before testing the ROM.
The exact minimum stock firmware version has not been established by this tree.
The ROM retains the installed bootloader, modem and other stock firmware. It
does not depend on `vendor/xiaomi-firmware` and has no partition conversion step.

The platform's non-A/B OTA generator handles Android partitions such as boot,
system and vendor. The device extension additionally installs **DTBO and vbmeta
from the same target-files archive**, checking each write result. Missing, empty
or duplicate required images and bundled `RADIO/` or `INSTALL/firmware-update/`
payloads fail OTA generation. This contract is identical for full and incremental
packages; incremental hooks use the target archive, not the old source images.

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
python3 -B kernel/xiaomi/crux/tools/testing/selftests/drivers/gpu/crux_fod_test.py
python3 -B kernel/xiaomi/crux/tools/testing/selftests/kconfig/crux_config_test.py
python3 -B kernel/xiaomi/crux/tools/testing/selftests/cpufreq/crux_input_boost_test.py
```

These checks cover local contracts and failure paths. Before distributing a ROM,
also build the complete target, run its VINTF/SELinux/ELF checks, inspect the final
boot and DTBO images, and verify the generated OTA partition writes. Actual
fingerprint illumination, touch gestures, temperature reporting, radio and
display behavior require device testing. Keep logcat, tombstones and pstore logs
with the source and build records.

The inherited Oreo-to-Pie Widevine data migration hook is removed for this
Pie-launched target. The Widevine HAL and its vendor data directory remain
packaged; the extraction fixup reproduces that init change.
