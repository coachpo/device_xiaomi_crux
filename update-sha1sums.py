#!/usr/bin/env python3
#
# Copyright (C) 2016 The CyanogenMod Project
# Copyright (C) 2017-2020 The LineageOS Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import argparse
from hashlib import sha1
from pathlib import Path
import re
import sys


DEVICE_DIR = Path(__file__).resolve().parent
FILE_LIST = DEVICE_DIR / "proprietary-files.txt"
BLOBS_DIR = DEVICE_DIR.parents[2] / "vendor/xiaomi/crux/proprietary"


def process(lines, check=False):
    updated = list(lines)
    seen = set()
    pinned = 0
    donor_section = False
    errors = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if line.startswith("#"):
            donor_section = " - from" in line
            continue
        spec, *hashes = line.split("|")
        destination = spec.lstrip("-").split(";", 1)[0].split(":")[-1]
        if destination in seen:
            errors.append(f"Duplicate destination: {destination}")
        seen.add(destination)
        if len(hashes) > 2 or any(not re.fullmatch(r"[0-9a-fA-F]{40}", h) for h in hashes):
            errors.append(f"Invalid SHA-1 pin: {destination}")
            continue
        blob = BLOBS_DIR / destination
        if not blob.is_file():
            errors.append(f"Missing blob: {destination}")
            continue
        if hashes:
            pinned += 1
            actual = sha1(blob.read_bytes()).hexdigest()
            if actual not in [h.lower() for h in hashes]:
                errors.append(f"SHA-1 mismatch: {destination}: {actual}")
        elif donor_section and not check:
            updated[index] = f"{spec}|{sha1(blob.read_bytes()).hexdigest()}"

    if check:
        actual_files = {p.relative_to(BLOBS_DIR).as_posix()
                        for p in BLOBS_DIR.rglob("*") if p.is_file()}
        errors.extend(f"Unlisted blob: {p}" for p in sorted(actual_files - seen))
    if errors:
        raise ValueError("\n".join(errors))
    return updated, len(seen), pinned


def main():
    parser = argparse.ArgumentParser(
        description="Validate pins or add missing donor pins without replacing source hashes.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("-c", "--cleanup", action="store_true", help="remove all SHA-1 pins")
    action.add_argument("--check", action="store_true", help="check blob inventory and pins without writing")
    args = parser.parse_args()
    lines = FILE_LIST.read_text(encoding="utf-8").splitlines()
    if args.cleanup:
        updated = [line if line.startswith("#") else line.split("|", 1)[0] for line in lines]
    else:
        try:
            updated, count, pinned = process(lines, check=args.check)
        except ValueError as error:
            print(error, file=sys.stderr)
            print("Verify the source before changing pins. Use extract-files.sh --kang "
                  "to record original and post-fixup hashes for an intentional blob update.",
                  file=sys.stderr)
            return 1
        if args.check:
            print(f"Verified {count} blobs and {pinned} SHA-1 pins.")
            return 0
    # Preserve existing source/fixup hash pairs; a vendor tree only contains the
    # post-fixup bytes and cannot reconstruct a missing original source hash.
    if updated != lines:
        FILE_LIST.write_text("\n".join(updated) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
