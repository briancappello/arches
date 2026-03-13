#!/usr/bin/env bash
#
# Build the Arches ISO for a given platform.
#
# Usage:
#   sudo ./scripts/build-iso.sh <platform>
#   sudo ./scripts/build-iso.sh x86-64
#
# Prerequisites:
#   - archiso package installed
#   - CachyOS keyring imported (for x86-64)
#   - AUR repo built (run build-aur-repo.sh first)
#   - Must run as root
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
ISO_PROFILE="$PROJECT_ROOT/iso"
WORK_DIR="/tmp/arches-work"
OUT_DIR="$PROJECT_ROOT/out"

PLATFORM="${1:-}"
if [[ -z "$PLATFORM" ]]; then
    echo "Usage: $0 <platform>"
    echo "Available platforms:"
    ls -1 "$PROJECT_ROOT/platforms/"
    exit 1
fi

PLATFORM_DIR="$PROJECT_ROOT/platforms/$PLATFORM"
if [[ ! -d "$PLATFORM_DIR" ]]; then
    echo "ERROR: Platform not found: $PLATFORM_DIR"
    exit 1
fi

echo "=== Arches ISO Builder ==="
echo "Platform:   $PLATFORM"
echo "Profile:    $ISO_PROFILE"
echo "Work dir:   $WORK_DIR"
echo "Output dir: $OUT_DIR"
echo ""

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Must run as root (sudo)."
    exit 1
fi

# Read platform arch from platform.toml
ARCH=$(grep '^arch' "$PLATFORM_DIR/platform.toml" | head -1 | sed 's/.*= *"\(.*\)"/\1/')
echo "Platform arch: $ARCH"

# Ensure CachyOS keyring is available (x86-64 only)
if [[ "$PLATFORM" == "x86-64" ]]; then
    echo "── Ensuring CachyOS keyring ──"
    if ! pacman-key --list-keys F3B607488DB35A47 &>/dev/null; then
        echo "Importing CachyOS signing key..."
        pacman-key --recv-keys F3B607488DB35A47 --keyserver keyserver.ubuntu.com
        pacman-key --lsign-key F3B607488DB35A47
    fi
fi

# Stage the installer
echo "── Staging arches-installer ──"
INSTALLER_DEST="$ISO_PROFILE/airootfs/opt/arches/installer"
mkdir -p "$INSTALLER_DEST"
cp -r "$PROJECT_ROOT/installer/"* "$INSTALLER_DEST/"

# Stage Ansible playbooks
echo "── Staging Ansible playbooks ──"
ANSIBLE_DEST="$ISO_PROFILE/airootfs/opt/arches/ansible"
mkdir -p "$ANSIBLE_DEST"
cp -r "$PROJECT_ROOT/ansible/"* "$ANSIBLE_DEST/"

# Stage platform config
echo "── Staging platform config ($PLATFORM) ──"
PLATFORM_DEST="$ISO_PROFILE/airootfs/opt/arches/platform"
mkdir -p "$PLATFORM_DEST"
cp "$PLATFORM_DIR/platform.toml" "$PLATFORM_DEST/"
cp "$PLATFORM_DIR/pacman.conf" "$PLATFORM_DEST/"

# Assemble package list (common + platform-specific)
echo "── Assembling package list ──"
cat "$ISO_PROFILE/packages.common" "$PLATFORM_DIR/packages" \
    | grep -v '^#' | grep -v '^$' | sort -u \
    > "$ISO_PROFILE/packages.$ARCH"
echo "  Wrote packages.$ARCH ($(wc -l < "$ISO_PROFILE/packages.$ARCH") packages)"

# Install the platform's pacman.conf as the ISO's pacman.conf
cp "$PLATFORM_DIR/pacman.conf" "$ISO_PROFILE/pacman.conf"

# Create the installer launch script
echo "── Creating installer launch script ──"
mkdir -p "$ISO_PROFILE/airootfs/usr/local/bin"
cat > "$ISO_PROFILE/airootfs/usr/local/bin/arches-install" << 'LAUNCHER'
#!/usr/bin/env bash
# Launch the Arches installer TUI
cd /opt/arches/installer
python -m pip install --quiet --break-system-packages textual 2>/dev/null
exec python -m arches_installer
LAUNCHER
chmod +x "$ISO_PROFILE/airootfs/usr/local/bin/arches-install"

# Clean previous build
echo "── Cleaning previous build ──"
rm -rf "$WORK_DIR"
mkdir -p "$OUT_DIR"

# Build the ISO
echo "── Building ISO ──"
mkarchiso -v -w "$WORK_DIR" -o "$OUT_DIR" "$ISO_PROFILE"

echo ""
echo "=== ISO built successfully ==="
ls -lh "$OUT_DIR"/*.iso 2>/dev/null
