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

# Load extract_utils and do some sanity checks
MY_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ANDROID_ROOT="$(cd -- "${MY_DIR}/../../.." && pwd)"

HELPER="${ANDROID_ROOT}/tools/extract-utils/extract_utils.sh"
if [ ! -f "${HELPER}" ]; then
    echo "Unable to find helper script at ${HELPER}"
    exit 1
fi
source "${HELPER}"

# Default to sanitizing the vendor folder before extraction
CLEAN_VENDOR=true

while [ "${#}" -gt 0 ]; do
    case "${1}" in
        -n | --no-cleanup )
                CLEAN_VENDOR=false
                ;;
        -k | --kang )
                KANG="--kang"
                ;;
        -s | --section )
                if [ "${#}" -lt 2 ] || [[ "${2}" == -* ]]; then
                    echo "${1} requires a section name" >&2
                    exit 1
                fi
                SECTION="${2}"; shift
                CLEAN_VENDOR=false
                ;;
        * )
                SRC="${1}"
                ;;
    esac
    shift
done

if [ -z "${SRC}" ]; then
    SRC="adb"
fi

function blob_fixup() {
    case "${1}" in
    vendor/lib64/hw/camera.qcom.so)
        "${PATCHELF}" --remove-needed "libMegviiFacepp-0.5.2.so" "${2}" || exit 1
        "${PATCHELF}" --remove-needed "libmegface.so" "${2}" || exit 1
        add_needed "libshim_megvii.so" "${2}"
        ;;
    vendor/lib64/camera/components/com.qti.node.watermark.so)
        add_needed "libpiex_shim.so" "${2}"
        ;;
    esac
}

function add_needed() {
    local needed
    needed="$("${PATCHELF}" --print-needed "${2}")" || exit 1
    # A ROM dump may already contain the patched library.
    if ! grep -Fxq -- "${1}" <<< "${needed}"; then
        "${PATCHELF}" --add-needed "${1}" "${2}" || exit 1
    fi
}

# Initialize the helper
setup_vendor "${DEVICE}" "${VENDOR}" "${ANDROID_ROOT}" false "${CLEAN_VENDOR}"

extract "${MY_DIR}/proprietary-files.txt" "${SRC}" \
        "${KANG}" --section "${SECTION}"

"${MY_DIR}/setup-makefiles.sh"
