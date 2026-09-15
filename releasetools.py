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

"""Crux non-A/B OTA hooks. The installed stock firmware is retained."""

import common


# These Android images must match the boot/system images in the same OTA.
# Bootloader, modem and other stock firmware partitions are never updated here.
ANDROID_IMAGES = ("dtbo.img", "vbmeta.img")


def _validate_target_files(input_zip):
    entries = input_zip.infolist()
    names = [entry.filename for entry in entries]
    for entry in entries:
        if not entry.is_dir() and entry.filename.startswith(
                ("RADIO/", "INSTALL/firmware-update/")):
            raise ValueError(
                "Crux ROM OTAs must not bundle stock firmware: " + entry.filename)
    for image in ANDROID_IMAGES:
        path = "IMAGES/" + image
        if names.count(path) != 1:
            raise ValueError("Crux target-files must contain exactly one " + path)
        if input_zip.getinfo(path).file_size == 0:
            raise ValueError("Crux target-files contains an empty " + path)


def _install_android_images(info, input_zip):
    _validate_target_files(input_zip)
    for image in ANDROID_IMAGES:
        common.ZipWriteStr(info.output_zip, image, input_zip.read("IMAGES/" + image))
        partition = image[:-4]
        info.script.Print("Flashing {} image...".format(partition))
        info.script.AppendExtra(
            'assert(package_extract_file("%s", '
            '"/dev/block/bootdevice/by-name/%s"));' % (image, partition))


def FullOTA_InstallBegin(info):
    _validate_target_files(info.input_zip)


def FullOTA_InstallEnd(info):
    _install_android_images(info, info.input_zip)


def IncrementalOTA_InstallBegin(info):
    _validate_target_files(info.target_zip)


def IncrementalOTA_InstallEnd(info):
    _install_android_images(info, info.target_zip)
