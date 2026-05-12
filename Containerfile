# Arches ISO builder — multi-arch (x86-64 + aarch64)
#
# Provides a full Arch Linux environment with all tools needed to build
# the Arches ISO for any supported platform:
#   - archiso (mkarchiso)
#   - grub, squashfs-tools, mtools
#   - base-devel + git (makepkg, AUR builds)
#   - cmake + KDE/Plasma dev libraries (custom plasmoid builds)
#
# Architecture-specific setup:
#   x86-64:  Official Arch Linux + CachyOS repos
#   aarch64: Arch Linux ARM + Asahi ALARM repos
#
# Pacman downloads during image build use BuildKit cache mounts so that
# packages persist across `--rebuild` invocations. At runtime, the host
# .pkg-cache/ directory is bind-mounted for mkarchiso and cache-packages.
#
# Usage (via build-iso.sh):
#   make iso                      # auto-detect platform
#   make usb                      # build + write USB
#   PLATFORM=x86-64 make iso      # explicit platform
#
# Or directly:
#   podman build --build-arg BASE_IMAGE=docker.io/archlinux:latest \
#                --build-arg TARGETARCH=amd64 -t arches-builder .
#   podman build --build-arg BASE_IMAGE=docker.io/lopsided/archlinux:latest \
#                --build-arg TARGETARCH=arm64 -t arches-builder .

# Build args: set by build-iso.sh based on detected platform
ARG BASE_IMAGE=docker.io/archlinux:latest
FROM ${BASE_IMAGE}

ARG TARGETARCH=amd64

# ─── Common: disable sandbox + cleanup hook ───────────
# Pacman's Landlock sandbox doesn't work inside unprivileged containers.
# The package-cleanup hook wipes the cache mount and fails on busy mounts.
RUN sed -i '/^#\?DownloadUser/d' /etc/pacman.conf && \
    sed -i '/^\[options\]/a DisableSandbox' /etc/pacman.conf && \
    mkdir -p /etc/pacman.d/hooks && \
    ln -sf /dev/null /etc/pacman.d/hooks/package-cleanup.hook

# ─── aarch64: Asahi ALARM repo bootstrap ─────────────
# The aarch64-apple platform needs this repo for linux-asahi,
# asahi-fwextract, etc. Add with relaxed sigs to bootstrap the keyring,
# then tighten after populating.
RUN if [ "$TARGETARCH" = "arm64" ]; then \
        sed -i '/^\[core\]/i \
[asahi-alarm]\n\
SigLevel = Optional TrustAll\n\
Server = https://github.com/asahi-alarm/asahi-alarm/releases/download/$arch\n' /etc/pacman.conf; \
        pacman-key --init && \
        pacman-key --populate archlinuxarm && \
        pacman -Sy --noconfirm && \
        pacman -S --noconfirm asahi-alarm-keyring && \
        pacman-key --populate asahi-alarm && \
        sed -i '/^\[asahi-alarm\]/,/^$/{s/SigLevel = Optional TrustAll/SigLevel = Required/}' /etc/pacman.conf; \
    else \
        pacman-key --init && \
        pacman-key --populate archlinux; \
    fi

# ─── x86-64: CachyOS repos ──────────────────────────
# The build container only needs the base [cachyos] repo (x86_64 baseline
# packages). Tier-specific repos (v3/v4/znver4) are NOT added here — they
# live in the platform's pacman.conf, which mkarchiso uses when building
# the ISO rootfs. This separation means a v3 host can build a v4 ISO.
#
# We do install CachyOS's patched pacman because it recognizes the
# non-standard architectures (x86_64_v3, etc.) that tier-specific
# packages use. Without it, mkarchiso's pacstrap would reject them.
RUN --mount=type=cache,target=/var/cache/pacman/pkg,sharing=locked \
    if [ "$TARGETARCH" = "amd64" ]; then \
        # Bootstrap CachyOS signing key. We import it manually rather than \
        # pinning a specific cachyos-keyring package URL because pinned \
        # filenames break whenever CachyOS bumps the keyring/mirrorlist \
        # packages (they prune old versions from the mirror). \
        pacman-key --recv-keys F3B607488DB35A47 --keyserver keyserver.ubuntu.com && \
        pacman-key --lsign-key F3B607488DB35A47 && \
        # Add [cachyos] repo with a direct Server= (mirrorlist file doesn't \
        # exist yet) and Optional TrustAll so we can sync before the keyring \
        # package is installed. We tighten SigLevel below after install. \
        # NOTE: sed's `i\` insert is written on a SINGLE line using \n \
        # escapes only. Do NOT use shell line continuations (\<NL>) inside \
        # the replacement text — sed's `i\` treats the literal newline as \
        # additional inserted content, producing spurious blank lines that \
        # break the section header parser. \
        sed -i '/^\[core\]/i [cachyos]\nSigLevel = Optional TrustAll\nServer = https://mirror.cachyos.org/repo/$arch/$repo\n' /etc/pacman.conf && \
        pacman -Sy --noconfirm && \
        # Install latest versions by name — never pin to specific filenames. \
        pacman -S --noconfirm \
            cachyos-keyring \
            cachyos-mirrorlist \
            cachyos-v3-mirrorlist \
            cachyos-v4-mirrorlist && \
        pacman-key --populate cachyos && \
        # Swap to mirrorlist-based config + Required signatures now that \
        # both the keyring and mirrorlist files are installed. Done in two \
        # steps (delete the temporary block, insert the final one) so we \
        # never rely on sed's multi-line `c\` semantics. \
        sed -i '/^\[cachyos\]/,/^$/d' /etc/pacman.conf && \
        sed -i '/^\[core\]/i [cachyos]\nInclude = /etc/pacman.d/cachyos-mirrorlist\n' /etc/pacman.conf && \
        pacman -Sy --noconfirm && \
        # Install CachyOS's patched pacman (recognizes x86_64_v3/v4 archs). \
        pacman -S --noconfirm pacman; \
    fi

# ─── Common: install all build dependencies ──────────
# Pre-installing KDE/Plasma dev libs avoids makepkg pulling them on every
# build (hundreds of MBs of KF6/Qt6 devel packages).
#
# The cache mount persists across rebuilds — packages already downloaded
# are reused. Do NOT run `pacman -Scc` — it would wipe the cache.
RUN --mount=type=cache,target=/var/cache/pacman/pkg,sharing=locked \
    pacman -Syu --noconfirm && \
    pacman -S --noconfirm \
        grub squashfs-tools erofs-utils libisoburn mtools rsync \
        dosfstools e2fsprogs arch-install-scripts base-devel git \
        cmake extra-cmake-modules qt6-base qt6-declarative \
        kconfig ki18n kio knotifications kservice kwindowsystem \
        plasma-activities plasma-activities-stats libplasma libksysguard \
        kitemmodels plasma-workspace plasma-desktop kwallet

# ─── llama.cpp build deps (x86-64 only) ──────────────
# Pre-installing the Vulkan + ROCm/HIP toolchains avoids re-downloading
# ~3-4 GB of ROCm libraries (rocm-llvm, hipblas, rocblas, rocwmma, ...)
# on every clean rebuild of modules/llama-cpp/build.sh. Same pattern as
# KDE devel libs above. Aarch64 doesn't have ROCm, and we don't expect
# to build llama.cpp for those platforms via this container.
#
# Keep this list in sync with modules/llama-cpp/build.sh `makedepends`.
RUN --mount=type=cache,target=/var/cache/pacman/pkg,sharing=locked \
    if [ "$TARGETARCH" = "amd64" ]; then \
        pacman -S --noconfirm \
            ninja openssl openblas \
            vulkan-headers vulkan-icd-loader shaderc spirv-headers \
            rocm-hip-sdk rocm-cmake rocwmma; \
    fi

# ─── archiso ─────────────────────────────────────────
# On x86-64, archiso is in the repos. On aarch64 (ALARM), it's not
# packaged — install the latest from upstream Arch (arch-independent
# shell scripts).
RUN --mount=type=cache,target=/var/cache/pacman/pkg,sharing=locked \
    if [ "$TARGETARCH" = "arm64" ]; then \
        ARCHISO_URL=$(curl -sL 'https://geo.mirror.pkgbuild.com/extra/os/x86_64/' \
            | grep -oP 'archiso-[0-9]+-[0-9]+-any\.pkg\.tar\.zst(?=")' \
            | sort -V | tail -1) && \
        echo "Installing archiso: $ARCHISO_URL" && \
        pacman -U --noconfirm \
            "https://geo.mirror.pkgbuild.com/extra/os/x86_64/${ARCHISO_URL}"; \
    else \
        pacman -S --noconfirm archiso; \
    fi

# ─── aarch64: patch mkarchiso GRUB modules ──────────
# Upstream archiso hardcodes x86-centric GRUB modules (at_keyboard, usb*,
# keylayouts) that don't exist for arm64-efi. This patch filters them.
COPY scripts/patch-mkarchiso-grub-modules.sh /tmp/
RUN if [ "$TARGETARCH" = "arm64" ]; then \
        bash /tmp/patch-mkarchiso-grub-modules.sh; \
    fi && \
    rm -f /tmp/patch-mkarchiso-grub-modules.sh

# ─── Non-root build user ────────────────────────────
# makepkg refuses to run as root. The Makefile / build-aur-repo.sh
# handle privilege dropping via SUDO_USER.
RUN useradd -m builder && \
    echo "builder ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers

WORKDIR /build
