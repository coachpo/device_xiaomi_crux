#!/usr/bin/env python3
"""Package the final signed public Crux images for a data-preserving fastboot install."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

from package_release import IMAGES, PackageError, dictionary, require, text_entry, version_coherence
from flash_crux import FlashError, expanded_image_size


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def release_identity(archive):
    names = archive.namelist()
    require(len(names) == len(set(names)), "Duplicate target-files entries")
    image_names = {"IMAGES/" + name + ".img" for name in IMAGES}
    require({n for n in names if n.startswith("IMAGES/") and n.endswith(".img")} == image_names,
            "Expected exactly the six public Android images; no data, firmware or GPT images")
    require(not any(not entry.is_dir() and entry.filename.startswith(("RADIO/", "INSTALL/firmware-update/"))
                    for entry in archive.infolist()),
            "Stock firmware is not part of the Crux fastboot bundle")
    misc = dictionary(text_entry(archive, "META/misc_info.txt"))
    props = dictionary(text_entry(archive, "SYSTEM/build.prop"))
    require(props.get("ro.build.flavor") == "aosp_crux_release-user"
            and props.get("ro.build.tags") == "release-keys", "Expected final signed public user target-files")
    require(props.get("ro.product.system.device", props.get("ro.product.device")) == "crux",
            "Expected device crux")
    require(misc.get("ab_update") != "true" and misc.get("use_dynamic_partitions") != "true",
            "Expected static non-A/B partitions")
    require(text_entry(archive, "OTA/android-info.txt").strip() == "board=crux",
            "Expected stock Crux fastboot board requirement")
    versions = version_coherence({
        "SYSTEM/build.prop": props,
        "VENDOR/build.prop": dictionary(text_entry(archive, "VENDOR/build.prop")),
        "RECOVERY/RAMDISK/prop.default": dictionary(text_entry(archive, "RECOVERY/RAMDISK/prop.default")),
    }, misc)
    for name in names:
        if name.endswith(("fstab.qcom", "recovery.fstab", "install-recovery.sh")):
            body = "\n".join(line.split("#", 1)[0] for line in archive.read(name).decode("utf-8").splitlines())
            require("crux_uboot" not in body and not any("by-name/" + p in body for p in
                    ("pesystem", "pevendor", "peuserdata", "pemetadata")),
                    "Development storage route in " + name)
    return versions["expected"]


def instructions(version, fingerprint):
    return f"""# Crux PE13 {version} fastboot candidate

For Xiaomi Mi 9 Pro 5G (`crux`), stock ABL and an unlocked bootloader. The firmware
reference is MIUI V13.0.1.0.RFXCNXM / Android 11; firmware already on the phone is
retained. These images are the same signed release used by the
matching Recovery ZIP. Keep the bootloader unlocked.

## Install or rescue without formatting

Install Android SDK Platform-Tools and Python 3. Connect the phone in bootloader
fastboot mode. From a working Android system, `adb reboot bootloader` enters it;
otherwise use the phone's bootloader key combination. Obtain its serial using
`fastboot devices`.

macOS/Linux:

```sh
./flash.sh --serial YOUR_SERIAL
```

Windows (Python available as `python`):

```bat
flash.cmd --serial YOUR_SERIAL
```

Alternatively, on any supported Python host:

```sh
python3 flash_crux.py --serial YOUR_SERIAL
```

Use `--fastboot /path/to/fastboot` when it is not on PATH. `--dry-run` performs
read-only checks and records the six planned flashes in its JSON log; it does not write or
reboot. `--no-reboot` leaves the phone in fastboot after a successful install.

The installer verifies the files, product, actual unlocked loader and partition
capacity, then flashes recovery, system, vendor, dtbo, vbmeta and boot. It
preserves userdata, metadata and cache. It stops at any failed flash and leaves
the phone in fastboot; rerun this same complete bundle to reinstall or recover a
system that cannot boot. Rescue requires an accessible stock ABL fastboot.

Each run saves a JSON log. Successful flashing requests a reboot. Check that the
phone starts normally and reports the expected post-boot version:

`{fingerprint}`

## First installation with incompatible existing data

This command does not convert MIUI data or reset encryption. Use the matching PE
Recovery's Format Data/WipeData procedure to reset userdata, metadata and cache
when a first installation requires it. That operation deletes user data. The
installer requires the public v2 data layout or data freshly reset by the matching
PE Recovery. Generic bootloader-to-Recovery menu entry is not provided by this
installer.
Do not restore metadata from an older data/encryption layout.

The bundle changes only the six Android partitions. Firmware and GPT are retained;
it contains no misc image, device-specific BCB, userdata image or signing keys.
"""


def package(args):
    source = args.signed_target_files.resolve()
    output = args.output_dir.resolve()
    require(not output.exists(), "Choose a new output directory")
    archive_path = output.parent / (output.name + ".zip")
    report_path = output.parent / (output.name + ".package-report.json")
    require(not archive_path.exists() and not report_path.exists(), "Bundle archive/report already exists")
    with zipfile.ZipFile(source) as archive:
        identity = release_identity(archive)
        output.mkdir(parents=True)
        (output / "images").mkdir()
        manifest = {"schema_version": 1, "product": "crux", "fingerprint": identity["fingerprint"],
                    "version": identity["version.incremental"], "images": {}}
        for partition in sorted(IMAGES):
            target = output / "images" / (partition + ".img")
            image_digest = hashlib.sha256()
            with archive.open("IMAGES/" + partition + ".img") as src, target.open("wb") as dst:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    dst.write(chunk)
                    image_digest.update(chunk)
            require(target.stat().st_size > 0, "Empty " + partition + " image")
            manifest["images"][partition] = {"file": "images/" + target.name,
                "bytes": target.stat().st_size, "expanded_bytes": expanded_image_size(target),
                "sha256": image_digest.hexdigest()}
    manifest["signed_target_files_sha256"] = digest(source)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    shutil.copyfile(Path(__file__).with_name("flash_crux.py"), output / "flash_crux.py")
    (output / "flash.sh").write_text('#!/bin/sh\nexec python3 "$(dirname "$0")/flash_crux.py" "$@"\n')
    (output / "flash.sh").chmod(0o755)
    (output / "flash.cmd").write_bytes(b'@echo off\r\npython "%~dp0flash_crux.py" %*\r\nexit /b %errorlevel%\r\n')
    (output / "README.md").write_text(instructions(manifest["version"], manifest["fingerprint"]))
    (output / "SHA256SUMS").write_text("".join(
        manifest["images"][p]["sha256"] + "  " + manifest["images"][p]["file"] + "\n" for p in sorted(IMAGES)))
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                archive.write(path, arcname=output.name + "/" + path.relative_to(output).as_posix())
    report = {"status": "complete", "signed_target_files": str(source),
              "signed_target_files_sha256": manifest["signed_target_files_sha256"],
              "bundle": str(output), "archive": str(archive_path),
              "archive_bytes": archive_path.stat().st_size, "archive_sha256": digest(archive_path),
              "image_identity": manifest["images"], "fingerprint": manifest["fingerprint"],
              "signing": "Existing signed image bytes copied unchanged; no signing or key access",
              "runtime_acceptance": False}
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print("Fastboot bundle written to " + str(archive_path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signed-target-files", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        package(args)
    except (PackageError, FlashError, OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        parser.exit(1, "error: " + str(error) + "\n")


if __name__ == "__main__":
    main()
