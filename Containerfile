# Arches ISO builder — aarch64
#
# Provides a full Arch Linux ARM environment with all tools needed
# to build the Arches ISO for aarch64 platforms, including:
#   - archiso (mkarchiso)
#   - grub, squashfs-tools
#   - base-devel + git (makepkg, AUR builds)
#   - cmake + KDE/Plasma dev libraries (custom plasmoid builds)
#   - Asahi ALARM keyring (for aarch64-apple builds)
#
# Pacman downloads during image build use BuildKit cache mounts so that
# packages persist across `--rebuild` invocations. At runtime, the host
# .pkg-cache/ directory is bind-mounted for mkarchiso and cache-packages.
#
# Usage (via wrapper):
#   make container-iso-aarch64             # aarch64-generic
#   make container-iso-aarch64-apple       # aarch64-apple (Asahi)
#
# Or directly:
#   podman build -t arches-builder -f Containerfile .
#   podman run --rm --privileged -v .:/build:z arches-builder make iso-aarch64-generic

FROM --platform=linux/arm64 docker.io/lopsided/archlinux:latest

# Pacman's Landlock sandbox doesn't work inside unprivileged containers.
# Disable it before any package operations.
RUN sed -i '/^#\?DownloadUser/d' /etc/pacman.conf && \
    sed -i '/^\[options\]/a DisableSandbox' /etc/pacman.conf

# Disable the package-cleanup hook — it runs `rm -rf /var/cache/pacman/pkg`
# after every transaction, which fails on cache mounts ("device or resource
# busy") and would wipe cached downloads we want to keep.
RUN mkdir -p /etc/pacman.d/hooks && \
    ln -sf /dev/null /etc/pacman.d/hooks/package-cleanup.hook

# Phase 1: Add Asahi ALARM repo with relaxed sigs so we can bootstrap
# the keyring. The aarch64-apple platform needs this repo for linux-asahi,
# asahi-fwextract, etc. We install the keyring first, then tighten sigs.
RUN sed -i '/^\[core\]/i \
[asahi-alarm]\n\
SigLevel = Optional TrustAll\n\
Server = https://github.com/asahi-alarm/asahi-alarm/releases/download/$arch\n' /etc/pacman.conf

RUN --mount=type=cache,target=/var/cache/pacman/pkg,sharing=locked \
    pacman-key --init && \
    pacman-key --populate archlinuxarm && \
    pacman -Sy --noconfirm && \
    pacman -S --noconfirm asahi-alarm-keyring && \
    pacman-key --populate asahi-alarm

# Phase 2: Tighten Asahi repo signature level now that the keyring is trusted.
RUN sed -i '/^\[asahi-alarm\]/,/^$/{s/SigLevel = Optional TrustAll/SigLevel = Required/}' /etc/pacman.conf

# Phase 3: Install all build dependencies up front.
# Pre-installing the KDE/Plasma dev libraries avoids makepkg -s pulling
# them on every build (hundreds of MBs of KF6/Qt6 devel packages).
#
# The cache mount persists across rebuilds — packages already downloaded
# are reused. Do NOT run `pacman -Scc` — it would wipe the cache.
#
# Packages from ALARM repos:
#   ISO build deps:       grub, squashfs-tools, erofs-utils, libisoburn, mtools,
#                         dosfstools, e2fsprogs, arch-install-scripts
#   Package building:     base-devel, git
#   Plasmoid build deps:  cmake, extra-cmake-modules, qt6-base, qt6-declarative
#   arches-taskmanager:   kconfig, ki18n, kio, knotifications, kservice,
#                         kwindowsystem, plasma-activities, plasma-activities-stats,
#                         libplasma, libksysguard, kitemmodels, plasma-workspace,
#                         plasma-desktop
#   plasma-ai-usage:      kwallet
RUN --mount=type=cache,target=/var/cache/pacman/pkg,sharing=locked \
    pacman -Syu --noconfirm && \
    pacman -S --noconfirm \
        grub squashfs-tools erofs-utils libisoburn mtools \
        dosfstools e2fsprogs arch-install-scripts base-devel git \
        cmake extra-cmake-modules qt6-base qt6-declarative \
        kconfig ki18n kio knotifications kservice kwindowsystem \
        plasma-activities plasma-activities-stats libplasma libksysguard \
        kitemmodels plasma-workspace plasma-desktop kwallet

# archiso is not packaged for Arch Linux ARM. It's an arch-independent
# package (shell scripts) — install the latest version from upstream Arch.
RUN --mount=type=cache,target=/var/cache/pacman/pkg,sharing=locked \
    ARCHISO_URL=$(curl -sL 'https://geo.mirror.pkgbuild.com/extra/os/x86_64/' \
        | grep -oP 'archiso-[0-9]+-[0-9]+-any\.pkg\.tar\.zst(?=")' \
        | sort -V | tail -1) && \
    echo "Installing archiso: $ARCHISO_URL" && \
    pacman -U --noconfirm \
        "https://geo.mirror.pkgbuild.com/extra/os/x86_64/${ARCHISO_URL}"

# Patch mkarchiso: filter out GRUB modules that don't exist for the target arch.
# Upstream archiso hardcodes an x86-centric module list (at_keyboard, usb*,
# keylayouts) that aren't available on arm64-efi.
COPY scripts/patch-mkarchiso-grub-modules.sh /tmp/
RUN bash /tmp/patch-mkarchiso-grub-modules.sh && rm /tmp/patch-mkarchiso-grub-modules.sh

# Create a non-root build user for makepkg (which refuses to run as root).
# The Makefile / build-aur-repo.sh handle privilege dropping via SUDO_USER.
RUN useradd -m builder && \
    echo "builder ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers

WORKDIR /build
