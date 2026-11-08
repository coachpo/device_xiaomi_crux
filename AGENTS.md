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
  `u-boot-port/`, not this device checkout. Development ROM and PE Recovery
  load through the maintained U-Boot menu. Every debug kernel handoff must
  arm and check the watchdog; use the
  [workspace watchdog guide](/mnt/mac/Users/qingli/Documents/project/crux/u-boot-port/notes/KERNEL-DEBUG-WATCHDOG.md).
- For the two build paths and their evidence, see the
  [workspace index](/mnt/mac/Users/qingli/Documents/project/crux/docs/README.md).
  This checkout is the local (B) source; GitHub Actions (A) builds the commits
  pinned in its source manifest, and both package the same signed TWRP ZIP.
  Do not upload local ROM output to GitHub Release. Pair the PE
  Recovery image/FIT with the actual build; the
  [retained components](/mnt/mac/Users/qingli/Documents/project/crux/docs/pe13-retained-components-2026-11-05.md#pe-recovery)
  distinguish host verification, FIT readback and Recovery boot.

## PE and MIUI storage separation

- `rootdir/etc/fstab.crux_uboot` is canonical for `aosp_crux`. Its Android volumes
  are `pesystem`, `pevendor`, `pemetadata`, and `peuserdata`. Preserve the same
  source for ramdisk/vendor `fstab.qcom` and `fstab.crux_uboot` aliases,
  Recovery `system/etc/recovery.fstab`, and Recovery root `/fstab.crux_uboot`.
  The Recovery root alias identifies PE when its boot suffix is missing.
- `BoardConfig.mk` supplies `androidboot.fstab_suffix=crux_uboot` for both boot
  and Recovery. `init.target.rc` uses default `mount_all --early` and
  `mount_all --late`; do not restore a charger mount of the MIUI system.
- Native protection is implemented in PE13 `system/core/init/crux_storage_guard.*`
  and invoked by `first_stage_init.cpp` before the first persistent mount.
  The matching PE profile in `system/core/fs_mgr/fs_mgr_fstab.cpp` excludes
  the original MIUI DT fstab. Keep these paths aligned with the canonical PE
  fstab; a vendor-only routing fix does not cover first-stage init.
- Do not add shared `misc` or `cache` to PE fstab or mount/format them through
  PE. The AOSP BCB and Recovery update paths do not implement the U-Boot PE
  routing. Installation goes through the signed TWRP ZIP: it writes PE
  system/vendor and stages the PE FITs for U-Boot to land, and it must preserve
  MIUI and TWRP payloads and other shared regions.
- Preserve the official MIUI and identified rollback inputs. Original
  userdata recovery is a separate task, not a development prerequisite;
  historical device records do not authorize writes.

## Development ADB

- Development `aosp_crux:userdebug` and `aosp_crux:eng` enable ROM and Recovery
  ADB by default, with `ro.adb.secure=0` and `ro.adb.secure.recovery=0`. Preserve
  the authenticated default in `aosp_crux-user`.
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

Follow the [OrbStack build and install guide](/mnt/mac/Users/qingli/Documents/project/crux/docs/pe13-local-build-notes.md).
Run from the PE13 root with no other Soong/Ninja build active, using a
dedicated Dev `OUT_DIR` and `lunch aosp_crux-userdebug`. Do not reuse retained
acceptance outputs.

The existing Rosetta host and dexpreopt-off workarounds are required for this
local build. Check changed init scripts with the built `host_init_verifier`.
Before device testing, inspect the actual boot/Recovery CPIOs and system/vendor
images for the PE fstab mappings, Recovery root alias, compiled native guard,
bootloader failure target, and the development ADB properties. A successful
build does not prove watchdog recovery, ROM UI boot, ADB, or runtime isolation.
Record actual revisions, local diffs and output identity for each build. The
October 21 local acceptance compiled four images but installed/read back only
new raw system/vendor and booted the retained v2 ROM FIT; it did not boot a
new Recovery. Keep startup and handoff observations separate from build checks.
