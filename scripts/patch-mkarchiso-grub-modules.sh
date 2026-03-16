#!/bin/bash
# Patch mkarchiso to filter GRUB modules by what's actually available
# for the target architecture. Upstream archiso hardcodes an x86-centric
# module list that includes at_keyboard, keylayouts, usb*, etc. which
# don't exist for arm64-efi.
#
# This inserts a line before grub-mkstandalone that filters grubmodules
# to only those with a matching .mod file.

set -euo pipefail

MKARCHISO="/usr/sbin/mkarchiso"

if ! grep -q 'grub-mkstandalone' "$MKARCHISO"; then
    echo "ERROR: grub-mkstandalone not found in $MKARCHISO"
    exit 1
fi

# Insert the filter line before the first grub-mkstandalone call.
# The inserted bash line re-assigns grubmodules to only those modules
# whose .mod file exists under /usr/lib/grub/$grub_target/.
FILTER_LINE='    grubmodules=($(for m in "${grubmodules[@]}"; do [ -f "/usr/lib/grub/${grub_target}/${m}.mod" ] && echo "$m"; done))'

sed -i "/grub-mkstandalone -O/i\\
${FILTER_LINE}" "$MKARCHISO"

if grep -q 'for m in "${grubmodules' "$MKARCHISO"; then
    echo "mkarchiso patched: GRUB module filtering added"
else
    echo "ERROR: patch verification failed"
    exit 1
fi
