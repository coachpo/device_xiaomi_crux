#!/usr/bin/env python3
"""Sign an existing public Crux target-files archive with platform releasetools."""

import argparse
import collections
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import zipfile


IMAGES = {"boot", "recovery", "system", "vendor", "dtbo", "vbmeta"}
AVB_OPTIONS = {"boot", "init_boot", "recovery", "system", "system_other",
               "vendor", "dtbo", "vbmeta", "vbmeta_system", "vbmeta_vendor"}
SPECIAL = {"PRESIGNED", "EXTERNAL"}
SOURCE_ROOT = Path(__file__).resolve().parents[4]


class PackageError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise PackageError(message)


def dictionary(text):
    return dict(line.split("=", 1) for line in text.splitlines()
                if line and not line.startswith("#") and "=" in line)


def text_entry(archive, name):
    require(archive.namelist().count(name) == 1, "Expected exactly one " + name)
    return archive.read(name).decode("utf-8")


def records(archive, name):
    result = collections.defaultdict(list)
    for line in text_entry(archive, name).splitlines():
        if not line.strip():
            continue
        try:
            fields = dict(token.split("=", 1) for token in shlex.split(line))
        except ValueError:
            continue
        if "name" in fields:
            result[fields["name"]].append(fields)
    return result


def consumed_records(rows, consumed_names, signing_fields, metadata_name):
    selected = {}
    for name in sorted(consumed_names):
        require(name in rows, "Missing signing declaration for " + name + " in " + metadata_name)
        declarations = {tuple(row.get(field) for field in signing_fields) for row in rows[name]}
        require(len(declarations) == 1,
                "Conflicting signing declarations for consumed package " + name + " in " + metadata_name)
        selected[name] = rows[name][-1]
    return selected


def host_tool(host, name):
    path = host / "bin" / name
    require(path.is_file() and os.access(path, os.X_OK),
            "Missing executable platform releasetool: " + str(path) + "; build host tools first")
    return str(path)


def apex_apks(archive, name, host):
    deapexer = host_tool(host, "deapexer")
    debugfs = host_tool(host, "debugfs_static")
    commands = []

    def probe(arguments):
        command = [deapexer, "--debugfs_path", debugfs] + arguments
        process = subprocess.run(command, cwd=SOURCE_ROOT, capture_output=True, text=True)
        commands.append({"argv": command, "exit_code": process.returncode})
        require(process.returncode == 0,
                "APEX inspection failed for " + name + ": " + process.stderr.strip())
        return process.stdout

    with tempfile.TemporaryDirectory(prefix="crux-apex-inventory-") as temporary:
        original = Path(temporary) / ("input.capex" if name.endswith(".capex") else "input.apex")
        with archive.open(name) as source, original.open("wb") as destination:
            shutil.copyfileobj(source, destination)
        apex_type = probe(["info", "--print-type", str(original)]).strip()
        require(apex_type in {"COMPRESSED", "UNCOMPRESSED"}, "Unsupported APEX type for " + name)
        if apex_type == "COMPRESSED":
            decompressed = Path(temporary) / "original.apex"
            probe(["decompress", "--input", str(original), "--output", str(decompressed)])
            original = decompressed
        contents = probe(["list", str(original)]).split()
        return {"type": apex_type, "nested_apk_paths": sorted(p for p in contents if p.endswith(".apk")),
                "commands": commands}


def cert_prefix(certificate, private_key):
    if certificate in SPECIAL and not private_key:
        return certificate
    if certificate == private_key == "PRESIGNED":
        return "PRESIGNED"
    require(certificate.endswith(".x509.pem") and private_key.endswith(".pk8")
            and certificate[:-9] == private_key[:-4],
            "Inconsistent certificate/private-key names in target-files")
    return certificate[:-9]


def version_coherence(properties, misc):
    system_path = "SYSTEM/build.prop"
    fields = ("fingerprint", "version.incremental", "date.utc", "date")

    def value(path, key):
        result = properties[path].get(key)
        require(bool(result), "Missing generated ROM version property: " + path + ": " + key)
        return result

    fingerprint = value(system_path, "ro.build.fingerprint")
    incremental = value(system_path, "ro.build.version.incremental")
    pieces = fingerprint.rsplit("/", 2)
    require(len(pieces) == 3 and ":" in pieces[1] and pieces[1].split(":", 1)[0] == incremental,
            "Generated ROM version mismatch: SYSTEM/build.prop: ro.build.fingerprint build number differs from ro.build.version.incremental=" + incremental)
    expected = {field: value(system_path, "ro.build." + field) for field in fields}
    required = {system_path: ("ro.build.", "ro.system.build."),
                "VENDOR/build.prop": ("ro.vendor.build.",),
                "RECOVERY/RAMDISK/prop.default": ("ro.build.", "ro.system.build.", "ro.vendor.build.")}
    partitions = {"system", "vendor", "odm", "system_ext", "product", "bootimage",
                  "vendor_dlkm", "odm_dlkm", "system_dlkm"}
    checked = {}
    for path, prefixes in required.items():
        keys = {prefix + field for prefix in prefixes for field in fields}
        for key in properties[path]:
            match = re.fullmatch(r'ro\.([^.]+)\.build\.(fingerprint|version\.incremental|date\.utc|date)', key)
            if match and match.group(1) in partitions:
                keys.add(key)
        checked[path] = {}
        for key in sorted(keys):
            field = key.split(".build.", 1)[1]
            observed = value(path, key)
            require(observed == expected[field],
                    "Generated ROM version mismatch: " + path + ": " + key + "=" + observed + "; expected " + expected[field])
            checked[path][key] = observed
    avb_fingerprints = {}
    for key, arguments in misc.items():
        if not re.fullmatch(r'avb_.+_add_(?:hash|hashtree)_footer_args', key):
            continue
        for token in shlex.split(arguments):
            token = token[len("--prop="):] if token.startswith("--prop=") else token
            prop, separator, observed = token.partition(":")
            if separator and re.fullmatch(r'com\.android\.build\.[^.]+\.fingerprint', prop):
                require(observed == fingerprint,
                        "Generated ROM version mismatch: META/misc_info.txt: " + key + ": " + prop + "=" + observed + "; expected " + fingerprint)
                avb_fingerprints[key + ":" + prop] = observed
    return {"expected": expected, "checked_properties": checked, "avb_fingerprints": avb_fingerprints}


def inventory(path, host):
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        require(not [n for n, count in collections.Counter(names).items()
                     if count > 1], "Duplicate ZIP entries in target-files")
        for entry in entries:
            if entry.is_dir():
                continue
            name = entry.filename
            require(not name.startswith(("RADIO/", "INSTALL/firmware-update/")),
                    "Crux releases must not bundle stock firmware: " + name)
            if name.startswith("IMAGES/") and name.endswith(".img"):
                require(name == "IMAGES/cache.img" or name[7:-4] in IMAGES,
                        "Unexpected partition image (including userdata/firmware): " + name)
            require(not name.endswith(".pk8"), "Private key present in target-files: " + name)
            require(not name.startswith("BOOTABLE_IMAGES/"),
                    "BOOTABLE_IMAGES bypasses platform image re-signing: " + name)
            require(name not in ("PREBUILT_IMAGES/boot.img", "PREBUILT_IMAGES/recovery.img"),
                    "Prebuilt Boot/Recovery bypasses rebuilt routing and OTA certificates: " + name)
        if "META/pack_radioimages.txt" in names:
            require(not archive.read("META/pack_radioimages.txt").strip(),
                    "Target-files requests bundled radio/firmware images")
        for image in sorted(IMAGES):
            name = "IMAGES/" + image + ".img"
            require(names.count(name) == 1 and archive.getinfo(name).file_size > 0,
                    "Missing or empty required image: " + name)
        for partition in ("BOOT", "RECOVERY"):
            require(partition + "/kernel" in names and any(
                    name.startswith(partition + "/RAMDISK/") for name in names),
                    "Missing rebuildable source tree for " + partition)
        misc = dictionary(text_entry(archive, "META/misc_info.txt"))
        props = dictionary(text_entry(archive, "SYSTEM/build.prop"))
        require(props.get("ro.build.flavor") == "aosp_crux_release-user",
                "Expected public aosp_crux_release-user target-files")
        require(props.get("ro.product.system.device", props.get("ro.product.device")) == "crux",
                "Target-files does not identify device crux")
        require(misc.get("ab_update") != "true"
                and misc.get("use_dynamic_partitions") != "true",
                "Only static non-A/B target-files are supported")
        generated_version = version_coherence({"SYSTEM/build.prop": props,
            "VENDOR/build.prop": dictionary(text_entry(archive, "VENDOR/build.prop")),
            "RECOVERY/RAMDISK/prop.default": dictionary(text_entry(archive, "RECOVERY/RAMDISK/prop.default"))}, misc)
        apk_rows = records(archive, "META/apkcerts.txt")
        basenames = {Path(name).name for name in names}
        compression = {row["compressed"] for name, rows in apk_rows.items() for row in rows
                       if row.get("compressed") and name + "." + row["compressed"] in basenames}
        require(len(compression) <= 1, "Multiple installed APK compression extensions")
        compressed_extension = "." + next(iter(compression)) if compression else None
        flat_apks = {}
        for name in names:
            if name.endswith(".apk"):
                flat_apks[name] = Path(name).name
            elif compressed_extension and name.endswith(".apk" + compressed_extension):
                flat_apks[name] = Path(name).name[:-len(compressed_extension)]
        apex_paths = {name: Path(name).name.replace(".capex", ".apex") for name in names
                      if name.endswith((".apex", ".capex"))}
        apex_rows = consumed_records(records(archive, "META/apexkeys.txt"), set(apex_paths.values()),
                                     ("public_key", "private_key", "container_certificate",
                                      "container_private_key", "sign_tool"), "META/apexkeys.txt")
        apex = {}
        for name, row in apex_rows.items():
            apex[name] = {
                "container_key": cert_prefix(row["container_certificate"],
                                             row["container_private_key"]),
                "payload_key": row["private_key"],
                "sign_tool": row.get("sign_tool"),
            }
        inspections = {}
        apk_names = set(flat_apks.values())
        for path, name in sorted(apex_paths.items()):
            if apex[name]["container_key"] == "PRESIGNED":
                inspections[path] = {"nested_apk_paths": [], "reason": "PRESIGNED outer APEX is copied without nested re-signing"}
                continue
            inspections[path] = apex_apks(archive, path, host)
            apk_names.update(Path(p).name for p in inspections[path]["nested_apk_paths"])
        apks = {name: cert_prefix(row["certificate"], row["private_key"])
                for name, row in consumed_records(apk_rows, apk_names,
                    ("certificate", "private_key"), "META/apkcerts.txt").items()}
        ota_sources = []
        for name in text_entry(archive, "META/otakeys.txt").split():
            require(name.endswith(".x509.pem"), "Invalid certificate in META/otakeys.txt")
            ota_sources.append(name[:-9])
        if not ota_sources:
            ota_sources.append(misc.get("default_system_dev_certificate",
                                        "build/make/target/product/security/testkey"))
        extra_ota = misc.get("extra_ota_keys", "").split()
        extra_recovery = misc.get("extra_recovery_keys", "").split()
        required_keys = sorted((set(apks.values()) - {"PRESIGNED"})
                               | ({row["container_key"] for row in apex.values()} - SPECIAL)
                               | set(ota_sources + extra_ota + extra_recovery))
        avb = {}
        if misc.get("avb_enable") == "true":
            for name, key in misc.items():
                match = re.fullmatch(r"avb_(.+)_key_path", name)
                if match and key:
                    partition = match.group(1)
                    avb[partition] = {"key": key,
                                      "algorithm": misc.get("avb_" + partition + "_algorithm")}
        fstab = text_entry(archive, "RECOVERY/RAMDISK/system/etc/recovery.fstab")
        active_fstab = "\n".join(line.split("#", 1)[0].strip() for line in fstab.splitlines())
        require("crux_uboot" not in active_fstab and not re.search(
            r'/by-name/pe(?:system|vendor|userdata|metadata)\s', active_fstab),
                "Development PE partition routing present in Recovery fstab")
        for partition in (IMAGES - {"dtbo"}) | {"misc", "cache", "userdata", "metadata"}:
            require(re.search(r'/by-name/' + partition + r'\s', active_fstab),
                    "Recovery fstab lacks stock partition " + partition)
        return {"misc": misc, "build_properties": props, "generated_version": generated_version, "apk_keys": apks,
                "apex_keys": apex, "required_key_map": required_keys,
                "ota_sources": ota_sources, "extra_ota_sources": extra_ota,
                "extra_recovery_sources": extra_recovery,
                "avb_keys": avb, "images": sorted(IMAGES),
                "recovery_fstab": fstab, "flat_apk_paths": flat_apks,
                "apex_paths": apex_paths, "apex_inspections": inspections,
                "source_key_probe_notice": "Original metadata remains unchanged; standard signer may probe unused source test keys. New release keys are required only for this shipping inventory."}


def template(info):
    return {
        "schema_version": 1,
        "key_map": {key: None for key in info["required_key_map"]},
        "ota_package_key": None,
        "apex": {
            name: {"container_key": "PRESIGNED" if row["container_key"] == "PRESIGNED" else None,
                   "payload_key": "PRESIGNED" if row["container_key"] == "PRESIGNED" else None}
            for name, row in info["apex_keys"].items()},
        "avb": {name: {"key": None, "algorithm": row["algorithm"]}
                for name, row in info["avb_keys"].items()},
    }


def beneath(path, directory):
    return path == directory or directory in path.parents


def key_file(key_dir, value, suffix=""):
    require(isinstance(value, str) and value and not Path(value).is_absolute(),
            "Signing configuration needs a key path relative to --key-dir")
    path = (key_dir / (value + suffix)).resolve()
    require(beneath(path, key_dir), "Key path escapes --key-dir")
    require(path.is_file() and path.stat().st_size > 0, "Missing or empty key file: " + str(path))
    return str(path)


def certificate(key_dir, prefix):
    key_file(key_dir, prefix, ".x509.pem")
    key_file(key_dir, prefix, ".pk8")
    return str((key_dir / prefix).resolve())


def signing_arguments(info, config, key_dir):
    require(set(config) == {"schema_version", "key_map", "ota_package_key", "apex", "avb"},
            "Unexpected or missing signing-configuration fields; use inspect's template")
    require(config["schema_version"] == 1, "Unsupported signing-configuration version")
    require(set(config["key_map"]) == set(info["required_key_map"]),
            "key_map must cover exactly the target-files APK/OTA key sources")
    require(set(config["apex"]) == set(info["apex_keys"]),
            "apex map must cover exactly the present APEX containers; regenerate and review the template")
    require(set(info["avb_keys"]) <= set(config["avb"]), "Missing required AVB key mappings")
    arguments = ["--replace_ota_keys"]
    resolved_map = {}
    for source, destination in sorted(config["key_map"].items()):
        if source not in SPECIAL:
            require((SOURCE_ROOT / (source + ".x509.pem")).is_file(),
                    "Missing source certificate needed for platform SELinux remapping: " + source)
        resolved_map[source] = certificate(key_dir, destination)
        arguments += ["--key_mapping", source + "=" + resolved_map[source]]
    ota_key = certificate(key_dir, config["ota_package_key"])
    require(ota_key in {resolved_map[key] for key in info["ota_sources"]},
            "ota_package_key must be trusted by the mapped OTA verification certificates")
    for name, row in sorted(config["apex"].items()):
        require(set(row) == {"container_key", "payload_key"}, "Invalid APEX signing configuration: " + name)
        source_container = info["apex_keys"][name]["container_key"]
        if source_container == "PRESIGNED":
            require(row["container_key"] == row["payload_key"] == "PRESIGNED",
                    "Input PRESIGNED APEX must retain PRESIGNED container and payload: " + name)
        if row["container_key"] == row["payload_key"] == "PRESIGNED":
            require(info["apex_keys"][name]["container_key"] == "PRESIGNED",
                    "Cannot preserve a development-signed APEX as PRESIGNED: " + name)
            arguments += ["--extra_apks", name + "=", "--extra_apex_payload_key", name + "="]
        else:
            require(row["container_key"] != "PRESIGNED" and row["payload_key"] != "PRESIGNED",
                    "APEX payload/container must be signed together: " + name)
            if source_container not in SPECIAL:
                require(row["container_key"] == config["key_map"][source_container],
                        "APEX container must match key_map for SELinux certificate remapping: " + name)
            arguments += ["--extra_apks", name + "=" + certificate(key_dir, row["container_key"]),
                          "--extra_apex_payload_key", name + "=" + key_file(key_dir, row["payload_key"])]
    resolved_avb = {}
    for partition, row in sorted(config["avb"].items()):
        require(partition in AVB_OPTIONS and partition in IMAGES,
                "Unsupported Crux AVB partition: " + partition)
        require(set(row) == {"key", "algorithm"} and isinstance(row["algorithm"], str)
                and re.fullmatch(r"SHA(?:256|512)_RSA(?:2048|4096|8192)", row["algorithm"]),
                "Missing or invalid AVB signing algorithm: " + partition)
        require("avb_" + partition + "_key_path" in info["misc"]
                and "avb_" + partition + "_algorithm" in info["misc"],
                "Platform signer cannot persist absent AVB metadata fields for " + partition)
        key = key_file(key_dir, row["key"])
        resolved_avb[partition] = {"key": key, "algorithm": row["algorithm"]}
        arguments += ["--avb_" + partition + "_key", key,
                      "--avb_" + partition + "_algorithm", row["algorithm"]]
    return arguments, ota_key, resolved_map, resolved_avb


def check_signed_target(path, info, resolved_map, resolved_avb, host):
    signed_info = inventory(path, host)
    for partition, row in resolved_avb.items():
        for field in ("key_path", "algorithm"):
            expected = row["key" if field == "key_path" else field]
            require(signed_info["misc"].get("avb_" + partition + "_" + field) == expected,
                    "Signer did not persist AVB " + field + " for " + partition)
    expected_main = {Path(resolved_map[k] + ".x509.pem").read_bytes() for k in info["ota_sources"]}
    certificates = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not name.endswith("mac_permissions.xml"):
                continue
            body = archive.read(name).decode("utf-8")
            for source, destination in resolved_map.items():
                if source in SPECIAL:
                    continue
                old_der = ssl.PEM_cert_to_DER_cert(
                    (SOURCE_ROOT / (source + ".x509.pem")).read_text()).hex()
                new_der = ssl.PEM_cert_to_DER_cert(Path(destination + ".x509.pem").read_text()).hex()
                require(old_der == new_der or not re.search(r'\b' + old_der + r'\b', body, re.IGNORECASE),
                        "Stale signing certificate in " + name)
        for name in archive.namelist():
            if not name.endswith("/otacerts.zip"):
                continue
            extras = info["extra_recovery_sources"] if name.startswith(("BOOT/", "RECOVERY/", "VENDOR_BOOT/")) else info["extra_ota_sources"]
            expected = expected_main | {Path(resolved_map[k] + ".x509.pem").read_bytes() for k in extras}
            with zipfile.ZipFile(io.BytesIO(archive.read(name))) as certs:
                require({certs.read(n) for n in certs.namelist()} == expected,
                        "OTA verification certificates were not replaced in " + name)
            certificates.append(name)
    require(any(name.startswith("RECOVERY/") for name in certificates),
            "Signed target-files lacks Recovery OTA verification certificates")
    return signed_info, certificates


def whole_file_signature_footer(path):
    size = path.stat().st_size
    require(size >= 6, "OTA ZIP is too short for a whole-file signature footer")
    with path.open("rb") as source:
        source.seek(-6, os.SEEK_END)
        footer = source.read(6)
        require(footer[2:4] == b"\xff\xff", "OTA ZIP lacks a whole-file signature footer")
        signature_start = int.from_bytes(footer[:2], "little")
        comment_size = int.from_bytes(footer[4:], "little")
        require(6 < signature_start <= comment_size, "Invalid OTA whole-file signature offset")
        require(size >= comment_size + 22, "OTA ZIP is too short for the signed EOCD")
        source.seek(-(comment_size + 22), os.SEEK_END)
        eocd = source.read(comment_size + 22)
    require(eocd[:4] == b"PK\x05\x06", "OTA whole-file footer does not locate the ZIP EOCD")
    require(int.from_bytes(eocd[20:22], "little") == comment_size,
            "OTA whole-file footer and EOCD comment lengths differ")
    require(b"PK\x05\x06" not in eocd[4:], "Additional EOCD marker in OTA signature tail")
    return {"format": "Android whole-file ZIP-comment signature", "comment_bytes": comment_size,
            "signature_blob_offset": size - signature_start, "signature_blob_bytes": signature_start - 6,
            "signed_bytes": size - comment_size - 2,
            "verification": "Footer/EOCD structure only; PKCS7 parsing and trusted-key crypto require Recovery verify_file."}


def inspect_ota(path, expected_certificate=None):
    signature = whole_file_signature_footer(path)
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), "Duplicate entries in OTA ZIP")
        require(not any(n.startswith(("RADIO/", "firmware-update/", "install/firmware-update/"))
                        or n.endswith("userdata.img") for n in names),
                "OTA ZIP bundles firmware or fixed-size userdata")
        metadata_text = text_entry(archive, "META-INF/com/android/metadata")
        metadata = dictionary(metadata_text)
        require(metadata.get("ota-type") == "BLOCK" and metadata.get("pre-device") == "crux",
                "OTA metadata does not describe a non-A/B Crux package")
        require("pre-build" not in metadata and metadata.get("ota-wipe") != "yes",
                "Expected a full data-preserving OTA")
        require("payload.bin" not in names, "A/B payload present in OTA ZIP")
        script = text_entry(archive, "META-INF/com/google/android/updater-script")
        paths = sorted(set(re.findall(r'/dev/block/[^\s"\',;)]+', script)))
        partitions = set()
        for path in paths:
            match = re.fullmatch(r'/dev/block/(?:bootdevice/)?by-name/([^/]+)', path)
            require(match is not None and match.group(1) in IMAGES,
                    "Unexpected updater block-device reference: " + path)
            partitions.add(match.group(1))
        require(not re.search(r'(?:format|delete_recursive)\([^;]*"/(?:data|metadata)"', script),
                "Updater script wipes user data or metadata")
        require({"boot", "system", "vendor", "dtbo", "vbmeta"} <= partitions,
                "OTA updater lacks an expected Android partition reference")
        certificate_name = "META-INF/com/android/otacert"
        require(certificate_name in names and archive.getinfo(certificate_name).file_size > 0,
                "Whole-file OTA lacks its public otacert entry")
        if expected_certificate is not None:
            require(archive.read(certificate_name) == expected_certificate,
                    "OTA public otacert differs from the selected package certificate")
        for name in names:
            if name.endswith(".img"):
                require(name in {image + ".img" for image in IMAGES},
                        "Unexpected raw image in OTA ZIP: " + name)
        return {"metadata": metadata, "whole_file_signature": signature,
                "otacert_matches_selected_key": True if expected_certificate is not None else None,
                "updater_block_references": paths,
                "updater_partitions": sorted(partitions),
                "updater_external_programs": re.findall(r'run_program\([^;]+', script),
                "raw_images": sorted(n for n in names if n.endswith(".img")),
                "inspection_limit": "Literal updater-script references only; root must review updater binary, install helpers and post-boot Recovery installer."}, metadata_text, script


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def run_tool(command, log_path, commands):
    print("Running " + Path(command[0]).name, flush=True)
    with log_path.open("w") as log:
        process = subprocess.run(command, cwd=SOURCE_ROOT, stdout=log, stderr=subprocess.STDOUT)
    commands.append({"argv": command, "exit_code": process.returncode, "log": log_path.name})
    require(process.returncode == 0,
            Path(command[0]).name + " failed (exit " + str(process.returncode) + "); see " + str(log_path))


def package(args, info):
    key_dir = args.key_dir.resolve()
    output = args.output_dir.resolve()
    require(key_dir.is_dir(), "External private-key directory does not exist")
    require(not beneath(key_dir, SOURCE_ROOT) and not beneath(key_dir, output)
            and not beneath(output, key_dir), "Keep private keys outside the source tree and output bundle")
    with args.config.open() as stream:
        config = json.load(stream)
    sign_args, ota_key, key_map, avb = signing_arguments(info, config, key_dir)
    host = args.host_tools.resolve()
    tools = {name: host / "bin" / name for name in
             ("sign_target_files_apks", "ota_from_target_files", "img_from_target_files")}
    for name in ("sign_target_files_apks", "ota_from_target_files") + (("img_from_target_files",) if args.images else ()):
        require(tools[name].is_file() and os.access(tools[name], os.X_OK),
                "Missing executable platform releasetool: " + str(tools[name]) + "; build host tools first")
    require(shutil.which("java") is not None,
            "Platform signing requires executable java on PATH; source build/envsetup.sh and lunch aosp_crux_release-user, or add the declared prebuilts/jdk/jdk11/linux-x86/bin to PATH")
    require(not output.exists(), "Output directory already exists; choose a new directory")
    if args.validate_only:
        print("Inputs, reviewed signing configuration and platform tool paths validated; no signing performed.")
        return
    output.mkdir(parents=True)
    report = {"status": "incomplete", "source_target_files": str(args.target_files.resolve()),
              "signing_configuration": config, "commands": [], "avb_input_arguments": {
                  k: v for k, v in info["misc"].items() if k.startswith("avb_") and k.endswith("args")},
              "shipping_inventory": {k: info[k] for k in ("apk_keys", "apex_keys", "flat_apk_paths", "apex_paths", "apex_inspections")},
              "generated_version": info["generated_version"],
              "source_key_probe_notice": info["source_key_probe_notice"]}
    try:
        signed = output / "signed-target_files.zip"
        run_tool([str(tools["sign_target_files_apks"]), "--path", str(host)] + sign_args
                 + [str(args.target_files.resolve()), str(signed)], output / "sign.log", report["commands"])
        signed_info, certs = check_signed_target(signed, info, key_map, avb, host)
        report["ota_certificate_archives"] = certs
        report["resolved_avb"] = avb
        report["signed_build_properties"] = signed_info["build_properties"]
        report["signed_generated_version"] = signed_info["generated_version"]
        ota = output / "ota.zip"
        run_tool([str(tools["ota_from_target_files"]), "--path", str(host),
                  "--package_key", ota_key, str(signed), str(ota)],
                 output / "ota.log", report["commands"])
        report["ota"], metadata, script = inspect_ota(ota, Path(ota_key + ".x509.pem").read_bytes())
        (output / "ota-metadata.txt").write_text(metadata)
        (output / "updater-script.txt").write_text(script)
        report["post_boot_recovery_installers"] = []
        with zipfile.ZipFile(signed) as archive:
            for name in archive.namelist():
                if name.endswith("/bin/install-recovery.sh"):
                    body = archive.read(name).decode("utf-8")
                    require(not re.search(r'/by-name/pe(?:system|vendor|userdata|metadata)\b', body),
                            "Development partition in post-boot Recovery installer")
                    (output / ("install-recovery-" + Path(name).parts[0] + ".sh")).write_text(body)
                    report["post_boot_recovery_installers"].append(name)
        if args.images:
            images = output / "images.zip"
            run_tool([str(tools["img_from_target_files"]), "--path", str(host), str(signed), str(images)],
                     output / "images.log", report["commands"])
            with zipfile.ZipFile(images) as archive:
                names = archive.namelist()
                require(len(names) == len(set(names)) and all(
                    n in {p + ".img" for p in IMAGES | {"cache"}} | {"android-info.txt"} for n in names),
                    "Image bundle contains unexpected partition/firmware/userdata entries")
                require(all(p + ".img" in names for p in IMAGES), "Incomplete same-source image bundle")
                report["image_bundle_entries"] = names
        report["status"] = "complete"
        report["cryptographic_verification"] = "Platform signing commands succeeded; independent signature/content acceptance remains with root."
        print("Package and inspection report written to " + str(output))
    except (PackageError, OSError, ValueError, KeyError, TypeError, AttributeError, zipfile.BadZipFile) as error:
        report["status"] = "failed"
        report["error"] = str(error)
        raise
    finally:
        write_json(output / "package-report.json", report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="derive a signing inventory and unfilled configuration")
    inspect.add_argument("--target-files", required=True, type=Path)
    inspect.add_argument("--host-tools", required=True, type=Path, help="matching built host directory; APEX inspection needs bin/deapexer and bin/debugfs_static")
    inspect.add_argument("--output", type=Path, help="write JSON here instead of stdout")
    release = commands.add_parser("package", help="sign target-files, then generate a signed full Recovery OTA")
    release.add_argument("--target-files", required=True, type=Path)
    release.add_argument("--key-dir", required=True, type=Path)
    release.add_argument("--config", required=True, type=Path, help="reviewed JSON derived from inspect")
    release.add_argument("--host-tools", required=True, type=Path, help="built platform host directory containing bin/ and framework/")
    release.add_argument("--output-dir", required=True, type=Path, help="new directory; existing directories are never overwritten")
    release.add_argument("--images", action="store_true", help="also run img_from_target_files on the signed target-files")
    release.add_argument("--validate-only", action="store_true", help="validate all inputs and tool paths without signing")
    args = parser.parse_args()
    try:
        info = inventory(args.target_files, args.host_tools.resolve())
        if args.command == "inspect":
            report = {"inventory": info, "signing_configuration_template": template(info)}
            if args.output:
                write_json(args.output, report)
            else:
                print(json.dumps(report, indent=2, sort_keys=True))
        else:
            package(args, info)
    except (PackageError, OSError, ValueError, KeyError, TypeError, AttributeError, zipfile.BadZipFile) as error:
        print("error: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
