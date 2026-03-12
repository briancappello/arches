#!/usr/bin/env bash
# shellcheck disable=SC2034

iso_name="arches"
iso_label="ARCHES_$(date +%Y%m)"
iso_publisher="Arches <https://github.com/your-user/arches>"
iso_application="Arches Install/Recovery Media"
iso_version="$(date +%Y.%m.%d)"
install_dir="arch"
buildmodes=('iso')
bootmodes=(
    'bios.syslinux.mbr'
    'bios.syslinux.eltorito'
    'uefi-ia32.grub.esp'
    'uefi-x64.grub.esp'
    'uefi-ia32.grub.eltorito'
    'uefi-x64.grub.eltorito'
)
arch="x86_64"
pacman_conf="pacman.conf"
airootfs_image_type="squashfs"
airootfs_image_tool_options=('-comp' 'zstd' '-Xcompression-level' '15' '-b' '1M')
file_permissions=(
    ["/root"]="0:0:750"
    ["/root/.bash_profile"]="0:0:644"
)
