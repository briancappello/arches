"""LUKS2 encryption for existing-partition installs (design D2).

One LUKS2 (argon2id) device sits on the target volume. It has exactly two
key slots when the install ends:

- a TPM2 slot bound to PCR 7 that also needs a PIN;
- a recovery key, generated here, handed once to ``on_recovery_key``, and
  never logged or written to disk.

A random key in ``/run`` (tmpfs on the live ISO) formats the device and
enrolls both slots, then its slot is wiped and the file is removed.

The target unlocks in the initramfs (``systemd`` + ``sd-encrypt`` hooks)
from ``/etc/crypttab.initramfs``.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from arches_installer.core.disk import MOUNT_ROOT, EncryptedVolume
from arches_installer.core.run import LogCallback, _log
from arches_installer.core.run import run as logged_run

# PCR 7: Secure Boot state and the keys that verified the boot chain.
TPM2_PCRS = "7"

# systemd-cryptenroll prints the recovery key on stdout as eight groups of
# eight "modhex" characters.
_RECOVERY_KEY_RE = re.compile(r"^[cbdefghijklnrtuv]{8}(-[cbdefghijklnrtuv]{8}){7}$")

MIN_PIN_LENGTH = 4

# Where the temporary formatting key lives. /run is tmpfs on the live ISO.
KEYFILE_DIR = Path("/run")


class EncryptionError(RuntimeError):
    """Encryption could not be set up."""


@dataclass
class EncryptionSecrets:
    """What the operator supplies for an encrypted install.

    ``pin`` is the TPM2 PIN. ``on_recovery_key`` receives the recovery key
    exactly once; it must show it to the operator and must not store it.
    """

    pin: str
    on_recovery_key: Callable[[str], None]

    def __repr__(self) -> str:  # never print the PIN
        return "EncryptionSecrets(pin=<hidden>)"


def validate_pin(pin: str) -> None:
    if len(pin) < MIN_PIN_LENGTH:
        raise EncryptionError(
            f"The TPM PIN needs at least {MIN_PIN_LENGTH} characters."
        )


def tpm2_present(sysfs: Path = Path("/sys/class/tpm")) -> bool:
    """True if a TPM 2.0 is visible to the kernel."""
    for tpm in sysfs.glob("tpm*"):
        major = tpm / "tpm_version_major"
        try:
            if major.read_text().strip() == "2":
                return True
        except OSError:
            continue
    return False


def _quiet(
    cmd: list[str], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Run a command whose output may hold a secret. Nothing is logged."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=env,
    )


def create_luks_volume(
    device: str,
    name: str,
    secrets_: EncryptionSecrets,
    log: LogCallback | None = None,
) -> EncryptedVolume:
    """Format *device* as LUKS2, enroll TPM2+PIN and a recovery key, open it.

    Returns the opened volume (``/dev/mapper/<name>``).
    """
    validate_pin(secrets_.pin)
    _log(f"  Encrypting {device} (LUKS2, argon2id) as {name}...", log)

    fd, keyfile = tempfile.mkstemp(prefix="arches-luks-", dir=KEYFILE_DIR)
    try:
        os.write(fd, secrets.token_bytes(64))
        os.close(fd)
        unlock = f"--unlock-key-file={keyfile}"

        logged_run(
            [
                "cryptsetup",
                "luksFormat",
                "--type",
                "luks2",
                "--pbkdf",
                "argon2id",
                "--batch-mode",
                "--label",
                name,
                "--key-file",
                keyfile,
                device,
            ],
            log=log,
        )
        logged_run(["cryptsetup", "open", "--key-file", keyfile, device, name], log=log)

        result = _quiet(["systemd-cryptenroll", unlock, "--recovery-key", device])
        key = result.stdout.strip()
        if result.returncode != 0 or not _RECOVERY_KEY_RE.match(key):
            raise EncryptionError(
                f"systemd-cryptenroll --recovery-key failed ({result.returncode})"
            )
        _log("  Recovery key enrolled. It is shown once and is not logged.", log)
        secrets_.on_recovery_key(key)
        del key

        env = dict(os.environ, NEWPIN=secrets_.pin)
        result = _quiet(
            [
                "systemd-cryptenroll",
                unlock,
                "--tpm2-device=auto",
                f"--tpm2-pcrs={TPM2_PCRS}",
                "--tpm2-with-pin=yes",
                device,
            ],
            env=env,
        )
        if result.returncode != 0:
            raise EncryptionError(
                f"TPM2 enrollment failed ({result.returncode}): {result.stderr.strip()}"
            )
        _log(f"  TPM2 + PIN enrolled (PCR {TPM2_PCRS}).", log)

        # The formatting key must not survive the install.
        logged_run(["systemd-cryptenroll", unlock, "--wipe-slot=0", device], log=log)
    finally:
        try:
            size = os.path.getsize(keyfile)
            with open(keyfile, "r+b") as f:
                f.write(b"\0" * size)
            os.unlink(keyfile)
        except OSError:
            pass

    logged_run(["systemd-cryptenroll", device], log=log)
    uuid = subprocess.run(
        ["cryptsetup", "luksUUID", device],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return EncryptedVolume(name=name, luks_device=device, luks_uuid=uuid)


# ---------------------------------------------------------------------------
# Target configuration
# ---------------------------------------------------------------------------


def crypttab_initramfs(volumes: list[EncryptedVolume]) -> str:
    lines = [
        "# <name> <device> <password> <options>  (written by the Arches installer)"
    ]
    for v in volumes:
        lines.append(f"{v.name} UUID={v.luks_uuid} none tpm2-device=auto")
    return "\n".join(lines) + "\n"


def write_crypttab_initramfs(
    volumes: list[EncryptedVolume],
    root: Path = MOUNT_ROOT,
    log: LogCallback | None = None,
) -> None:
    path = root / "etc" / "crypttab.initramfs"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(crypttab_initramfs(volumes))
    path.chmod(0o600)
    _log("Wrote /etc/crypttab.initramfs", log)


def edit_hooks(hooks: list[str], *, mdadm: bool) -> list[str]:
    """Return *hooks* converted to a systemd initramfs that unlocks LUKS.

    Busybox-only hooks are replaced by their systemd counterparts,
    ``keyboard`` and ``autodetect`` follow ``systemd`` (in that order), and
    ``mdadm_udev`` (if *mdadm*) and ``sd-encrypt`` go before ``filesystems``.
    """
    out: list[str] = []
    for h in hooks:
        h = {
            "udev": "systemd",
            "keymap": "sd-vconsole",
            "encrypt": "sd-encrypt",
            "mdadm": "mdadm_udev",
        }.get(h, h)
        if h in ("consolefont", "usr", "resume"):
            continue
        if h not in out:
            out.append(h)

    def insert_before_filesystems(hook: str) -> None:
        if hook in out:
            return
        if "filesystems" in out:
            out.insert(out.index("filesystems"), hook)
        else:
            out.append(hook)

    if "systemd" not in out:
        out.insert(out.index("base") + 1 if "base" in out else 0, "systemd")
    # autodetect keeps the UKIs small enough for an ESP shared with another
    # system (a QEMU UKI: 229 MB without, 41 MB with). keyboard goes before
    # it so every keyboard driver stays in, for the PIN prompt.
    for h in ("keyboard", "autodetect"):
        if h in out:
            out.remove(h)
    at = out.index("systemd") + 1
    out[at:at] = ["keyboard", "autodetect"]
    if "sd-vconsole" not in out:
        if "keyboard" in out:
            out.insert(out.index("keyboard") + 1, "sd-vconsole")
        else:
            insert_before_filesystems("keyboard")
            out.insert(out.index("keyboard") + 1, "sd-vconsole")
    insert_before_filesystems("block")
    if mdadm:
        insert_before_filesystems("mdadm_udev")
    insert_before_filesystems("sd-encrypt")
    # sd-encrypt needs the block device first.
    for hook in (["mdadm_udev"] if mdadm else []) + ["sd-encrypt"]:
        if out.index(hook) < out.index("block"):
            out.remove(hook)
            out.insert(out.index("block") + 1, hook)
    return out


_HOOKS_RE = re.compile(r"^HOOKS=\(([^)]*)\)", re.MULTILINE)


def configure_mkinitcpio_hooks(
    root: Path = MOUNT_ROOT,
    *,
    mdadm: bool,
    log: LogCallback | None = None,
) -> Path:
    """Edit the HOOKS line that mkinitcpio actually uses, in place.

    The last file to assign HOOKS wins: ``mkinitcpio.conf``, then the
    drop-ins in lexical order. Editing that line (instead of overriding it)
    keeps changes other tools made, such as chwd removing ``kms``.
    """
    etc = root / "etc"
    candidates = [etc / "mkinitcpio.conf"] + sorted(
        (etc / "mkinitcpio.conf.d").glob("*.conf")
    )
    owner: Path | None = None
    for path in candidates:
        if path.is_file() and _HOOKS_RE.search(path.read_text()):
            owner = path
    if owner is None:
        raise EncryptionError(
            "No HOOKS=(...) line found in the target's mkinitcpio config."
        )

    text = owner.read_text()
    matches = list(_HOOKS_RE.finditer(text))
    last = matches[-1]
    new = edit_hooks(last.group(1).split(), mdadm=mdadm)
    text = text[: last.start()] + f"HOOKS=({' '.join(new)})" + text[last.end() :]
    owner.write_text(text)
    _log(f"Set mkinitcpio HOOKS in {owner.relative_to(root)}: {' '.join(new)}", log)
    return owner


def write_mdadm_conf(
    md_devices: list[str],
    root: Path = MOUNT_ROOT,
    log: LogCallback | None = None,
) -> None:
    """Record the new arrays in the target's /etc/mdadm.conf."""
    if not md_devices:
        return
    scan = subprocess.run(
        ["mdadm", "--detail", "--scan"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    arrays = [
        line
        for line in scan.splitlines()
        if line.startswith("ARRAY ") and line.split()[1] in md_devices
    ]
    if len(arrays) != len(md_devices):
        raise EncryptionError(
            f"mdadm --detail --scan did not list {', '.join(md_devices)}:\n{scan}"
        )
    conf = root / "etc" / "mdadm.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    existing = conf.read_text() if conf.exists() else ""
    with conf.open("a") as f:
        for line in arrays:
            if line not in existing:
                f.write(line + "\n")
    _log(f"Recorded {', '.join(md_devices)} in /etc/mdadm.conf", log)
