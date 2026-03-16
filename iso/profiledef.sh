#!/usr/bin/env bash
# shellcheck disable=SC2034

# Arches ISO profile definition.
#
# The arch-specific values (arch, bootmodes) are set dynamically
# based on the platform. The Makefile's assemble-packages target
# generates the correct packages.<arch> file and pacman.conf.
#
# For manual builds, override ARCHES_ARCH before sourcing:
#   ARCHES_ARCH=aarch64 mkarchiso ...

iso_name="arches"
iso_label="ARCHES_$(date +%Y%m)"
iso_publisher="Arches <https://github.com/your-user/arches>"
iso_application="Arches Install/Recovery Media"
iso_version="$(date +%Y.%m.%d)"
install_dir="arch"
buildmodes=('iso')

# Default to x86_64 if not set by build system
arch="${ARCHES_ARCH:-x86_64}"

# Boot modes depend on architecture
if [[ "$arch" == "x86_64" ]]; then
    bootmodes=(
        'bios.syslinux'
        'uefi.grub'
    )
elif [[ "$arch" == "aarch64" ]]; then
    bootmodes=(
        'uefi.grub'
    )
fi

pacman_conf="pacman.conf"
airootfs_image_type="squashfs"

# The ALARM linux-aarch64 kernel lacks CONFIG_SQUASHFS_ZSTD; use xz instead.
if [[ "$arch" == "aarch64" ]]; then
    airootfs_image_tool_options=('-comp' 'xz' '-b' '1M')
else
    airootfs_image_tool_options=('-comp' 'zstd' '-Xcompression-level' '15' '-b' '1M')
fi
file_permissions=(
    ["/etc/shadow"]="0:0:400"
    ["/root"]="0:0:750"
    ["/root/.bash_profile"]="0:0:644"
    ["/usr/local/bin/arches-install"]="0:0:755"
)
