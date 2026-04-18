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

import common  # pyright: ignore[reportMissingImports]
import re

FIRMWARE_IMAGES = (
  ("abl.elf", "/dev/block/bootdevice/by-name/abl"),
  ("abl.elf", "/dev/block/bootdevice/by-name/ablbak"),
  ("aop.mbn", "/dev/block/bootdevice/by-name/aop"),
  ("aop.mbn", "/dev/block/bootdevice/by-name/aopbak"),
  ("BTFM.bin", "/dev/block/bootdevice/by-name/bluetooth"),
  ("cmnlib.mbn", "/dev/block/bootdevice/by-name/cmnlib"),
  ("cmnlib.mbn", "/dev/block/bootdevice/by-name/cmnlibbak"),
  ("cmnlib64.mbn", "/dev/block/bootdevice/by-name/cmnlib64"),
  ("cmnlib64.mbn", "/dev/block/bootdevice/by-name/cmnlib64bak"),
  ("devcfg.mbn", "/dev/block/bootdevice/by-name/devcfg"),
  ("devcfg.mbn", "/dev/block/bootdevice/by-name/devcfgbak"),
  ("dspso.bin", "/dev/block/bootdevice/by-name/dsp"),
  ("hyp.mbn", "/dev/block/bootdevice/by-name/hyp"),
  ("imagefv.elf", "/dev/block/bootdevice/by-name/imagefv"),
  ("km4.mbn", "/dev/block/bootdevice/by-name/keymaster"),
  ("km4.mbn", "/dev/block/bootdevice/by-name/keymasterbak"),
  ("NON-HLOS.bin", "/dev/block/bootdevice/by-name/modem"),
  ("qupv3fw.elf", "/dev/block/bootdevice/by-name/qupfw"),
  ("qupv3fw.elf", "/dev/block/bootdevice/by-name/qupfwbak"),
  ("storsec.mbn", "/dev/block/bootdevice/by-name/storsec"),
  ("tz.mbn", "/dev/block/bootdevice/by-name/tz"),
  ("tz.mbn", "/dev/block/bootdevice/by-name/tzbak"),
  ("uefi_sec.mbn", "/dev/block/bootdevice/by-name/uefisecapp"),
  ("uefi_sec.mbn", "/dev/block/bootdevice/by-name/uefisecappbak"),
  ("xbl.elf", "/dev/block/bootdevice/by-name/xbl"),
  ("xbl.elf", "/dev/block/bootdevice/by-name/xblbak"),
  ("xbl_config.elf", "/dev/block/bootdevice/by-name/xbl_config"),
  ("xbl_config.elf", "/dev/block/bootdevice/by-name/xbl_configbak"),
)

def FullOTA_InstallBegin(info):
  return

def FullOTA_InstallEnd(info):
  input_zip = info.input_zip
  OTA_UpdateFirmware(info, input_zip)
  OTA_InstallEnd(info, input_zip)
  return

def IncrementalOTA_InstallEnd(info):
  input_zip = info.target_zip
  OTA_UpdateFirmware(info, input_zip)
  OTA_InstallEnd(info, input_zip)
  return

def AddFirmware(info, input_zip, basename):
  path = "RADIO/" + basename
  if path not in input_zip.namelist():
    return False

  common.ZipWriteStr(info.output_zip,
                     "install/firmware-update/" + basename,
                     input_zip.read(path))
  return True

def OTA_UpdateFirmware(info, input_zip):
  packaged_firmware = set()
  for basename, dest in FIRMWARE_IMAGES:
    if basename not in packaged_firmware and AddFirmware(info, input_zip, basename):
      packaged_firmware.add(basename)

    if basename in packaged_firmware:
      info.script.AppendExtra(
          'package_extract_file("install/firmware-update/%s", "%s");' %
          (basename, dest))

def AddImage(info, dir, input_zip, basename, dest):
  path = dir + "/" + basename
  if path not in input_zip.namelist():
    return

  data = input_zip.read(path)
  common.ZipWriteStr(info.output_zip, basename, data)
  info.script.Print("Flashing {} image".format(dest.split('/')[-1]))
  info.script.AppendExtra('package_extract_file("%s", "%s");' % (basename, dest))

def OTA_InstallEnd(info, input_zip):
  info.script.Print("Patching firmware images...")
  AddImage(info, "IMAGES", input_zip, "dtbo.img", "/dev/block/bootdevice/by-name/dtbo")
  AddImage(info, "IMAGES", input_zip, "vbmeta.img", "/dev/block/bootdevice/by-name/vbmeta")
  return
