"""Core install logic — pacstrap, genfstab, chroot configuration."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from arches_installer.core.disk import MOUNT_ROOT
from arches_installer.core.network import copy_network_profiles
from arches_installer.core.platform import PlatformConfig, load_platform_from_iso
from arches_installer.core.provider import (
    ProviderConfig,
    ProviderInputs,
    provider_id_for_platform,
    resolve_provider,
    validate_provider_inputs,
)
from arches_installer.core.run import LogCallback, _log, chroot_run, run
from arches_installer.core.template import InstallTemplate

# Type import only — avoid circular dependency at runtime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arches_installer.core.hardware import HardwareConfig

# Pre-downloaded package cache baked into the ISO
ISO_PKG_CACHE = Path("/opt/arches/pkg-cache")

# Ansible playbooks shipped on the ISO
ISO_ANSIBLE_DIR = Path("/opt/arches/ansible")

# Build host SSH public key (optional, embedded at ISO build time)
ISO_BUILD_HOST_PUBKEY = Path("/opt/arches/build-host.pub")


@dataclass
class PackageTransaction:
    provider: ProviderConfig
    root: Path
    work: Path
    cache: Path
    sync_source: Path
    keyring_source: Path
    legacy: bool
    final_conf: str
    mounted: bool = False

    @property
    def config(self) -> Path:
        return self.work / "pacman.conf"

    @property
    def dbpath(self) -> Path:
        return self.root / "var/lib/pacman"

    @property
    def gpgdir(self) -> Path:
        return self.root / "etc/pacman.d/gnupg"

    @property
    def pacman_args(self) -> list[str]:
        return [
            "--config",
            str(self.config),
            "--root",
            str(self.root),
            "--dbpath",
            str(self.dbpath),
            "--gpgdir",
            str(self.gpgdir),
        ]


@contextmanager
def package_transaction(platform, *, provider=None, inputs=None, log=None, root=None):
    """Post-mount package state. This is not destructive-install preflight."""
    provider = provider or resolve_provider(platform)
    provider_id_for_platform(platform.arch, platform.name, provider.id)
    legacy = inputs is None and provider.id in ("cachyos", "alarm", "asahi")
    if inputs is None and not legacy:
        raise ValueError("Explicit isolated provider inputs are required")
    files = {}
    if legacy:
        if (ISO_PKG_CACHE / "manifest.json").exists():
            raise ValueError(
                "An identity-bearing cache requires explicit authenticated inputs"
            )
        live = load_platform_from_iso()
        live_provider = resolve_provider(live)
        if (
            live.arch != platform.arch
            or live_provider.id != provider.id
            or live_provider.pacman_conf != provider.pacman_conf
        ):
            raise ValueError("Legacy default cache does not match selected provider")
    else:
        files = validate_provider_inputs(provider, platform.arch, inputs)
    root = MOUNT_ROOT if root is None else root
    if root == Path("/") or not root.is_dir():
        raise ValueError("A separate mounted installation root is required")
    for relative in (
        "etc/pacman.conf",
        "etc/pacman.d/gnupg",
        "var/lib/pacman",
        "var/cache/pacman/pkg",
        "var/log/pacman.log",
        "mnt/arches-pkg-cache",
    ):
        path = root / relative
        if path.resolve() != path:
            raise ValueError(f"Symlink in target package path: {path}")
    if not legacy:
        for relative in (
            "var/lib/pacman",
            "etc/pacman.d/gnupg",
            "var/cache/pacman/pkg",
        ):
            path = root / relative
            if path.exists() and any(path.iterdir()):
                raise ValueError(
                    f"Alternative provider requires empty target package state: {path}"
                )
    with tempfile.TemporaryDirectory(prefix=".arches-packages-", dir=root) as directory:
        work = Path(directory)
        source = ISO_PKG_CACHE if legacy else inputs.root
        tx = PackageTransaction(
            provider,
            root,
            work,
            source if legacy else source / "packages",
            source / "sync",
            Path("/etc/pacman.d/gnupg") if legacy else work / "keyrings",
            legacy,
            provider.pacman_conf,
        )
        try:
            if not legacy:
                for name in files:
                    if not name.startswith(("packages/", "omarchy/")):
                        dest = work / name
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source / name, dest)
                tx.sync_source = work / "sync"
                if "Include = /etc/pacman.d/mirrorlist" in tx.final_conf:
                    mirrorlist = (work / "mirrorlist").read_text()
                    active = [
                        line.strip()
                        for line in mirrorlist.splitlines()
                        if line.strip() and not line.lstrip().startswith("#")
                    ]
                    if not active or any(
                        not re.fullmatch(r"Server = https://[^\s/]+/\S+", line)
                        for line in active
                    ):
                        raise ValueError("Invalid isolated Arch mirrorlist")
                    tx.final_conf = tx.final_conf.replace(
                        "Include = /etc/pacman.d/mirrorlist", mirrorlist
                    )
            _make_pacman_conf_with_cache(tx)
            _preseed_pacman_databases(
                log, source=tx.sync_source, target=tx.dbpath / "sync"
            )
            if legacy:
                _wait_for_keyring(log)
            _preseed_pacman_keyring(log, transaction=tx)
            yield tx
        finally:
            _unmount_iso_cache_from_target(log, transaction=tx)


def _setup_local_repo_mirror(*, sync_dir: Path, mirror_dir: Path) -> Path | None:
    """Create a file mirror from selected databases, never live pacman state.

    pacstrap always runs ``pacman -Sy`` which downloads database files from
    mirrors.  When offline, this fails.  By creating a local repo structure
    with the pre-synced database files and adding ``file://`` Server lines
    *before* the remote mirrors in each repo section, pacman's ``-Sy``
    succeeds offline (it finds the database at the file:// URL and stops).

    Returns the path to the local mirror directory, or None if not available.
    """
    if not sync_dir.exists() or not any(sync_dir.glob("*.db")):
        return None

    # For each .db file, create a repo directory structure:
    #   <mirror_dir>/<reponame>/<reponame>.db
    for db_file in sync_dir.glob("*.db"):
        repo_name = db_file.name.replace(".db", "")
        repo_dir = mirror_dir / repo_name
        repo_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db_file, repo_dir / db_file.name)
        signature = db_file.with_suffix(".db.sig")
        if signature.is_file():
            shutil.copy2(signature, repo_dir / signature.name)

    return mirror_dir


def _make_pacman_conf_with_cache(transaction: PackageTransaction) -> Path:
    """Create a pacman.conf with cache dirs and local mirror for offline/online use.

    Always returns a custom config with CacheDir pointing at writable
    storage.  The live ISO's root overlay (cowspace) is typically only
    256 MB — far too small for the ~1-4 GB of packages pacstrap
    downloads.  By directing the cache to the target filesystem (which
    is already mounted at MOUNT_ROOT) we avoid filling the overlay.

    Only selected cache candidates are visible. Downloads use the target disk.
    """
    cache = transaction.cache
    has_offline_cache = cache.exists() and any(cache.glob("*.pkg.tar.*"))

    conf_text = transaction.final_conf
    # No inherited state or host overlay fallback, even when -c is used.
    conf_text = re.sub(
        r"^\s*(?:RootDir|DBPath|GPGDir|CacheDir|LogFile)\s*=.*\n?",
        "",
        conf_text,
        flags=re.MULTILINE,
    )

    # Build CacheDir lines.  Order matters — pacman searches in order
    # and writes downloads to the FIRST writable CacheDir.
    #
    # 1. Target filesystem cache (writable, plenty of space)
    # 2. Selected artifact cache (source only)
    target_cache = transaction.root / "var/cache/pacman/pkg"
    target_cache.mkdir(parents=True, exist_ok=True)

    cache_lines = f"CacheDir = {target_cache}/\n"
    if has_offline_cache:
        cache_lines += f"CacheDir = {cache}/\n"
    (transaction.root / "var/log").mkdir(parents=True, exist_ok=True)
    cache_lines += f"DBPath = {transaction.dbpath}\nGPGDir = {transaction.gpgdir}\nLogFile = {transaction.root}/var/log/pacman.log\n"

    conf_text = conf_text.replace(
        "[options]\n",
        f"[options]\n{cache_lines}",
        1,
    )

    # Create a local file:// mirror from the selected snapshot.
    # Insert a Server = file:// line at the top of each repo section
    # so pacman's -Sy finds the database locally before trying mirrors.
    mirror_dir = _setup_local_repo_mirror(
        sync_dir=transaction.sync_source, mirror_dir=transaction.work / "mirror"
    )
    if not transaction.legacy:
        # A pinned database set cannot fall through to mutable online databases.
        conf_text = re.sub(
            r"^\s*(?:Server|Include)\s*=.*\n?", "", conf_text, flags=re.MULTILINE
        )
    if mirror_dir:

        def _add_local_server(match: re.Match) -> str:
            repo_name = match.group(1)
            # Skip [options] and local repos that already have file:// servers
            if repo_name in ("options", "arches-local"):
                return match.group(0)
            local_repo = mirror_dir / repo_name
            if local_repo.exists():
                return f"[{repo_name}]\nServer = file://{local_repo}\n"
            return match.group(0)

        conf_text = re.sub(
            r"^\[([a-zA-Z0-9_-]+)\]\s*$",
            _add_local_server,
            conf_text,
            flags=re.MULTILINE,
        )

    transaction.config.write_text(conf_text)
    return transaction.config


def _mount_iso_cache_in_target(
    target_pacman_conf: Path,
    log: LogCallback | None = None,
    *,
    transaction: PackageTransaction,
) -> None:
    """Bind-mount the ISO package cache into the target and register it as a CacheDir.

    This allows chroot pacman operations (override packages, hardware
    detection) to find packages locally without downloading.  The mount
    is inside the chroot at ``/mnt/arches-pkg-cache``, and the target's
    pacman.conf gets an extra ``CacheDir`` pointing there.
    """
    cache = transaction.cache
    if not cache.exists() or not any(cache.glob("*.pkg.tar.*")):
        return

    mount_point = transaction.root / "mnt/arches-pkg-cache"
    mount_point.mkdir(parents=True, exist_ok=True)

    try:
        run(["mount", "--bind", str(cache), str(mount_point)], log=log)
        transaction.mounted = True
        run(["mount", "-o", "remount,bind,ro", str(mount_point)], log=log)
    except subprocess.CalledProcessError:
        _log("Failed to bind-mount package cache into target.", log)
        raise

    # Add the bind-mount as an extra CacheDir in the target's pacman.conf.
    # The chroot sees it at /mnt/arches-pkg-cache.
    conf_text = target_pacman_conf.read_text()
    if "/mnt/arches-pkg-cache" not in conf_text:
        cache_line = "CacheDir = /mnt/arches-pkg-cache/\n"
        conf_text = conf_text.replace(
            "[options]\n",
            f"[options]\n{cache_line}",
            1,
        )
        target_pacman_conf.write_text(conf_text)

    _log("Bind-mounted ISO package cache into target.", log)


def _unmount_iso_cache_from_target(
    log: LogCallback | None = None, *, transaction: PackageTransaction
) -> None:
    """Unmount the ISO package cache from the target and clean up pacman.conf."""
    mount_point = transaction.root / "mnt/arches-pkg-cache"
    target_conf = transaction.root / "etc/pacman.conf"
    try:
        if transaction.mounted:
            # Keep mounted=True if unmount fails; never report false success.
            run(["umount", str(mount_point)], log=log)
            transaction.mounted = False
    finally:
        if target_conf.exists():
            conf_text = target_conf.read_text()
            cleaned = conf_text.replace("CacheDir = /mnt/arches-pkg-cache/\n", "")
            if cleaned != conf_text:
                target_conf.write_text(cleaned)

    # Clean up mount point
    try:
        mount_point.rmdir()
    except OSError:
        pass


def _query_available_packages(
    pacman_conf: Path,
    log: LogCallback | None = None,
    *,
    dbpath: Path,
    gpgdir: Path,
) -> set[str] | None:
    """Query pacman for all available package names.

    Returns a set of package names, or None if the query fails.
    Used to filter out packages that aren't in any configured repo.
    """
    try:
        result = subprocess.run(
            [
                "pacman",
                "--config",
                str(pacman_conf),
                "--dbpath",
                str(dbpath),
                "--gpgdir",
                str(gpgdir),
                "-Ssq",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return set(result.stdout.strip().splitlines())
    except (subprocess.CalledProcessError, FileNotFoundError):
        _log("WARNING: Could not query available packages.", log)
        return None


def _preseed_pacman_databases(
    log: LogCallback | None = None, *, source: Path, target: Path
) -> None:
    """Copy selected snapshot databases into the target.

    pacstrap runs ``pacman -Sy`` which fails offline because it can't
    sync remote databases. By pre-seeding the selected snapshot, pacstrap
    finds them already present and can install from the local cache.
    """
    host_db = source
    target_db = target
    if host_db.exists() and any(host_db.glob("*.db")):
        target_db.mkdir(parents=True, exist_ok=True)
        for db_file in host_db.iterdir():
            shutil.copy2(db_file, target_db / db_file.name)
        _log("Pre-seeded selected pacman databases.", log)


def _preseed_pacman_keyring(
    log: LogCallback | None = None, *, transaction: PackageTransaction
) -> None:
    """Initialize selected public roots; only matching legacy media may copy trust."""
    if not transaction.legacy:
        gpgdir = transaction.gpgdir
        gpgdir.mkdir(mode=0o700, parents=True, exist_ok=True)
        common = [
            "pacman-key",
            "--config",
            str(transaction.config),
            "--gpgdir",
            str(gpgdir),
        ]
        run(common + ["--init"], log=log)
        names = (
            ["archlinux", "omarchy"]
            if transaction.provider.id == "omarchy"
            else ["archlinux"]
        )
        run(
            common
            + ["--populate-from", str(transaction.keyring_source), "--populate"]
            + names,
            log=log,
        )
        if not all(
            (gpgdir / name).is_file() and (gpgdir / name).stat().st_size
            for name in ("pubring.gpg", "trustdb.gpg")
        ):
            raise ValueError("Isolated bootstrap keyring initialization failed")
        gpgdir.chmod(0o700 if transaction.provider.id == "omarchy" else 0o755)
        return
    host_gnupg = transaction.keyring_source
    target_gnupg = transaction.gpgdir

    if not host_gnupg.exists() or not (host_gnupg / "trustdb.gpg").exists():
        _log("WARNING: Host keyring not found, skipping keyring pre-seed.", log)
        return

    target_gnupg.mkdir(parents=True, exist_ok=True)

    def _ignore_sockets(directory: str, entries: list[str]) -> list[str]:
        """Skip Unix domain sockets (gpg-agent, keyboxd, dirmngr)."""
        ignored = []
        for entry in entries:
            full = Path(directory) / entry
            try:
                if full.is_socket():
                    ignored.append(entry)
            except OSError:
                ignored.append(entry)
        return ignored

    shutil.copytree(
        host_gnupg, target_gnupg, dirs_exist_ok=True, ignore=_ignore_sockets
    )

    # The live ISO's pacman-init.service chmods /etc/pacman.d/gnupg to
    # 0700 (gpg2 refuses to write to a homedir that's group/other
    # accessible). copytree preserves that mode on the target — but
    # 0700 prevents an unprivileged user on the INSTALLED system from
    # running e.g. `pacman -Q` (pacman wants to read pubring.gpg /
    # trustdb.gpg to validate db signatures, and can't even traverse
    # the directory). Vanilla Arch ships /etc/pacman.d/gnupg at 0755;
    # match that here. gpg2 is happy with 0755 + 0600 files inside,
    # which is exactly what we get after pacstrap re-populates the
    # keyring.
    try:
        target_gnupg.chmod(0o755)
    except OSError as e:
        _log(f"WARNING: failed to chmod {target_gnupg} to 0755: {e}", log)

    _log("Pre-seeded pacman keyring from live system.", log)


def pacstrap(
    platform: PlatformConfig,
    template: InstallTemplate,
    log: LogCallback | None = None,
    hardware: HardwareConfig | None = None,
    *,
    provider: ProviderConfig | None = None,
    inputs: ProviderInputs | None = None,
    transaction: PackageTransaction | None = None,
) -> None:
    """Install base packages into MOUNT_ROOT via pacstrap."""
    if transaction is None:
        with package_transaction(
            platform, provider=provider, inputs=inputs, log=log
        ) as transaction:
            _pacstrap(platform, template, log, hardware, transaction)
    else:
        if (
            provider != transaction.provider
            or transaction.root != MOUNT_ROOT
            or inputs is not None
        ):
            raise ValueError("Mismatched package transaction")
        _pacstrap(platform, template, log, hardware, transaction)


def _pacstrap(platform, template, log, hardware, transaction):
    _log("Running pacstrap...", log)

    # Base packages and kernels come from the selected provider.
    # This includes core system packages, keyrings, bootloader, etc.
    base_packages = list(transaction.provider.base_packages)

    # Kernel variants (each gets a bootloader entry)
    for variant in transaction.provider.kernel.variants:
        base_packages.append(variant.package)
        base_packages.append(variant.headers)

    # Template-specific packages (pacstrap phase only).
    # Override and firstboot packages are handled in separate pipeline steps.
    all_packages = base_packages + template.install.pacstrap

    # Hardware-specific packages (from machine profile)
    if hardware and hardware.all_packages:
        _log(f"Adding {len(hardware.all_packages)} hardware packages", log)
        all_packages = all_packages + hardware.all_packages

    _check_package_arguments(all_packages)

    _log(f"Total packages: {len(all_packages)}", log)

    pacman_conf = transaction.config
    has_offline_cache = transaction.cache.exists() and any(
        transaction.cache.glob("*.pkg.tar.*")
    )
    if has_offline_cache:
        _log(f"Using cached packages from {transaction.cache}", log)
        cache_sync = transaction.sync_source
        if cache_sync.exists():
            dbs = list(cache_sync.glob("*.db"))
            _log(f"  Cached databases: {len(dbs)} in {cache_sync}", log)
        else:
            _log(f"  WARNING: No cached databases at {cache_sync}", log)
    else:
        _log("No offline package cache — packages will be downloaded", log)
    # Log CacheDir and file:// servers in generated config
    for line in pacman_conf.read_text().splitlines():
        if "CacheDir" in line or "Server = file://" in line:
            _log(f"  {line.strip()}", log)

    # Check if the arches-local repo is empty. If so, filter out packages
    # that aren't available in upstream repos to avoid pacstrap failing on
    # custom packages that haven't been pre-built (e.g. host-install without
    # a prior ISO build). Filtered packages are deferred to post-install.
    deferred: list[str] = []
    local_repo = Path("/opt/arches-repo")
    local_repo_empty = not local_repo.exists() or not any(
        local_repo.glob("*.pkg.tar.*")
    )
    if transaction.legacy and local_repo_empty:
        # An unbuilt file repository cannot participate in an online sync.
        transaction.final_conf = re.sub(
            r"(?ms)^\[arches-local\]\s*\n.*?(?=^\[|\Z)", "", transaction.final_conf
        )
        _make_pacman_conf_with_cache(transaction)
    if not transaction.legacy or (local_repo_empty and template.install.pacstrap):
        if transaction.legacy and not any(transaction.sync_source.glob("*.db")):
            transaction.dbpath.mkdir(parents=True, exist_ok=True)
            run(
                ["pacman"]
                + transaction.pacman_args
                + ["-Sy", "--noconfirm", "--ask=64"],
                log=log,
            )
            # Retain this selected database set for pacstrap and later transactions.
            transaction.sync_source = transaction.dbpath / "sync"
            _make_pacman_conf_with_cache(transaction)
        available = _query_available_packages(
            pacman_conf, log, dbpath=transaction.dbpath, gpgdir=transaction.gpgdir
        )
        if not transaction.legacy:
            if available is None:
                raise ValueError("Cannot query selected provider packages")
            missing = set(all_packages) - available
            if missing:
                raise ValueError(
                    f"Unavailable required provider packages: {', '.join(sorted(missing))}"
                )
        if available is not None:
            filtered = []
            for pkg in all_packages:
                if pkg in available:
                    filtered.append(pkg)
                else:
                    deferred.append(pkg)
                    _log(f"  Deferring unavailable package: {pkg}", log)
            if deferred:
                _log(
                    f"Deferred {len(deferred)} package(s) not in upstream repos.",
                    log,
                )
            all_packages = filtered

    if not transaction.legacy:
        # Verify the complete dependency closure before executing package hooks.
        # Bit 6 refuses ALPM_QUESTION_IMPORT_KEY, as in the signed-closure proof.
        run(
            ["pacman"]
            + transaction.pacman_args
            + ["-Sw", "--noconfirm", "--ask=64"]
            + all_packages,
            log=log,
        )

    # pacstrap uses `pacman -Sy` which syncs databases + installs.  Our
    # custom pacman.conf points at the selected snapshot, so -Sy can
    # succeed offline. Packages come from the selected cache.
    #
    # -c: Preserve explicit CacheDir paths rather than pacstrap's override
    # -G: Never copy the host keyring implicitly
    # -M: Don't copy mirrorlist (we install our own later)
    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        try:
            run(
                [
                    "pacstrap",
                    "-c",
                    "-G",
                    "-M",
                    "-C",
                    str(pacman_conf),
                    str(MOUNT_ROOT),
                    "--dbpath",
                    str(transaction.dbpath),
                    "--gpgdir",
                    str(transaction.gpgdir),
                    "--ask=64",
                ]
                + all_packages,
                log=log,
            )
            break
        except subprocess.CalledProcessError:
            if attempt < max_attempts:
                _log(
                    f"pacstrap failed (attempt {attempt}/{max_attempts}), retrying...",
                    log,
                )
            else:
                raise

    if deferred:
        _log(
            f"NOTE: {len(deferred)} package(s) were deferred because they are "
            "only available in the arches-local repo, which is empty. "
            "Pre-build with: make iso",
            log,
        )
        _log(f"  Deferred: {', '.join(deferred)}", log)

    if not transaction.legacy:
        # Standalone pacstrap must not leave the package-default Arch config
        # behind when its selected provider owns a different repository policy.
        (transaction.root / "etc/pacman.conf").write_text(transaction.final_conf)


def generate_fstab(log: LogCallback | None = None) -> None:
    """Generate fstab from current mounts."""
    _log("Generating fstab...", log)

    # Unmount stale mounts that pacstrap hooks may have created
    # (e.g., asahi-fwextract mounts the ESP at /run/.system-efi)
    stale_run = MOUNT_ROOT / "run" / ".system-efi"
    if stale_run.is_mount():
        try:
            run(["umount", str(stale_run)], log=log)
        except subprocess.CalledProcessError:
            pass

    result = run(
        ["genfstab", "-U", str(MOUNT_ROOT)],
        log=log,
        capture_output=True,
    )

    # Filter out stale entries that genfstab may have picked up:
    # - /run/.system-efi (asahi-fwextract hook artifact)
    # - nonexistent swap files
    stale_patterns = ["/run/.system-efi", "/var/swap/swapfile"]
    lines = result.stdout.splitlines(keepends=True)
    filtered: list[str] = []
    skip_block = False
    for line in lines:
        if any(p in line for p in stale_patterns):
            skip_block = True
            _log(f"Filtered stale fstab entry: {line.strip()}", log)
            continue
        if skip_block and line.strip() == "":
            skip_block = False
            continue
        if skip_block:
            continue
        filtered.append(line)

    fstab_path = MOUNT_ROOT / "etc" / "fstab"
    fstab_path.write_text("".join(filtered))


def append_swap_fstab_entries(
    swap_partitions: list[str],
    log: LogCallback | None = None,
) -> None:
    """Append fstab entries for swap partitions.

    ``genfstab`` only emits swap entries for *active* swap (read from
    /proc/swaps). Swap partitions created by ``mkswap`` during disk
    layout application aren't active yet — so they never appear in
    fstab unless we add them ourselves.

    Each partition path is resolved to a UUID via ``blkid`` so the
    fstab entry survives reboots even if kernel device names change.
    """
    if not swap_partitions:
        return

    fstab_path = MOUNT_ROOT / "etc" / "fstab"
    lines = ["", "# Swap partitions (added by Arches installer)"]
    for part in swap_partitions:
        try:
            uuid = run(
                ["blkid", "-s", "UUID", "-o", "value", part],
                log=log,
                capture_output=True,
            ).stdout.strip()
        except subprocess.CalledProcessError:
            _log(f"  WARNING: could not read UUID for swap {part}", log)
            continue
        if not uuid:
            _log(f"  WARNING: empty UUID for swap {part}", log)
            continue
        lines.append(f"UUID={uuid}\tnone\tswap\tdefaults\t0 0")
        _log(f"  swap fstab: UUID={uuid} ({part})", log)

    if len(lines) > 2:  # we actually added at least one entry
        with open(fstab_path, "a") as f:
            f.write("\n".join(lines) + "\n")


def _bind_mount_var_path(
    var_rel: str,
    usr_rel: str,
    label: str,
    log: LogCallback | None = None,
) -> bool:
    """Move a ``/var`` path onto the root subvolume via bind mount.

    Copies *var_rel* (relative to ``/var``) to *usr_rel* (relative to
    ``/usr``), bind-mounts it back, and appends an fstab entry.

    Returns ``True`` if the bind mount was set up, ``False`` if skipped.
    """
    src = MOUNT_ROOT / "var" / var_rel
    dest = MOUNT_ROOT / "usr" / usr_rel
    fstab_path = MOUNT_ROOT / "etc" / "fstab"

    if not src.is_dir():
        _log(f"  /var/{var_rel} does not exist yet — skipping {label}.", log)
        return False

    _log(f"  Binding /var/{var_rel} → /usr/{usr_rel} ({label})...", log)

    # 1. Copy to the root subvolume
    dest.mkdir(parents=True, exist_ok=True)
    run(["cp", "-a", "--", f"{src}/.", str(dest)], log=log)

    # 2. Bind-mount so chroot operations use the new location
    run(["mount", "--bind", str(dest), str(src)], log=log)

    # 3. Append fstab entry for persistence across reboots
    fstab_entry = (
        f"\n# Bind-mount {label} onto root subvolume for snapshot consistency\n"
        f"/usr/{usr_rel}\t/var/{var_rel}\tnone\tbind\t0 0\n"
    )
    with fstab_path.open("a") as f:
        f.write(fstab_entry)

    return True


def configure_var_bind_mounts(log: LogCallback | None = None) -> None:
    """Bind-mount critical ``/var`` state directories onto the root subvolume.

    When btrfs is used with separate ``@`` and ``@var`` subvolumes,
    directories under ``/var`` are excluded from snapper snapshots of
    ``@``.  A snapshot restore then rolls back the root filesystem but
    leaves these databases out of sync — a dangerous split-brain.

    This function moves the following onto ``/usr`` (part of ``@``) and
    bind-mounts them back so tools find them at the expected paths:

    * **pacman DB** (``/var/lib/pacman`` → ``/usr/lib/pacman``):
      Without this, a rollback leaves pacman thinking newer packages are
      installed while the actual files are from the snapshot.

    * **DKMS state** (``/var/lib/dkms`` → ``/usr/lib/dkms``):
      Without this, DKMS tracks module builds for kernel versions that
      no longer match what is on disk, potentially leaving the system
      without nvidia or other out-of-tree modules after rollback.

    The function is a no-op if ``/var`` is not a separate mount (i.e.
    there is no subvolume split to worry about).
    """
    var_mount = MOUNT_ROOT / "var"

    if not var_mount.is_mount():
        _log(
            "/var is not a separate mount — skipping snapshot bind mounts.",
            log,
        )
        return

    _log("Configuring /var bind mounts for snapshot consistency...", log)

    _bind_mount_var_path("lib/pacman", "lib/pacman", "pacman DB", log)
    _bind_mount_var_path("lib/dkms", "lib/dkms", "DKMS state", log)

    _log("Bind mounts configured.", log)


def install_pacman_conf(
    log: LogCallback | None = None, *, transaction: PackageTransaction
) -> None:
    """Install selected repository policy and, for legacy media, its local repo."""
    _log("Installing selected provider pacman.conf...", log)
    target = transaction.root / "etc/pacman.conf"
    target.write_text(transaction.final_conf)

    # Make the ISO package cache available inside the chroot so that
    # post-pacstrap operations (override packages, hardware detection)
    # can install without downloading.  We bind-mount the cache into
    # the target and add it as an extra CacheDir in pacman.conf.
    _mount_iso_cache_in_target(target, log, transaction=transaction)

    if not transaction.legacy:
        return

    # Copy the local AUR repo into the target so packages are available
    # inside the chroot (for ansible tasks that install from arches-local).
    iso_repo = Path("/opt/arches-repo")
    target_repo = transaction.root / "opt/arches-repo"
    if iso_repo.exists() and not target_repo.exists():
        _log("Copying local AUR repo into target...", log)
        shutil.copytree(iso_repo, target_repo)


def copy_mirrorlists(
    platform: PlatformConfig,
    log: LogCallback | None = None,
) -> None:
    """Copy active mirrorlist files from the live ISO into the installed target.

    pacstrap ``-M`` skips the host mirrorlist copy, leaving the target with
    the package-default (empty/commented) mirrorlist files.  Copying the
    live system's populated files gives the installed system working mirrors
    immediately — even on offline installs where reflector can't run.

    The tier-specific CachyOS mirrorlist (e.g. ``cachyos-v3-mirrorlist``)
    is derived from the platform's ``cachyos_optimization_tier`` so the
    correct file is copied for v3, v4, and znver4 configurations.
    """
    mirrorlists = ["mirrorlist", "cachyos-mirrorlist"]
    if platform.cachyos_mirrorlist_name:
        mirrorlists.append(platform.cachyos_mirrorlist_name)
    for name in mirrorlists:
        src = Path("/etc/pacman.d") / name
        dst = MOUNT_ROOT / "etc/pacman.d" / name
        if src.exists() and dst.exists():
            shutil.copy2(src, dst)
            _log(f"Copied mirrorlist: {name}", log)


def sync_chroot_databases(
    log: LogCallback | None = None, *, transaction: PackageTransaction
) -> None:
    """Reuse pacstrap's selected repository state, not the live configuration."""
    try:
        run(
            ["pacman"] + transaction.pacman_args + ["-Sy", "--noconfirm", "--ask=64"],
            log=log,
        )
    except subprocess.CalledProcessError:
        if not transaction.legacy:
            raise
        _log(
            "Database sync failed (no network?), continuing with cached databases.", log
        )


def _check_package_arguments(packages: list[str]) -> None:
    # pacstrap appends its own pacman options after the package arguments, so
    # inserting '--' would also turn its --config into a package name.
    for package in packages:
        if (
            not package
            or package.startswith(("-", "/"))
            or any(c.isspace() or ord(c) < 32 for c in package)
        ):
            raise ValueError(f"Invalid package argument: {package!r}")


def install_override_packages(
    template: InstallTemplate,
    log: LogCallback | None = None,
    *,
    transaction: PackageTransaction,
) -> None:
    """Install packages that conflict with stock packages (need --overwrite).

    Must run after install_pacman_conf so the arches-local repo is available.
    Reads from template.install.override.
    """
    if not template.install.override:
        return

    if transaction.provider.pacman_owner != "arches":
        raise ValueError("Selected provider does not permit Arches overrides")
    _check_package_arguments(template.install.override)

    if any(transaction.cache.glob("*.pkg.tar.*")) and not transaction.mounted:
        raise ValueError("Selected package cache must be mounted before overrides")
    # arch-chroot restores the API mounts that pacstrap tears down. Remap the
    # existing transaction configuration, not the final online repository policy.
    conf_text = transaction.config.read_text()
    conf_text = re.sub(r"^CacheDir\s*=.*\n?", "", conf_text, flags=re.MULTILINE)
    conf_text = re.sub(
        r"^((?:DBPath|GPGDir|LogFile)\s*=\s*)" + re.escape(str(transaction.root)) + "/",
        r"\1/",
        conf_text,
        flags=re.MULTILINE,
    )
    conf_text = conf_text.replace(
        f"Server = file://{transaction.root}/", "Server = file:///"
    )
    caches = "CacheDir = /var/cache/pacman/pkg/\n"
    if transaction.mounted:
        caches += "CacheDir = /mnt/arches-pkg-cache/\n"
    conf_text = conf_text.replace("[options]\n", f"[options]\n{caches}", 1)
    chroot_conf = transaction.work / "chroot-pacman.conf"
    chroot_conf.write_text(conf_text)

    _log(f"Installing override packages: {', '.join(template.install.override)}", log)
    # Databases are already synced from pacstrap.  Use -S (not -Sy) so
    # this works offline.  The local arches-repo is already available.
    # --ask=68 approves conflict replacements and refuses unknown-key imports
    # (e.g. kwin -> arches-kwin-patched which declares conflicts=('kwin'))
    run(
        [
            "arch-chroot",
            str(transaction.root),
            "pacman",
            "--config",
            "/" + chroot_conf.relative_to(transaction.root).as_posix(),
            "--dbpath",
            "/var/lib/pacman",
            "--gpgdir",
            "/etc/pacman.d/gnupg",
        ]
        + ["-S", "--noconfirm", "--overwrite", "*", "--ask=68"]
        + template.install.override,
        log=log,
    )


def configure_locale(
    locale: str = "en_US.UTF-8",
    log: LogCallback | None = None,
) -> None:
    """Set locale in the target system."""
    _log(f"Setting locale to {locale}...", log)
    locale_gen = MOUNT_ROOT / "etc" / "locale.gen"
    content = locale_gen.read_text()
    content = content.replace(f"#{locale}", locale)
    locale_gen.write_text(content)
    chroot_run(["locale-gen"], log=log)

    locale_conf = MOUNT_ROOT / "etc" / "locale.conf"
    locale_conf.write_text(f"LANG={locale}\n")

    vconsole = MOUNT_ROOT / "etc" / "vconsole.conf"
    vconsole.write_text("KEYMAP=us\nFONT=ter-116n\n")


def configure_timezone(
    timezone: str = "America/Denver",
    log: LogCallback | None = None,
) -> None:
    """Set timezone in the target system."""
    _log(f"Setting timezone to {timezone}...", log)
    chroot_run(
        [
            "ln",
            "-sf",
            f"/usr/share/zoneinfo/{timezone}",
            "/etc/localtime",
        ],
        log=log,
    )
    chroot_run(["hwclock", "--systohc"], log=log)


def configure_hostname(
    hostname: str,
    log: LogCallback | None = None,
) -> None:
    """Set hostname."""
    _log(f"Setting hostname to {hostname}...", log)
    hostname_file = MOUNT_ROOT / "etc" / "hostname"
    hostname_file.write_text(f"{hostname}\n")

    hosts_file = MOUNT_ROOT / "etc" / "hosts"
    hosts_file.write_text(
        f"127.0.0.1  localhost\n::1        localhost\n127.0.1.1  {hostname}\n"
    )

    # Override os-release so that limine-entry-tool and other tools
    # identify the OS as "Arches Linux" instead of "Arch Linux".
    os_release = MOUNT_ROOT / "etc" / "os-release"
    os_release.write_text(
        'NAME="Arches Linux"\n'
        'PRETTY_NAME="Arches Linux"\n'
        "ID=arches\n"
        "ID_LIKE=arch\n"
        "BUILD_ID=rolling\n"
    )
    _log("Set os-release to Arches Linux.", log)


def create_user(
    username: str,
    password: str,
    log: LogCallback | None = None,
) -> None:
    """Create a user with sudo privileges.

    Idempotent: if the user already exists (e.g., from a prior failed
    install on the same subvolume), update their groups and shell instead
    of failing.
    """
    _log(f"Creating user {username}...", log)
    try:
        chroot_run(
            [
                "useradd",
                "-m",
                "-G",
                "wheel",
                "-s",
                "/bin/zsh",
                username,
            ],
            log=log,
        )
    except subprocess.CalledProcessError:
        # User already exists — update groups and shell
        _log(f"User {username} already exists, updating...", log)
        chroot_run(
            ["usermod", "-a", "-G", "wheel", "-s", "/bin/zsh", username],
            log=log,
        )

    # Set password via chpasswd
    _log("Setting password...", log)
    subprocess.run(
        ["arch-chroot", str(MOUNT_ROOT), "chpasswd"],
        input=f"{username}:{password}\n",
        capture_output=True,
        text=True,
        check=True,
    )

    # Enable wheel group in sudoers
    sudoers = MOUNT_ROOT / "etc" / "sudoers.d" / "wheel"
    sudoers.parent.mkdir(parents=True, exist_ok=True)
    sudoers.write_text("%wheel ALL=(ALL:ALL) NOPASSWD: ALL\n")
    sudoers.chmod(0o440)


def deploy_ssh_key(
    username: str,
    log: LogCallback | None = None,
) -> None:
    """Deploy build host's SSH public key to the created user's authorized_keys.

    The key is embedded into the ISO at build time as /opt/arches/build-host.pub.
    If the file doesn't exist (no key was available at build time), this is a
    silent no-op.
    """
    if not ISO_BUILD_HOST_PUBKEY.exists():
        return

    _log("Deploying build host SSH key...", log)
    pubkey = ISO_BUILD_HOST_PUBKEY.read_text().strip()

    ssh_dir = MOUNT_ROOT / "home" / username / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    auth_keys = ssh_dir / "authorized_keys"
    auth_keys.write_text(pubkey + "\n")

    # Set ownership to the user (look up UID/GID from the target's /etc/passwd)
    passwd = (MOUNT_ROOT / "etc" / "passwd").read_text()
    for line in passwd.splitlines():
        fields = line.split(":")
        if fields[0] == username:
            uid, gid = int(fields[2]), int(fields[3])
            break
    else:
        _log("WARNING: Could not find user in /etc/passwd, skipping SSH key.", log)
        return

    os.chown(ssh_dir, uid, gid)
    ssh_dir.chmod(0o700)
    os.chown(auth_keys, uid, gid)
    auth_keys.chmod(0o600)
    _log(f"Installed authorized_keys for {username}.", log)


def enable_services(
    services: list[str],
    log: LogCallback | None = None,
) -> None:
    """Enable systemd services in the target."""
    for service in services:
        _log(f"Enabling {service}...", log)
        chroot_run(["systemctl", "enable", service], log=log)


def stage_ansible(
    template: InstallTemplate,
    log: LogCallback | None = None,
) -> None:
    """Copy ansible playbooks into the target for firstboot use."""
    if not template.ansible.firstboot_roles:
        _log("No first-boot roles configured, skipping ansible staging.", log)
        return

    from arches_installer.core import ohmyzsh, provider

    offline_omz = "zsh" in template.ansible.firstboot_roles and ohmyzsh.required(provider.STAGED_ROOT)
    if offline_omz:
        source = provider.STAGED_ROOT
        ohmyzsh.preflight(source)
        target = MOUNT_ROOT / "opt/arches"
        target.mkdir(parents=True, exist_ok=True)
        assets = target / "firstboot-assets"
        if assets.exists():
            shutil.rmtree(assets)
        assets.mkdir(mode=0o755)
        for relative in ("firstboot-assets.json", "firstboot-assets/ohmyzsh.bundle"):
            shutil.copyfile(source / relative, target / relative)
            (target / relative).chmod(0o644)
        shutil.copyfile(Path(ohmyzsh.__file__), target / "ohmyzsh.py")
        (target / "ohmyzsh.py").chmod(0o644)
        ohmyzsh.preflight(target)

    _log(
        f"Staging ansible for first-boot (roles: "
        f"{', '.join(template.ansible.firstboot_roles)})...",
        log,
    )

    target_ansible = MOUNT_ROOT / "opt" / "arches" / "ansible"
    if ISO_ANSIBLE_DIR.exists():
        _log(f"Copying {ISO_ANSIBLE_DIR} -> {target_ansible}...", log)
        if target_ansible.exists():
            shutil.rmtree(target_ansible)
        shutil.copytree(ISO_ANSIBLE_DIR, target_ansible)
        if offline_omz:
            # Survives loss of both the helper and manifest at firstboot.
            required = target_ansible / "group_vars/all/ohmyzsh.yml"
            required.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            required.write_text("zsh_offline_required: true\n")
            required.chmod(0o644)
        _log("Ansible playbooks staged.", log)
    else:
        _log(
            f"WARNING: Ansible directory not found at {ISO_ANSIBLE_DIR}, "
            f"first-boot roles will not be available!",
            log,
        )


def run_mkinitcpio(
    platform: PlatformConfig,
    log: LogCallback | None = None,
) -> None:
    """Regenerate initramfs in the target."""
    _log("Regenerating initramfs...", log)
    chroot_run(["mkinitcpio", "-P"], log=log)


def run_hardware_detection(
    platform: PlatformConfig,
    log: LogCallback | None = None,
) -> None:
    """Run platform-specific hardware detection (e.g., GPU drivers)."""
    hw = platform.hardware_detection
    if not hw.enabled:
        _log("Hardware detection not enabled for this platform, skipping.", log)
        return

    _log(f"Running hardware detection ({hw.tool})...", log)
    try:
        chroot_run([hw.tool] + hw.args, log=log)
    except subprocess.CalledProcessError:
        if hw.optional:
            _log(
                f"{hw.tool} failed (may be expected in a VM), continuing...",
                log,
            )
        else:
            raise


# Module-level cache: once the keyring is verified ready in this process,
# subsequent callers (e.g. _query_available_packages, pacstrap) can short-circuit.
_KEYRING_READY = False


def _keyring_is_populated() -> bool:
    """Return True if /etc/pacman.d/gnupg looks fully initialized.

    Checks the marker file written by pacman-init.service's ExecStartPost,
    plus the canonical trustdb.gpg file as a fallback.
    """
    marker = Path("/run/arches-keyring-ready")
    if marker.exists():
        return True
    trustdb = Path("/etc/pacman.d/gnupg/trustdb.gpg")
    pubring = Path("/etc/pacman.d/gnupg/pubring.gpg")
    # trustdb alone isn't sufficient — --init creates it before --populate
    # imports keys. We need both, and a non-empty pubring.
    return (
        trustdb.exists()
        and trustdb.stat().st_size > 0
        and pubring.exists()
        and pubring.stat().st_size > 0
    )


def _wait_for_keyring(log: LogCallback | None = None, timeout: int = 120) -> None:
    """Wait for pacman-init.service to finish populating the keyring.

    On the live ISO, /etc/pacman.d/gnupg is a tmpfs that starts empty.
    pacman-init.service runs ``pacman-key --init && --populate`` but it
    races with getty autologin — if the installer starts pacstrap before
    the keyring is ready, pacman fails with "keyring is not writable"
    (gpg locks) or "key is unknown" (populate not yet finished).

    Idempotent and cheap once the keyring is ready (sets a process-local
    flag so repeated calls are instant). Safe to call before any pacman
    invocation in the installer.
    """
    global _KEYRING_READY
    if _KEYRING_READY:
        return

    if _keyring_is_populated():
        _log("Pacman keyring already initialized.", log)
        _KEYRING_READY = True
        return

    _log("Waiting for pacman-init.service to finish...", log)

    # Best-effort: kick the unit in case it hasn't started yet (e.g. when
    # installer is launched manually before multi-user.target).
    try:
        subprocess.run(
            ["systemctl", "start", "--no-block", "pacman-init.service"],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    deadline = time.monotonic() + timeout
    service_failed = False
    while time.monotonic() < deadline:
        if _keyring_is_populated():
            _log("Pacman keyring populated.", log)
            _KEYRING_READY = True
            return

        try:
            result = subprocess.run(
                ["systemctl", "is-active", "pacman-init.service"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            state = result.stdout.strip()
            if state == "failed":
                _log(
                    "WARNING: pacman-init.service failed. "
                    "Falling back to manual keyring init.",
                    log,
                )
                service_failed = True
                break
            # "active" + RemainAfterExit oneshot = done, but double-check
            # files actually exist (defensive against partial runs).
            if state == "active" and _keyring_is_populated():
                _log("pacman-init.service completed.", log)
                _KEYRING_READY = True
                return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        time.sleep(2)

    # Timeout or explicit failure: do it ourselves. Ensure homedir perms
    # are gpg-correct before invoking pacman-key.
    if not service_failed:
        _log(
            "Timed out waiting for pacman-init.service; "
            "initializing keyring manually.",
            log,
        )
    gnupg_dir = Path("/etc/pacman.d/gnupg")
    try:
        gnupg_dir.mkdir(parents=True, exist_ok=True)
        gnupg_dir.chmod(0o700)
    except OSError as e:
        _log(f"WARNING: could not prepare {gnupg_dir}: {e}", log)

    try:
        subprocess.run(
            ["pacman-key", "--init"],
            check=True,
            capture_output=True,
            timeout=60,
        )
        subprocess.run(
            ["pacman-key", "--populate"],
            check=True,
            capture_output=True,
            timeout=120,
        )
        _log("Pacman keyring initialized manually.", log)
        _KEYRING_READY = True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        stderr = getattr(e, "stderr", b"")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        _log(f"WARNING: Manual keyring init failed: {e}\n{stderr}", log)


def _pre_pacstrap_setup(log: LogCallback | None = None) -> None:
    """Create config files that pacstrap's post-install hooks need."""
    etc = MOUNT_ROOT / "etc"
    etc.mkdir(parents=True, exist_ok=True)

    # mkinitcpio's keymap/sd-vconsole hooks need vconsole.conf
    (etc / "vconsole.conf").write_text("KEYMAP=us\nFONT=ter-116n\n")
    _log("Pre-created /etc/vconsole.conf for pacstrap hooks.", log)

    # Pre-create limine config files so that limine-mkinitcpio-hook's
    # post-install scripts work during pacstrap (before our bootloader
    # phase writes the full config).  The hook needs:
    #   - /etc/default/limine with ESP_PATH
    #   - /etc/kernel/cmdline (even if placeholder)
    default_dir = etc / "default"
    default_dir.mkdir(parents=True, exist_ok=True)
    (default_dir / "limine").write_text('ESP_PATH="/boot"\n')
    kernel_dir = etc / "kernel"
    kernel_dir.mkdir(parents=True, exist_ok=True)
    (kernel_dir / "cmdline").write_text("root=/dev/vda2 rw\n")
    _log(
        "Pre-created /etc/default/limine and /etc/kernel/cmdline for pacstrap hooks.",
        log,
    )

    # Provide a mkinitcpio.conf with reliable hooks.
    # The default includes 'autodetect' which can fail in chroot/VM.
    # We use a broad set that works everywhere; autodetect can be
    # re-enabled by the user post-install for a smaller initramfs.
    (etc / "mkinitcpio.conf").write_text(
        "# Arches — generated by installer\n"
        "MODULES=()\n"
        "BINARIES=()\n"
        "FILES=()\n"
        "HOOKS=(base udev microcode modconf kms keyboard keymap "
        "block filesystems fsck)\n"
    )
    _log("Pre-created /etc/mkinitcpio.conf (no autodetect).", log)


def copy_apple_firmware(
    platform: PlatformConfig,
    log: LogCallback | None = None,
) -> None:
    """Copy Apple Silicon firmware from the host into the target system.

    On Apple Silicon, the firmware is extracted from the macOS APFS partition
    by asahi-fwextract and stored at /lib/firmware/vendor/. When installing
    from a running Asahi Linux host, we copy this firmware into the target
    so the new system has working WiFi, Bluetooth, GPU, etc.

    This is a no-op on non-Apple platforms or if the firmware directory
    doesn't exist on the host.
    """
    if platform.name != "aarch64-apple":
        return

    # Check common firmware locations on the host.
    # In the container, host firmware is bind-mounted at /host-firmware.
    # On a native host, it's at /lib/firmware/vendor or /usr/lib/firmware/vendor.
    host_fw_paths = [
        Path("/host-firmware"),
        Path("/lib/firmware/vendor"),
        Path("/usr/lib/firmware/vendor"),
    ]
    host_fw = None
    for p in host_fw_paths:
        if p.exists() and any(p.iterdir()):
            host_fw = p
            break

    if host_fw is None:
        _log(
            "WARNING: No Apple firmware found on host. "
            "WiFi/BT/GPU may not work in the installed system. "
            "Run asahi-fwextract after booting into the new system.",
            log,
        )
        return

    target_fw = MOUNT_ROOT / "lib" / "firmware" / "vendor"
    if target_fw.exists() and any(target_fw.iterdir()):
        _log("Firmware already present in target, skipping.", log)
        return

    _log(f"Copying Apple firmware from {host_fw}...", log)
    target_fw.parent.mkdir(parents=True, exist_ok=True)
    # Use dirs_exist_ok=True because asahi-fwextract creates the empty
    # vendor/ directory during pacstrap.
    shutil.copytree(host_fw, target_fw, dirs_exist_ok=True)
    _log("Apple firmware copied.", log)


def preseed_network_deps(
    username: str,
    log: LogCallback | None = None,
    *,
    template: InstallTemplate | None = None,
) -> None:
    """Pre-seed selected network dependencies on legacy media.

    New media supplies Oh My Zsh through the verified firstboot asset instead.
    The selected Rust toolchain still needs network; this is not an offline
    contract for Rust or other non-package workloads.
    """
    _log("Pre-seeding network-dependent resources...", log)

    from arches_installer.core import ohmyzsh, provider

    roles = template.ansible.firstboot_roles if template else ["zsh", "rust"]
    offline_omz = ohmyzsh.required(provider.STAGED_ROOT)
    if "zsh" in roles and offline_omz:
        ohmyzsh.preflight(provider.STAGED_ROOT)
        _log("Oh My Zsh will be cloned from the verified firstboot asset.", log)

    # oh-my-zsh for user
    omz_user = MOUNT_ROOT / "home" / username / ".oh-my-zsh"
    if "zsh" in roles and not offline_omz and not omz_user.exists():
        _log(f"Cloning oh-my-zsh for {username}...", log)
        try:
            chroot_run(
                [
                    "su",
                    "-",
                    username,
                    "-c",
                    "git clone --depth 1 https://github.com/ohmyzsh/ohmyzsh.git"
                    " ~/.oh-my-zsh",
                ],
                log=log,
            )
        except subprocess.CalledProcessError:
            _log("WARNING: oh-my-zsh clone failed for user (no network?)", log)

    # oh-my-zsh for root
    omz_root = MOUNT_ROOT / "root" / ".oh-my-zsh"
    if "zsh" in roles and not offline_omz and not omz_root.exists():
        _log("Cloning oh-my-zsh for root...", log)
        try:
            chroot_run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "https://github.com/ohmyzsh/ohmyzsh.git",
                    "/root/.oh-my-zsh",
                ],
                log=log,
            )
        except subprocess.CalledProcessError:
            _log("WARNING: oh-my-zsh clone failed for root (no network?)", log)

    # rustup stable toolchain
    if "rust" not in roles:
        return
    _log(f"Initializing rustup stable toolchain for {username}...", log)
    try:
        chroot_run(
            ["su", "-", username, "-c", "rustup default stable"],
            log=log,
        )
    except subprocess.CalledProcessError:
        _log("WARNING: rustup toolchain init failed (no network?)", log)

    _log("Pre-seeding complete.", log)


def configure_apple_input(
    platform: PlatformConfig,
    log: LogCallback | None = None,
) -> None:
    """Configure Apple Silicon keyboard and touchpad for a usable layout.

    Apple keyboards have a non-standard layout (Fn where Ctrl is, Cmd where
    Alt is, etc.). This sets hid_apple module options to swap keys into a
    standard PC layout:
      - swap_fn_leftctrl: Fn ↔ Left Ctrl
      - swap_opt_cmd: Option ↔ Command (so Cmd acts as Alt)
      - fnmode=2: Function keys are F1-F12 by default (media keys with Fn)

    Also ensures libinput quirks for the Apple touchpad are active.
    """
    if platform.name != "aarch64-apple":
        return

    _log("Configuring Apple keyboard and touchpad...", log)

    # hid_apple module options
    modprobe_dir = MOUNT_ROOT / "etc" / "modprobe.d"
    modprobe_dir.mkdir(parents=True, exist_ok=True)
    (modprobe_dir / "hid_apple.conf").write_text(
        "# Arches — Apple keyboard layout remapping\n"
        "# swap_fn_leftctrl: Fn ↔ Left Ctrl\n"
        "# swap_opt_cmd: Option ↔ Command (Cmd acts as Alt)\n"
        "# fnmode=2: F1-F12 by default, media keys with Fn held\n"
        "options hid_apple swap_fn_leftctrl=1\n"
        "options hid_apple swap_opt_cmd=1\n"
        "options hid_apple fnmode=2\n"
    )

    _log("Apple input configured (hid_apple key remapping).", log)


def install_system(
    platform: PlatformConfig,
    template: InstallTemplate,
    hostname: str,
    username: str,
    password: str,
    hardware: HardwareConfig | None = None,
    log: LogCallback | None = None,
    *,
    provider: ProviderConfig | None = None,
    inputs: ProviderInputs | None = None,
) -> None:
    """Full install pipeline after disk is prepared and mounted."""
    provider = provider or resolve_provider(platform)
    if template.providers and provider.id not in template.providers:
        raise ValueError("Template requires a different provider")
    if provider.lifecycle_owner != "arches":
        raise ValueError("Selected provider requires its lifecycle adapter (Task 5)")
    with package_transaction(
        platform, provider=provider, inputs=inputs, log=log
    ) as transaction:
        _install_system(
            platform, template, hostname, username, password, hardware, log, transaction
        )


def _install_system(
    platform, template, hostname, username, password, hardware, log, transaction
):
    # Hardware and boot properties remain platform-owned; package tooling does not.
    platform = replace(
        platform, hardware_detection=transaction.provider.hardware_detection
    )
    _pre_pacstrap_setup(log)
    pacstrap(
        platform,
        template,
        log,
        hardware=hardware,
        provider=transaction.provider,
        transaction=transaction,
    )
    generate_fstab(log)
    configure_var_bind_mounts(log)
    install_pacman_conf(log, transaction=transaction)
    if transaction.legacy:
        copy_mirrorlists(platform, log)
    sync_chroot_databases(log, transaction=transaction)
    install_override_packages(template, log, transaction=transaction)
    configure_locale(template.system.locale, log)
    configure_timezone(template.system.timezone, log)
    configure_hostname(hostname, log)
    create_user(username, password, log)
    deploy_ssh_key(username, log)
    copy_apple_firmware(platform, log)
    configure_apple_input(platform, log)
    # Deploy hardware quirk/machine config files (modprobe, udev, sysctl)
    if hardware:
        from arches_installer.core.hardware import (
            compute_fingerprint,
            deploy_hardware_files,
            detect_pci_ids,
            get_dmi_info,
            write_fingerprint,
            write_manifest,
        )

        manifest = deploy_hardware_files(hardware, log)
        write_manifest(manifest, MOUNT_ROOT, log)
    copy_network_profiles(log)
    preseed_network_deps(username, log, template=template)
    run_hardware_detection(platform, log)
    # Snapshot the hardware identity (PCI + DMI + chwd profile) so the
    # runtime arches-hardware-rescan service has a baseline to compare
    # against on every subsequent boot. We compute this AFTER chwd runs
    # so the recorded chwd_profile reflects the installer's decision.
    # Note: detect_chwd_profile() needs to run inside the chroot because
    # chwd's state lives in the target system, not the live ISO.
    if hardware or platform.hardware_detection.enabled:
        from arches_installer.core.hardware import (
            compute_fingerprint,
            detect_pci_ids,
            get_dmi_info,
            write_fingerprint,
        )

        try:
            # PCI + DMI are read from the build host (which sees the
            # same physical hardware as the target during install).
            pci_ids = detect_pci_ids()
            dmi = get_dmi_info()
            # chwd profile must be read from the chroot. Best-effort:
            # if it fails, we record an empty profile name — the first
            # runtime rescan will then trigger and re-record.
            chwd_profile = ""
            try:
                result = subprocess.run(
                    [
                        "arch-chroot",
                        str(MOUNT_ROOT),
                        "chwd",
                        "--list-installed",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        line = line.strip()
                        if line and not line.startswith(
                            ("=", "-", "NAME", "Profile")
                        ):
                            chwd_profile = line.split()[0]
                            break
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            fp = compute_fingerprint(pci_ids, dmi, chwd_profile)
            write_fingerprint(fp, MOUNT_ROOT, log)
        except Exception as e:
            # Never fail the install over fingerprint recording — the
            # runtime rescan will just see an empty fingerprint and
            # do its first reconciliation on the next boot.
            _log(f"  warning: failed to record hardware fingerprint: {e}", log)
    # NOTE: mkinitcpio is NOT run here — it's handled by
    # limine-mkinitcpio in the bootloader phase (Phase 3),
    # which generates both the initramfs and limine.conf entries.
    # Merge hardware services with template services
    services = list(template.services)
    if hardware:
        for svc in hardware.all_services:
            if svc not in services:
                services.append(svc)
        # Remove services the machine profile explicitly disables
        if hardware.all_services_disable:
            disabled = set(hardware.all_services_disable)
            removed = [s for s in services if s in disabled]
            if removed:
                _log(f"Hardware profile disabling services: {', '.join(removed)}", log)
            services = [s for s in services if s not in disabled]
    enable_services(services, log)
    stage_ansible(template, log)
