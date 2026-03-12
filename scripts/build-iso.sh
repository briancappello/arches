#!/usr/bin/env bash
#
# Build the Arches ISO.
#
# Prerequisites:
#   - archiso package installed
#   - CachyOS keyring imported
#   - AUR repo built (run build-aur-repo.sh first)
#   - Must run as root
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
ISO_PROFILE="$PROJECT_ROOT/iso"
WORK_DIR="/tmp/arches-work"
OUT_DIR="$PROJECT_ROOT/out"

echo "=== Arches ISO Builder ==="
echo "Profile:    $ISO_PROFILE"
echo "Work dir:   $WORK_DIR"
echo "Output dir: $OUT_DIR"
echo ""

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Must run as root (sudo)."
    exit 1
fi

# Ensure CachyOS keyring is available in the build environment
echo "── Ensuring CachyOS keyring ──"
if ! pacman-key --list-keys F3B607488DB35A47 &>/dev/null; then
    echo "Importing CachyOS signing key..."
    pacman-key --recv-keys F3B607488DB35A47 --keyserver keyserver.ubuntu.com
    pacman-key --lsign-key F3B607488DB35A47
fi

# Install the installer into airootfs
echo "── Installing arches-installer into ISO ──"
INSTALLER_DEST="$ISO_PROFILE/airootfs/opt/arches/installer"
mkdir -p "$INSTALLER_DEST"
cp -r "$PROJECT_ROOT/installer/"* "$INSTALLER_DEST/"

# Copy ansible playbooks into ISO
echo "── Copying Ansible playbooks into ISO ──"
ANSIBLE_DEST="$ISO_PROFILE/airootfs/opt/arches/ansible"
mkdir -p "$ANSIBLE_DEST"
cp -r "$PROJECT_ROOT/ansible/"* "$ANSIBLE_DEST/"

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
