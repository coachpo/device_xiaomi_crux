#!/bin/bash
#
# Copyright (C) 2018-2020 The LineageOS Project
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

set -e

DEVICE=crux
VENDOR=xiaomi
INITIAL_COPYRIGHT_YEAR=2020

# Load extract_utils and do some sanity checks
MY_DIR="${BASH_SOURCE%/*}"
if [[ ! -d "${MY_DIR}" ]]; then MY_DIR="${PWD}"; fi

ANDROID_ROOT="${MY_DIR}/../.."

HELPER="${ANDROID_ROOT}/tools/extract-utils/extract_utils.sh"
if [ ! -f "${HELPER}" ]; then
    echo "Unable to find helper script at ${HELPER}"
    exit 1
fi
source "${HELPER}"

AUTHORITATIVE_VENDOR_DIR="${ANDROID_ROOT}/vendor_and_device/vendor_${VENDOR}_${DEVICE}"
LEGACY_VENDOR_DIR="${ANDROID_ROOT}/vendor/${VENDOR}/${DEVICE}"
CREATED_VENDOR_LINK=false

function cleanup_vendor_dir() {
    if [ "${CREATED_VENDOR_LINK}" = "true" ] && [ -L "${LEGACY_VENDOR_DIR}" ]; then
        rm -f "${LEGACY_VENDOR_DIR}"
    fi
}

function ensure_vendor_dir() {
    if [ ! -d "${AUTHORITATIVE_VENDOR_DIR}" ]; then
        echo "Unable to find authoritative vendor directory at ${AUTHORITATIVE_VENDOR_DIR}"
        exit 1
    fi

    mkdir -p "$(dirname "${LEGACY_VENDOR_DIR}")"

    if [ -L "${LEGACY_VENDOR_DIR}" ]; then
        if [ "$(readlink -f "${LEGACY_VENDOR_DIR}")" != "$(readlink -f "${AUTHORITATIVE_VENDOR_DIR}")" ]; then
            echo "Legacy vendor path ${LEGACY_VENDOR_DIR} does not point to ${AUTHORITATIVE_VENDOR_DIR}"
            exit 1
        fi
        return
    fi

    if [ -e "${LEGACY_VENDOR_DIR}" ]; then
        echo "Legacy vendor path ${LEGACY_VENDOR_DIR} already exists and is not a symlink"
        exit 1
    fi

    ln -s "${AUTHORITATIVE_VENDOR_DIR}" "${LEGACY_VENDOR_DIR}"
    CREATED_VENDOR_LINK=true
}

trap cleanup_vendor_dir EXIT
ensure_vendor_dir

# Initialize the helper
setup_vendor "${DEVICE}" "${VENDOR}" "${ANDROID_ROOT}"

# Copyright headers and guards
write_headers "${DEVICE}"

# The standard common blobs
write_makefiles "${MY_DIR}/proprietary-files.txt" true

# Finish
write_footers
