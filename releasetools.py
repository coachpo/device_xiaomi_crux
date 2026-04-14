# Copyright (C) 2009 The Android Open Source Project
# Copyright (C) 2019 The Mokee Open Source Project
# Copyright (C) 2020 The LineageOS Open Source Project
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

import importlib
from typing import Any

common = importlib.import_module("common")


# The official crux fastboot ROM exposes discrete system/vendor partitions. Keep
# any super_dummy handling as external build glue instead of modeling a real
# crux super partition in this device-specific firmware script.
FIRMWARE_IMAGES = (
    ("abl.elf", ("abl", "ablbak")),
    ("aop.mbn", ("aop", "aopbak")),
    ("BTFM.bin", ("bluetooth",)),
    ("cmnlib.mbn", ("cmnlib", "cmnlibbak")),
    ("cmnlib64.mbn", ("cmnlib64", "cmnlib64bak")),
    ("devcfg.mbn", ("devcfg", "devcfgbak")),
    ("dspso.bin", ("dsp",)),
    ("hyp.mbn", ("hyp", "hypbak")),
    ("imagefv.elf", ("imagefv",)),
    ("km4.mbn", ("keymaster", "keymasterbak")),
    ("NON-HLOS.bin", ("modem",)),
    ("qupv3fw.elf", ("qupfw", "qupfwbak")),
    ("storsec.mbn", ("storsec",)),
    ("tz.mbn", ("tz", "tzbak")),
    ("uefi_sec.mbn", ("uefisecapp", "uefisecappbak")),
    ("xbl.elf", ("xbl", "xblbak")),
    ("xbl_config.elf", ("xbl_config", "xbl_configbak")),
)

SUPER_DUMMY_SCRIPT = "install/bin/flash_super_dummy.sh"

PARTITION_IMAGES = (
    ("dtbo.img", "/dev/block/bootdevice/by-name/dtbo"),
    ("vbmeta.img", "/dev/block/bootdevice/by-name/vbmeta"),
)


def FullOTA_InstallBegin(info: Any) -> None:
    OTA_InstallSuperDummy(info, info.input_zip)
    return


def FullOTA_InstallEnd(info: Any) -> None:
    input_zip = info.input_zip
    OTA_UpdateFirmware(info)
    OTA_InstallEnd(info, input_zip)
    return


def IncrementalOTA_InstallEnd(info: Any) -> None:
    input_zip = info.target_zip
    OTA_UpdateFirmware(info)
    OTA_InstallEnd(info, input_zip)
    return


def OTA_UpdateFirmware(info: Any) -> None:
    info.script.Print("Patching official crux firmware images...")

    for image, partitions in FIRMWARE_IMAGES:
        for partition in partitions:
            info.script.AppendExtra(
                'package_extract_file("install/firmware-update/{image}", "/dev/block/bootdevice/by-name/{partition}");'.format(
                    image=image, partition=partition
                )
            )


def OTA_InstallSuperDummy(info: Any, input_zip: Any) -> None:
    if SUPER_DUMMY_SCRIPT not in input_zip.namelist():
        info.script.Print("Skipping optional crux super_dummy helper wiring")
        return

    if not AddImage(
        info, input_zip, "super_dummy.img", "/tmp/super_dummy.img", directory="RADIO"
    ):
        info.script.Print("Skipping optional crux super_dummy compatibility image")
        return

    common.ZipWriteStr(
        info.output_zip, SUPER_DUMMY_SCRIPT, input_zip.read(SUPER_DUMMY_SCRIPT)
    )
    info.script.Print("Applying optional crux super_dummy compatibility image...")
    info.script.AppendExtra(
        'package_extract_file("{script}", "/tmp/flash_super_dummy.sh");'.format(
            script=SUPER_DUMMY_SCRIPT
        )
    )
    info.script.AppendExtra(
        'set_metadata("/tmp/flash_super_dummy.sh", "uid", 0, "gid", 0, "mode", 0755);'
    )
    info.script.AppendExtra('run_program("/tmp/flash_super_dummy.sh");')


def AddImage(
    info: Any,
    input_zip: Any,
    basename: str,
    destination: str,
    directory: str = "IMAGES",
) -> bool:
    path = "{}/{}".format(directory, basename)
    if path not in input_zip.namelist():
        return False

    data = input_zip.read(path)
    common.ZipWriteStr(info.output_zip, basename, data)
    info.script.Print("Flashing {} image".format(destination.rsplit("/", 1)[-1]))
    info.script.AppendExtra(
        'package_extract_file("{image}", "{destination}");'.format(
            image=basename, destination=destination
        )
    )
    return True


def OTA_InstallEnd(info: Any, input_zip: Any) -> None:
    for image, destination in PARTITION_IMAGES:
        AddImage(info, input_zip, image, destination)

    return
