"""Secure Boot preparation for the shared-ESP install (design D4).

At install time the target gets its own sbctl keys (on the encrypted root),
so mkinitcpio signs every UKI it builds and limine-entry-tool signs the
Limine binary. Nothing is enrolled into the firmware here: that is the
separate, attended ``arches-secureboot-enroll`` step, run on the new system
after its first good boot with Secure Boot off.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from arches_installer.core.disk import MOUNT_ROOT
from arches_installer.core.run import LogCallback, _log, chroot_run

FILES_DIR = Path(__file__).resolve().parent.parent / "files" / "secureboot"

PACKAGES = ["sbctl", "sbsigntools"]

SCRIPTS = [
    "arches-secureboot-resign",
    "arches-secureboot-enroll",
    "arches-freeze-other-systems",
    "arches-tpm-reenroll",
]
HOOK = "zzz-arches-secureboot.hook"


def install_secure_boot_tools(
    root: Path = MOUNT_ROOT, log: LogCallback | None = None
) -> None:
    """Copy the re-sign hook and the attended commands into the target."""
    bin_dir = root / "usr" / "local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in SCRIPTS:
        dest = bin_dir / name
        shutil.copyfile(FILES_DIR / name, dest)
        dest.chmod(0o755)
    hooks = root / "etc" / "pacman.d" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FILES_DIR / HOOK, hooks / HOOK)
    _log(f"Installed {', '.join(SCRIPTS)} and the {HOOK} pacman hook.", log)


def prepare_secure_boot(log: LogCallback | None = None) -> None:
    """Create the target's Secure Boot keys and install its signing tools.

    Must run before the bootloader phase builds the UKIs, so they come
    out signed.
    """
    _log("Creating Secure Boot keys (sbctl create-keys)...", log)
    chroot_run(["sbctl", "create-keys"], log=log)
    install_secure_boot_tools(log=log)
