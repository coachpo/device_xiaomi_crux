# Local release packaging

`package_release.py` consumes an existing `aosp_crux_release-user` target-files
ZIP. Run it inside the matching Linux build tree after the platform's hermetic
`sign_target_files_apks`, `ota_from_target_files` and, optionally,
`img_from_target_files` host tools have been built. It does not build a ROM or
generate keys. Both commands require `--host-tools`; inspecting re-signed APEX
payloads also requires that directory's `bin/deapexer` and `bin/debugfs_static`.

Set up the declared build environment before packaging so the platform signer's
default `java` executable is on PATH:

```sh
source build/envsetup.sh
lunch aosp_crux_release-user
```

For packaging an already built archive in this tree, you can instead add the
existing declared JDK directly, from the Android source root:

```sh
export PATH="$PWD/prebuilts/jdk/jdk11/linux-x86/bin:$PATH"
```

`package`, including `--validate-only`, checks that executable `java` is found on
PATH before creating its output directory. The script does not launch Java during
that check or install/select a JDK.

First derive the signing requirements from the actual archive:

```sh
python3 device/xiaomi/crux/scripts/package_release.py inspect \
  --target-files /path/to/crux-target_files.zip \
  --host-tools /path/to/build-out/host/linux-x86 \
  --output /path/to/signing-inventory.json
```

Copy `signing_configuration_template` from that JSON into a separate reviewed
configuration file. Fill every `null`; do not pass the whole inventory as the
configuration. Values referring to destination keys are paths relative to the
external private-key directory:

- `key_map` maps the source certificate prefixes used by actual flat APKs,
  including the metadata-declared compression suffix, nested APKs in re-signed
  APEX payloads, present APEX containers and OTA verification to destination
  prefixes. A prefix requires both
  `<prefix>.x509.pem` and `<prefix>.pk8`. Source certificates must remain readable
  in the matching source tree so platform SELinux certificate remapping works.
- `ota_package_key` selects a destination prefix trusted by the mapped main OTA
  certificates. The script both replaces Recovery verification certificates and
  supplies this package key explicitly to the OTA generator.
- `apex` covers only present APEX containers, using their names from
  `META/apexkeys.txt` with `.capex` normalized to `.apex`. `container_key` is a
  certificate prefix matching its `key_map` destination; `payload_key` is the full
  filename of the payload private PEM key. An already `PRESIGNED` APEX must retain
  both fields as `PRESIGNED`; overriding its container or payload with a new key
  is rejected before signing. The script does not invent names or use the same
  key for unrelated APEX payloads without an explicit configuration.
- `avb` maps partition names to a private PEM filename and the reviewed signing
  algorithm. Required roles come from the actual metadata. Optional roles must
  already have `avb_<partition>_key_path` and `avb_<partition>_algorithm` fields in
  the target-files metadata because this platform signer preserves only existing
  metadata fields. Existing AVB flags, rollback indices and locations remain in
  force and are recorded for review.

The inventory lists nested APK paths using matching platform tools in isolated
temporary directories; compressed APEXes are decompressed before listing. An
already `PRESIGNED` outer APEX is preserved without nested re-signing obligations.
Identical repeated signing declarations, including partition-only differences,
are coalesced. Conflicting declarations for a consumed basename stop inspection.
Unused global APK/APEX declarations do not require new release keys.

Original target-files metadata remains unchanged. The standard signer can still
probe unused source test-key paths while collecting key passwords; this is
accepted and does not make those modules part of the shipping signing inventory.
Input certificate declarations and the requested configuration do not establish
the signatures in the final APK/APEX binaries; inspect those independently.

Keep private keys outside the Android source tree and the output directory.
Use a new output directory for every invocation:

```sh
python3 device/xiaomi/crux/scripts/package_release.py package \
  --target-files /path/to/crux-target_files.zip \
  --key-dir /path/outside/source/private-release-keys \
  --config /path/to/reviewed-signing.json \
  --host-tools /path/to/build-out/host/linux-x86 \
  --output-dir /path/to/new-release-output --images
```

Add `--validate-only` to check all inputs, mappings, key-file existence and tool
paths without signing or creating output. Missing configuration, keys or tools
stop the command. Encrypted keys use the platform tool's existing password
handling; private key contents are never included in the script's reports.

Outputs are signed target-files, a signed full non-A/B `ota.zip`, and optional
`images.zip` generated from those same signed target-files. Reports retain command
arguments and exits, tool logs, OTA metadata, the updater script and post-boot
Recovery installers. The source target-files must contain the six Android images
boot/Recovery/system/vendor/DTBO/vbmeta, ordinary stock Recovery mappings, no radio
or bootloader payloads and no fixed-size userdata image. Boot and Recovery require
rebuildable `BOOT/` and `RECOVERY/` source trees containing their kernels and
ramdisk entries. `BOOTABLE_IMAGES` and `PREBUILT_IMAGES/boot.img` or
`PREBUILT_IMAGES/recovery.img` are rejected because prebuilt images can retain
routing or OTA certificates that differ from the rewritten source trees. Final
archives and mapped OTA certificates are checked again.

Literal updater block references may use `/dev/block/by-name/` or
`/dev/block/bootdevice/by-name/`. Validation uses the partition identity after
either exact prefix: only boot/Recovery/system/vendor/DTBO/vbmeta are allowed,
and boot/system/vendor/DTBO/vbmeta must all appear. PE aliases, firmware, misc,
data/metadata and other device paths are rejected.

Non-A/B OTA signing uses the platform's whole-file signature in the ZIP comment,
with detached PKCS7 data and a six-byte footer. The wrapper checks that footer's
offset/length constraints, its EOCD placement and comment length, absence of a
later EOCD marker, and the public `META-INF/com/android/otacert` against the selected
package certificate. This is structural inspection. PKCS7 parsing and trusted-key
cryptographic acceptance require Recovery's `verify_file`; JAR `.RSA` entries are
not required by this format.

`package-report.json` becomes `complete` only after those checks. Failed output
directories retain a `failed` report and logs for diagnosis and are not release
artifacts. The report's block references cover literal updater-script paths;
independent final signature, updater-binary/helper behavior, cache/BCB effects,
post-boot Recovery installation and device acceptance belong to the release
owner. This script provides no device flashing or installation command.

Run the portable packaging contract suite from the Android source root:

```sh
python3 -B -m unittest discover \
  -s device/xiaomi/crux/scripts/tests -p test_package_release.py -v
```

It uses isolated temporary source/key placeholders, mocked platform commands and
a synthetic whole-file signature envelope. No real release keys, ROM artifacts,
JDK launch, signing or device access are needed. The suite checks wrapper contracts;
trusted-key cryptographic acceptance remains the native Recovery integration test.
