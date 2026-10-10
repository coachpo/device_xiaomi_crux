#!/usr/bin/env python3
"""Install the six signed Crux images through an unlocked stock bootloader."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import time


FLASH_ORDER = ("recovery", "system", "vendor", "dtbo", "vbmeta", "boot")
SPARSE_MAGIC = 0xED26FF3A


class FlashError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise FlashError(message)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def expanded_image_size(path: Path) -> int:
    size = path.stat().st_size
    with path.open("rb") as stream:
        header = stream.read(28)
    if len(header) < 4 or struct.unpack("<I", header[:4])[0] != SPARSE_MAGIC:
        return size
    require(len(header) == 28, "Truncated sparse-image header: " + str(path))
    _, major, _, file_header, chunk_header, block_size, blocks, _, _ = struct.unpack(
        "<IHHHHIIII", header)
    require(major == 1 and file_header >= 28 and chunk_header >= 12
            and block_size > 0 and blocks > 0 and file_header <= size,
            "Unsupported or invalid sparse-image header: " + str(path))
    return block_size * blocks


def validate_bundle(directory):
    manifest_path = directory / "manifest.json"
    with manifest_path.open(encoding="utf-8") as stream:
        manifest = json.load(stream)
    require(isinstance(manifest, dict) and manifest.get("schema_version") == 1,
            "Expected manifest schema_version 1")
    require(manifest.get("product") == "crux", "Bundle product must be crux")
    fingerprint, version = manifest.get("fingerprint"), manifest.get("version")
    require(isinstance(fingerprint, str) and isinstance(version, str) and version,
            "Manifest requires fingerprint and version strings")
    pieces = fingerprint.rsplit("/", 2)
    require(len(pieces) == 3 and pieces[2] == "release-keys"
            and pieces[1].split(":", 1)[0] == version,
            "Manifest fingerprint and release version disagree")
    images = manifest.get("images")
    require(isinstance(images, dict) and set(images) == set(FLASH_ORDER),
            "Manifest must contain exactly boot/recovery/system/vendor/dtbo/vbmeta")
    validated = {}
    for partition in FLASH_ORDER:
        row = images[partition]
        require(isinstance(row, dict), "Invalid image record: " + partition)
        expected_file = "images/" + partition + ".img"
        require(row.get("file") == expected_file,
                "Unexpected image path for " + partition)
        path = directory / expected_file
        require(path.resolve().parent == (directory / "images").resolve()
                and path.is_file(), "Missing or misplaced image: " + expected_file)
        for field in ("bytes", "expanded_bytes"):
            require(type(row.get(field)) is int and row[field] > 0,
                    "Invalid " + field + " for " + partition)
        require(path.stat().st_size == row["bytes"], "Image size mismatch: " + partition)
        require(expanded_image_size(path) == row["expanded_bytes"],
                "Expanded image size mismatch: " + partition)
        require(isinstance(row.get("sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]),
                "Invalid image digest: " + partition)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        require(digest.hexdigest() == row["sha256"], "Image SHA256 mismatch: " + partition)
        validated[partition] = {**row, "path": str(path.resolve())}
    return manifest, validated


def variable_value(output, variable):
    pattern = r"(?m)^\s*(?:\(bootloader\)\s*)?" + re.escape(variable) + r":\s*([^\r\n]*)"
    values = re.findall(pattern, output)
    require(len(set(value.strip() for value in values)) <= 1,
            "Conflicting fastboot variable values: " + variable)
    return values[-1].strip() if values else None


def boolean_value(value):
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in ("yes", "true", "1"):
        return True
    if normalized in ("no", "false", "0"):
        return False
    raise FlashError("Unrecognized bootloader boolean: " + value)


class Session:
    def __init__(self, args, log_path):
        self.args = args
        self.log_path = log_path
        self.report = {"schema_version": 1, "started_utc": utc_now(), "status": "incomplete",
                       "serial": args.serial, "bundle_dir": str(args.bundle_dir.resolve()),
                       "dry_run": args.dry_run, "no_reboot": args.no_reboot,
                       "commands": [], "evidence": {}}

    def save(self):
        self.log_path.write_text(json.dumps(self.report, indent=2, sort_keys=True) + "\n",
                                 encoding="utf-8")

    def argv(self, *arguments):
        return [self.args.fastboot, "-s", self.args.serial, *arguments]

    def command(self, *arguments, optional=False):
        command = self.argv(*arguments)
        record = {"argv": command, "started_utc": utc_now()}
        self.report["commands"].append(record)
        self.save()
        try:
            process = subprocess.run(command, capture_output=True, text=True,
                                     encoding="utf-8", errors="replace")
        except OSError as error:
            record.update(finished_utc=utc_now(), launch_error=str(error))
            self.save()
            raise FlashError("Cannot launch fastboot: " + str(error)) from error
        record.update(finished_utc=utc_now(), exit_code=process.returncode,
                      stdout=process.stdout, stderr=process.stderr)
        # Some platform-tools commands print a remote FAILED response but exit 0.
        remote_failed = bool(re.search(r"\bFAILED(?:\s*\(|\s*$)|^\s*fastboot:\s*error:",
                                      process.stdout + "\n" + process.stderr, re.MULTILINE))
        record["remote_failed"] = remote_failed
        self.save()
        require(optional or (process.returncode == 0 and not remote_failed),
                "fastboot " + " ".join(arguments) + " failed (exit "
                + str(process.returncode) + "); see " + str(self.log_path))
        return process.returncode or (1 if remote_failed else 0), process.stdout + "\n" + process.stderr

    def getvar(self, name, optional=False):
        exit_code, output = self.command("getvar", name, optional=optional)
        return variable_value(output, name) if exit_code == 0 else None

    def preflight(self, images):
        _, devices = self.command("devices")
        require(any(line.split()[:2] == [self.args.serial, "fastboot"]
                    for line in devices.splitlines()),
                "Requested serial is not present in fastboot devices: " + self.args.serial)
        product = self.getvar("product")
        self.report["evidence"]["product"] = product
        require(product == "crux", "Connected product is not crux: " + str(product))

        unlocked = boolean_value(self.getvar("unlocked", optional=True))
        oem_exit, oem_output = self.command("oem", "device-info", optional=True)
        oem_unlocked = None
        if oem_exit == 0:
            matches = re.findall(r"(?m)^\s*(?:\(bootloader\)\s*)?Device unlocked:\s*([^\r\n]*)",
                                 oem_output)
            states = {boolean_value(value) for value in matches}
            require(len(states) <= 1, "Contradictory OEM unlocked evidence")
            oem_unlocked = next(iter(states)) if states else None
        userspace = boolean_value(self.getvar("is-userspace", optional=True))
        self.report["evidence"].update(unlocked_getvar=unlocked,
                                       unlocked_oem=oem_unlocked, is_userspace=userspace)
        require(unlocked is None or oem_unlocked is None or unlocked == oem_unlocked,
                "Contradictory bootloader unlocked evidence; no images flashed")
        require(unlocked is not False and oem_unlocked is not False,
                "Bootloader is locked; no images flashed")
        require(unlocked is True or oem_unlocked is True,
                "No supported positive bootloader unlocked evidence; no images flashed")
        require(userspace is not True, "Userspace fastboot is unsupported; use stock ABL fastboot")
        require(userspace is False or oem_unlocked is True,
                "Cannot establish stock bootloader fastboot; no images flashed")

        capacities = {}
        self.report["evidence"]["partition_sizes"] = capacities
        for partition in FLASH_ORDER:
            value = self.getvar("partition-size:" + partition)
            require(value is not None and re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)", value),
                    "Invalid partition capacity for " + partition + ": " + str(value))
            capacities[partition] = int(value, 16 if value.lower().startswith("0x") else 10)
        for partition in FLASH_ORDER:
            require(capacities[partition] >= images[partition]["expanded_bytes"],
                    "Partition " + partition + " is too small for the expanded image")
        self.save()

    def execute(self):
        manifest, images = validate_bundle(self.args.bundle_dir.resolve())
        self.report["evidence"].update(fingerprint=manifest["fingerprint"],
                                       version=manifest["version"], images=images)
        self.save()
        self.preflight(images)
        planned = [self.argv("flash", partition, images[partition]["path"])
                   for partition in FLASH_ORDER]
        if not self.args.no_reboot:
            planned.append(self.argv("reboot"))
        self.report["planned_commands"] = planned
        self.save()
        if self.args.dry_run:
            self.report["status"] = "dry_run_complete"
            return
        for partition in FLASH_ORDER:
            print("Flashing " + partition, flush=True)
            self.command("flash", partition, images[partition]["path"])
        if not self.args.no_reboot:
            self.command("reboot")
        self.report["status"] = "flashed"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True, help="serial of the intended Crux phone")
    parser.add_argument("--fastboot", default="fastboot", help="platform-tools fastboot executable")
    parser.add_argument("--bundle-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--log", type=Path, help="new JSON evidence log; existing files are not overwritten")
    parser.add_argument("--dry-run", action="store_true", help="check images and read-only loader preflight without flash/reboot")
    parser.add_argument("--no-reboot", action="store_true", help="remain in fastboot after successful flashing")
    args = parser.parse_args(argv)
    log_path = (args.log or args.bundle_dir / ("flash-log-" + str(time.time_ns()) + ".json")).resolve()
    session = Session(args, log_path)
    try:
        require(bool(args.serial.strip()) and not args.serial.startswith("-"), "Invalid serial")
        require(not log_path.exists(), "Evidence log already exists: " + str(log_path))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        session.save()
    except (FlashError, OSError) as error:
        print("error: " + str(error), file=sys.stderr)
        return 1
    exit_code = 0
    try:
        session.execute()
    except (FlashError, OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        session.report.update(status="failed", error=str(error))
        print("error: " + str(error), file=sys.stderr)
        exit_code = 1
    except KeyboardInterrupt:
        session.report.update(status="interrupted", error="Interrupted; inspect the last command before retrying")
        print("Interrupted; no further commands will run.", file=sys.stderr)
        exit_code = 130
    finally:
        session.report["finished_utc"] = utc_now()
        try:
            session.save()
        except OSError as error:
            print("error: Cannot save final evidence log: " + str(error), file=sys.stderr)
            exit_code = 1
    print("Evidence: " + str(log_path))
    if exit_code == 0:
        print("Dry run passed; no images flashed." if args.dry_run else
              "Six images flashed; check ROM boot and retained data.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
