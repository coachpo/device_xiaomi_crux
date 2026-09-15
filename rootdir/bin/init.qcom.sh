#! /vendor/bin/sh

# Copyright (c) 2009-2016, The Linux Foundation. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of The Linux Foundation nor
#       the names of its contributors may be used to endorse or promote
#       products derived from this software without specific prior written
#       permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NON-INFRINGEMENT ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
# OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR
# OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
# ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#

# Keep the RIL cache intact until a complete firmware configuration is available.
modem_config=/data/vendor/modem_config
firmware_config=/vendor/firmware_mnt/image/modem_pr/mcfg/configs
firmware_version=/vendor/firmware_mnt/verinfo/ver_info.txt
firmware_ota=/vendor/firmware_mnt/image/modem_pr/mbn_ota.txt

if [ ! -s "$firmware_version" ] || [ ! -r "$firmware_version" ] ||
        [ ! -d "$firmware_config" ] || [ ! -r "$firmware_ota" ]; then
    echo "Modem firmware configuration is unavailable" >&2
    exit 1
fi

cache_contents_present() {
    cmp -s "$firmware_ota" "$modem_config/mbn_ota.txt" || return 1
    entries=$(find "$firmware_config" -mindepth 1) || return 1
    [ -n "$entries" ] || return 1
    while IFS= read -r source; do
        cached="$modem_config/${source#"$firmware_config"/}"
        if [ -L "$source" ]; then
            [ -L "$cached" ] || return 1
        elif [ -d "$source" ]; then
            [ -d "$cached" ] && [ ! -L "$cached" ] || return 1
        elif [ -f "$source" ]; then
            [ -f "$cached" ] && [ ! -L "$cached" ] || return 1
            if [ -s "$source" ]; then
                [ -s "$cached" ] || return 1
            fi
        else
            return 1
        fi
    done <<EOF
$entries
EOF
}

# Older copies could publish ver_info.txt after a failed configs copy. A matching
# version alone must not make an incomplete cache ready for the RIL.
if [ -f "$modem_config/ver_info.txt" ] &&
        cmp -s "$firmware_version" "$modem_config/ver_info.txt" &&
        cache_contents_present; then
    chmod g-w "$modem_config" || exit 1
    setprop ro.vendor.ril.mbn_copy_completed 1
    exit $?
fi

# Stage inside the existing modem-data directory to retain its SELinux type.
# Replacing its root would require write access to the parent /data/vendor.
chmod g+w "$modem_config" || exit 1
staging=$(mktemp -d "$modem_config/.staging.XXXXXX") || exit 1
backup=""
replacing=0
installing=0

cleanup() {
    if [ "$replacing" = 1 ]; then
        if [ "$installing" = 1 ]; then
            chmod -R g+w "$modem_config"/* 2>/dev/null
            rm -rf "$modem_config"/* || return 1
        fi
        for entry in "$backup"/*; do
            [ -e "$entry" ] || [ -L "$entry" ] || continue
            mv "$entry" "$modem_config/" || return 1
        done
    fi
    chmod -R g+w "$staging" 2>/dev/null
    rm -rf "$staging" || return 1
    if [ -n "$backup" ]; then
        chmod -R g+w "$backup" 2>/dev/null
        rm -rf "$backup" || return 1
    fi
    chmod g-w "$modem_config"
}

trap 'status=$?; cleanup || status=1; exit "$status"' EXIT
trap 'exit 1' HUP INT TERM

# A failed copy, including an empty configs directory, never touches the cache.
cp --preserve=m -dr "$firmware_config"/* "$staging/" &&
    cp --preserve=m -d "$firmware_ota" "$staging/" &&
    cp --preserve=m -d "$firmware_version" "$staging/" &&
    chown -hR radio.root "$staging"/* || exit 1

backup=$(mktemp -d "$modem_config/.previous.XXXXXX") || exit 1
replacing=1
# Move the version marker first and publish its replacement last. An interrupted
# installation must not look current when this service next starts.
if [ -e "$modem_config/ver_info.txt" ]; then
    mv "$modem_config/ver_info.txt" "$backup/" || exit 1
fi
for entry in "$modem_config"/*; do
    [ -e "$entry" ] || [ -L "$entry" ] || continue
    mv "$entry" "$backup/" || exit 1
done
installing=1
for entry in "$staging"/*; do
    [ "${entry##*/}" = ver_info.txt ] && continue
    mv "$entry" "$modem_config/" || exit 1
done
mv "$staging/ver_info.txt" "$modem_config/" || exit 1
replacing=0

cleanup || exit 1
trap - EXIT HUP INT TERM
setprop ro.vendor.ril.mbn_copy_completed 1
