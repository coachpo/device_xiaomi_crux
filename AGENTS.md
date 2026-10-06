# Crux PE13 device guidance

## Source and build ownership

- This checkout owns the PE13 device configuration. The PE13 tree is
  `/home/qingli/crux-pe13-offline-2026-09-25/pe13` in OrbStack `cruxbuild`.
  `BoardConfig.mk` selects `kernel/xiaomi/crux-pe-cepheus`, whose maintained
  checkout is `/home/qingli/crux-kernel-cepheus` on branch `thirteen`. The
  maintained remote is `coachpo/kernel_xiaomi_crux`, branch `thirteen`. Official
  Cepheus upstream is the immutable reference `f4048f154b512cf9a8b38579956834514af9626e`.
  Preserve accepted Image/symbol pairing and later functional candidate changes.
- U-Boot source and cache/FIT installation belong to the Mac workspace
  `/Users/qingli/Documents/project/crux/u-boot-port`, not this device checkout.
  All ROM and Recovery entries load through the maintained U-Boot menu. Every
  debug kernel handoff must arm and check the watchdog; use the workspace
  [watchdog guide](/Users/qingli/Documents/project/crux/u-boot-port/notes/KERNEL-DEBUG-WATCHDOG.md).

## PE and MIUI storage separation

- `rootdir/etc/fstab.crux_uboot` is the canonical PE fstab. Its Android volumes
  are `pesystem`, `pevendor`, `pemetadata`, and `peuserdata`. Preserve the same
  source for ramdisk/vendor `fstab.qcom` and `fstab.crux_uboot` aliases,
  Recovery `system/etc/recovery.fstab`, and Recovery root `/fstab.crux_uboot`.
  The Recovery root alias identifies PE when its boot suffix is missing.
- `BoardConfig.mk` supplies `androidboot.fstab_suffix=crux_uboot` for both boot
  and Recovery images. `init.target.rc` uses default `mount_all --early` and
  `mount_all --late`; do not restore a charger mount of the MIUI system.
- Native protection is implemented in PE13 `system/core/init/crux_storage_guard.*`
  and invoked by `first_stage_init.cpp` before the first persistent mount.
  The matching PE profile in `system/core/fs_mgr/fs_mgr_fstab.cpp` excludes
  the original MIUI DT fstab. Keep these paths aligned with the canonical PE
  fstab; a vendor-only routing fix does not cover first-stage init.
- Do not add shared `misc` or `cache` to PE fstab or mount/format them through
  PE. Shared BCB and generic OTA boot/recovery writes do not implement the
  U-Boot PE routing. Installation updates PE system/vendor and the designated
  PE FIT slots; it must preserve MIUI and TWRP payloads and other shared regions.
- The owner stated on 2026-10-05 that test device `8bd37df4` has no personal
  data and an existing complete MIUI ROM backup. Keep the preserved images;
  main-data recovery is not a prerequisite for PE work. This statement does
  not authorize changing MIUI storage or deleting the backups.

## Development ADB

- Crux `userdebug` and `eng` images enable ROM and Recovery ADB by default,
  with `ro.adb.secure=0` and `ro.adb.secure.recovery=0`. `user` builds retain
  authentication and their ordinary USB default. Do not apply this profile to
  other devices or release builds.
- The authentication property branch is in PE13 `vendor/aosp/config/common.mk`.
  `device.mk` installs platform `adbd_crux` and the system-domain
  `rootdir/etc/init.crux-development-adb.rc` only for the two development
  variants; `ro.vendor.crux.early_adb` selects the matching gadget path.
  `packages/modules/adb/Android.bp` reuses the normal daemon binary defaults.
- Early ADB begins in second-stage `on init`, after bootstrap runtime and
  SELinux are available but before storage encryption and normal APEX
  activation. It does not diagnose failures before second-stage init.
  Keep the first descriptors wait and UDC binding in the same init action;
  initial property-trigger actions are enabled too late for this purpose.
- `crux_adbd` uses the normal adbd socket and SELinux domain. Early debug root
  avoids restarting merely to collect privileged logs while init is stalled;
  `ro.secure` stays 1. `early-boot` unbinds and stops it, waits until Reap has
  removed the old socket, then restores ordinary root and USB properties for
  normal APEX adbd. The user profile retains the original vendor `on boot`
  gadget setup; the development profile initializes that gadget only once.
- Do not issue early `adb unroot`, `tcpip`, or `usb` during an init stall:
  daemon restart closes FunctionFS and needs another UDC binding action,
  which cannot run while init waits. Early cold-start ADB and the normal
  daemon handoff require separate hardware verification.

## Verification entry points

Run from the PE13 tree, with no other Soong/Ninja build active:

```sh
unset CRUX_UBOOT_DUALBOOT CC_WRAPPER
source build/envsetup.sh
lunch aosp_crux-userdebug
export USE_CCACHE=1 CCACHE_EXEC=/usr/bin/ccache
m bootimage recoveryimage systemimage vendorimage -j14
```

The existing Rosetta host and dexpreopt-off workarounds are required for this
local build. Check changed init scripts with the built `host_init_verifier`.
Before device testing, inspect the actual boot/Recovery CPIOs and system/vendor
images for the PE fstab mappings, Recovery root alias, compiled native guard,
bootloader failure target, and the development ADB properties. A successful
build does not prove watchdog recovery, ROM UI boot, ADB, or runtime isolation.
The accepted source/artifact record is workspace `docs/pe13-development-baseline-2026-10-05.md`
and `out/crux-pe13-baseline-2026-10-05/artifact-registry.json`. Current functional
candidates are recorded separately in `docs/pe13-functional-validation-2026-10-06.md`.
Keep device startup and handoff results separate from source/build checks.
