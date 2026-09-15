# Crux port baseline

## Target and reference

This target ports the PixelExperience Plus Android 13 **Cepheus** implementation
to **Crux (Xiaomi Mi 9 Pro 5G)**. The shared SM8150 architecture is the reason to
reuse the donor tree. Shared code and recorded compatibility donors remain valid
inputs; Crux-specific device selection and hardware configuration are maintained
explicitly. All three repositories use `thirteen-plus` as their sole working
branch.

The baseline has host-side source and regression checks. A complete ROM build
and a test device were not available for this revision, so the table below
describes source integration, not a list of hardware certified to work.

## Source contracts

| Area | Crux baseline |
| --- | --- |
| Product | `aosp_crux`, `PRODUCT_DEVICE=crux`, Crux OTA assertion, `Mi9 Pro 5G` identity. No EEA product suffix is invented. |
| Platform | SM8150-AC / Snapdragon 855 Plus; minimum 8 GiB RAM profile; battery statistics use the specified 4000 mAh typical capacity. |
| Kernel | `kernel/xiaomi/crux`, `crux_defconfig`, `Image-dtb`, Crux DTBO; one Crux Kconfig declaration and 4+3+1 CPU masks. `/proc/config.gz` reflects the actual build configuration. |
| Storage | Non-A/B, separate recovery, static system/vendor partitions. The Crux DT overlay selects the packaged first-stage fstab. No dynamic partition conversion or fixed-capacity userdata image is included. |
| Radio | External SDX50 loader and its dependencies supplied by a pinned Crux dump; either SIM can be the primary NR data SIM. Framework declares NSA support and no SA support; default network modes include NR. |
| Sensors | 41 pinned Crux registry JSONs with the `CRUX` platform selector; compatible donor HAL/DSP-facing libraries retained. |
| Fingerprint / touch | Goodix wrapper, actual Xiaomi touch ioctl, FTS double-tap control, and PE pressed-layer marker connected to the kernel's MI HBM path. |
| Services | AIDL lights, thermal helper and HAL startup, DSP readiness, and modem helper startup have explicit product/init wiring. |
| Codec sandbox | The arm64 software codec extension is installed at the path read by PE's mediaswcodec service. |
| DRM | Widevine HAL and data directory retained; the irrelevant Oreo-to-Pie migration hook is removed by a reproducible extraction fixup. |
| Firmware | The ROM keeps the installed official Crux bootloader and radio firmware. Runtime DSP/modem loaders use that firmware; OTA packaging does not replace it. |

Partition size declarations inherited by this source baseline are image build
limits, not a substitute for reading the real device's GPT. The old donor
userdata size is removed because Crux ships with multiple storage capacities.
Charging limits, battery calibration curves and panel timings are not derived
from marketing specifications.

## Input provenance

- Xiaomi's [official specifications](https://www.mi.com/mi9pro/specs), including
  the [page's data script](https://cdn.cnbj1.fds.api.mi-img.com/mi.com-assets/shop/pro/js/product/mi9pro/specs.0f7233a0.js),
  establish 855 Plus, 8/12 GiB memory, 4000 mAh typical battery and NSA-only 5G.
- Crux-specific vendor inputs use
  [`AndroidBlobs/vendor_xiaomi_crux@92cfc58837275219946d74430f8f23c95de3c5b1`](https://github.com/AndroidBlobs/vendor_xiaomi_crux/tree/92cfc58837275219946d74430f8f23c95de3c5b1),
  labelled `V10.4.6.0.PFXCNXM` / Android 9. These are public stock-derived bytes,
  not a newly verified Xiaomi package. Their hashes and exact replacements are
  documented in the companion vendor tree's `PROVENANCE.md`.
- [Stock-derived Android 11 recovery metadata](https://github.com/twrpdtgen/android_device_xiaomi_crux/tree/2c0e48a178bc145e8450eed9b5a12a5dfc5371eb)
  corroborates the boot header version and named static partitions. Its TWRP
  flags, permissive mode and anti-rollback overrides are not imported.
- PE QCOM policy
  [`device_qcom_sepolicy_vndr-legacy-um@a31f4acce72fe49bef7641bb33a389093df5655e`](https://github.com/PixelExperience/device_qcom_sepolicy_vndr-legacy-um/tree/a31f4acce72fe49bef7641bb33a389093df5655e)
  supplies the external modem loader and modem-data permissions used by the
  selected msmnile policy. The final merged policy remains a full-build check.

## Repeatable offline checks

From the Android source root containing the three repositories:

```sh
python3 -B device/xiaomi/crux/update-sha1sums.py --check
python3 -B -m unittest discover -s device/xiaomi/crux/tests -v
python3 -B kernel/xiaomi/crux/tools/testing/selftests/kconfig/crux_config_test.py
python3 -B kernel/xiaomi/crux/tools/testing/selftests/cpufreq/crux_input_boost_test.py
python3 -B kernel/xiaomi/crux/tools/testing/selftests/drivers/gpu/crux_fod_test.py
```

The checks use the actual product/board makefiles, the kernel's Kconfig and
DT tooling, real ZIP archives, and compiled production functions where practical.
Android services, hardware and some kernel objects use boundary test doubles;
passing these checks does not prove Android linking or device behavior.

## Full-build and device acceptance

Use the instructions in `README.md` with a complete PE Android 13 checkout on
Linux. Retain the exact revision manifest, any local patches, target-files ZIP
and final OTA ZIP for each test build.

Before a device test, establish the complete build's VINTF and SELinux results,
ELF dependencies, generated boot/DTBO contents and OTA partition write list.
With a recovered test device, collect `logcat -b all`, kernel/pstore logs and
tombstones together with the build records.

| Area | Required device evidence |
| --- | --- |
| Boot / storage | Correct GPT and image-size limits, recovery boot, first-stage mounts, userdata formatting/encryption, repeat cold boots. |
| Radio | `mdm_helper`/ESOC operation, firmware image transfer, modem registration, SIM switching, LTE and NSA data, calls and IMS. Framework 5G flags alone do not establish service. |
| Sensors / power | Sensor enumeration, proximity/light behavior, suspend/wake, double tap, thermal reporting and charging under load. |
| Fingerprint / display | Enrolment and authentication, illumination on/off, cancellation, AOD and DC-dimming interactions, display resume. |
| Camera / audio / NFC | All sensors and modes, recording/playback/calls, calibration behavior, NFC controller firmware and secure-element routing. These groups still include donor inputs. |
| DRM / connectivity | Widevine operation, Wi-Fi/Bluetooth/GNSS and hardware-specific failures recorded with logs. |

Hardware-specific replacements should be taken from a matched Crux source group
with provenance and dependency checks. Keep the functioning Android 13 shim and
interface requirements when replacing a donor group; a matching filename alone
is insufficient evidence of compatibility.
