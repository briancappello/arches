"""Authenticated Omarchy 4.0.2 bootstrap. No public installation entry point."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import tomllib
from contextlib import ExitStack, contextmanager
from functools import partial
from pathlib import Path

from arches_installer.core.install import package_transaction
from arches_installer.core.provider import (
    ProviderInputs,
    validate_provider_inputs,
    validate_selection,
)
from arches_installer.core.run import run as _run

run = partial(_run, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})


def load_lock() -> dict:
    from arches_installer.core.provider import _PROVIDERS_SEARCH

    for directory in _PROVIDERS_SEARCH:
        path = directory / "omarchy/bootstrap.lock.toml"
        if path.is_file():
            with path.open("rb") as stream:
                return tomllib.load(stream)
    raise ValueError("Omarchy bootstrap lock is missing")


def _check_digest(path: Path, expected: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"Missing or unsafe bootstrap artifact: {path}")
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected:
        raise ValueError(f"Bootstrap artifact digest mismatch: {path.name}")


def authenticate_iso(iso: Path, signature: Path, public_key: Path) -> dict:
    """Authenticate local files before extraction, with no host trust-store access."""
    lock = load_lock()
    if lock["revision"] != "4.0.2":
        raise ValueError("Unsupported Omarchy bootstrap revision")
    _check_digest(iso, lock["iso_sha256"])
    _check_digest(public_key, lock["key_sha256"])
    if (
        not signature.is_file()
        or signature.is_symlink()
        or not signature.stat().st_size
    ):
        raise ValueError("Missing bootstrap signature")
    with tempfile.TemporaryDirectory(prefix="omarchy-gpgv-") as home:
        result = run(
            [
                "gpgv",
                "--homedir",
                home,
                "--keyring",
                str(public_key),
                "--status-fd",
                "1",
                "--",
                str(signature),
                str(iso),
            ],
            capture_output=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
    signers = re.findall(r"^\[GNUPG:\] VALIDSIG ([0-9A-F]{40}) ", result.stdout, re.M)
    if signers != [lock["fingerprint"]]:
        raise ValueError("Unexpected Omarchy bootstrap signing fingerprint")
    return lock


def stage_bootstrap(platform, provider, iso, signature, public_key, closure, output):
    """Convert the retained proof snapshot to signed, externally pinned inputs.

    Run natively in the existing builder, which supplies bsdtar, squashfs-tools,
    gpgv and pacman-key. This imports no host initialized keyring, executes no
    extracted script, and installs no packages. Unavailable local artifacts fail.
    """
    if provider.id != "omarchy" or platform.arch != "x86_64":
        raise ValueError("Bootstrap supports only Omarchy x86_64")
    if not output.is_absolute() or output.resolve() != output or output.exists():
        raise ValueError("Bootstrap output must be a new absolute directory")
    for parent in (output.parent, *output.parent.parents):
        info = parent.lstat()
        if (
            info.st_uid not in (0, os.geteuid())
            or stat.S_ISLNK(info.st_mode)
            or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)
        ):
            raise ValueError("Bootstrap output requires protected parent directories")
    lock = authenticate_iso(iso, signature, public_key)
    selection_path = closure / "selected-manifest.tsv"
    if not selection_path.is_file() or selection_path.is_symlink():
        raise ValueError("Missing or unsafe selected package manifest")
    selection = selection_path.read_bytes()
    if hashlib.sha256(selection).hexdigest() != lock["selected_manifest_sha256"]:
        raise ValueError("Selected package manifest digest mismatch")
    _check_digest(closure / "targets.txt", lock["targets_sha256"])
    with tempfile.TemporaryDirectory(
        prefix=".omarchy-bootstrap-", dir=output.parent
    ) as directory:
        work = Path(directory)
        staged = work / "inputs"
        staged.mkdir()
        files = {}

        def copy(source, name, expected=None):
            if source.resolve() != source or not source.is_file():
                raise ValueError(
                    "Bootstrap source must be a regular file without symlinks"
                )
            if expected is not None:
                _check_digest(source, expected)
            destination = staged / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            destination.chmod(0o644)
            with destination.open("rb") as stream:
                files[name] = hashlib.file_digest(stream, "sha256").hexdigest()
            if expected is not None and files[name] != expected:
                raise ValueError("Bootstrap artifact changed during staging")

        run(["bsdtar", "-xf", str(iso), "-C", str(work), "arch/x86_64/airootfs.sfs"])
        sfs = work / "arch/x86_64/airootfs.sfs"
        _check_digest(sfs, lock["squashfs_sha256"])
        extracted = work / "iso-root"
        run(
            [
                "unsquashfs",
                "-no-progress",
                "-d",
                str(extracted),
                str(sfs),
                "opt/packages",
                "usr/share/pacman/keyrings",
                "usr/share/omarchy-iso",
            ]
        )
        for name, digest in lock["sources"].items():
            _check_digest(extracted / "usr/share/omarchy-iso" / name, digest)
        for name in ("archlinux", "omarchy"):
            for suffix in (".gpg", "-trusted", "-revoked"):
                filename = name + suffix
                copy(
                    extracted / "usr/share/pacman/keyrings" / filename,
                    "keyrings/" + filename,
                    lock["arch_public_roots"].get(filename),
                )
        nodes = list((extracted / "opt/packages").glob("node-v*-linux-x64.tar.gz"))
        if len(nodes) != 1 or not re.fullmatch(
            r"node-v[0-9.]+-linux-x64\.tar\.gz", nodes[0].name
        ):
            raise ValueError(
                "The authenticated ISO must contain exactly one Node payload"
            )
        copy(nodes[0], "omarchy/" + nodes[0].name)
        copy(
            extracted / "usr/share/omarchy-iso/omarchy-base.packages",
            "omarchy/base.packages",
            lock["base_list_sha256"],
        )
        copy(closure / "targets.txt", "omarchy/targets.txt", lock["targets_sha256"])
        for name, digest in lock["databases"].items():
            copy(closure / f"db/sync/{name}.db", f"sync/{name}.db", digest)
        for row in selection.decode("utf-8").splitlines():
            repo, name, version, arch, filename, url, digest = row.split("\t")
            if repo not in lock["databases"] or not re.fullmatch(
                r"[\w.+:@-]+\.pkg\.tar\.(zst|xz|gz)", filename
            ):
                raise ValueError("Invalid locked package identity")
            copy(closure / "cache" / filename, "packages/" + filename, digest)
            signature_file = closure / "cache" / (filename + ".sig")
            if signature_file.exists():
                copy(signature_file, "packages/" + filename + ".sig")
        for filename, digest in lock["packages"].items():
            _check_digest(staged / "packages" / filename, digest)
        data = {
            "provider": provider.id,
            "architecture": platform.arch,
            "channel": "stable",
            "config_sha256": hashlib.sha256(provider.pacman_conf.encode()).hexdigest(),
            "bootstrap": {"revision": "4.0.2", "iso_sha256": lock["iso_sha256"]},
            "files": files,
        }
        encoded = json.dumps(data, sort_keys=True).encode()
        (staged / "manifest.json").write_bytes(encoded)
        manifest_sha256 = hashlib.sha256(encoded).hexdigest()
        inputs = ProviderInputs(staged, manifest_sha256)
        target = work / "signature-check-root"
        target.mkdir()
        with package_transaction(
            platform, provider=provider, inputs=inputs, root=target
        ) as tx:
            targets = (staged / "omarchy/targets.txt").read_text().splitlines()
            from arches_installer.core.install import _check_package_arguments

            _check_package_arguments(targets)
            run(
                ["pacman"]
                + tx.pacman_args
                + ["-Sw", "--noconfirm", "--ask=64"]
                + targets
            )
        staged.rename(output)
    return ProviderInputs(output, manifest_sha256)


_BOOT_HOOKS = (
    "60-mkinitcpio-remove.hook",
    "60-limine-mkinitcpio-remove-pre.hook",
    "80-limine-efi-deploy.hook",
    "90-limine-mkinitcpio-remove-post.hook",
    "90-mkinitcpio-install.hook",
)


@contextmanager
def _defer_hooks(directory, names, backup):
    """Mask only reviewed boot hooks, restoring originals even on partial failure."""
    directory.mkdir(parents=True, exist_ok=True)
    backup.mkdir()
    masked = []
    try:
        for name in names:
            path = directory / name
            if path.exists() or path.is_symlink():
                path.rename(backup / name)
            masked.append(path)
            path.symlink_to("/dev/null")
        yield
    finally:
        for path in reversed(masked):
            path.unlink(missing_ok=True)
            saved = backup / path.name
            if saved.exists() or saved.is_symlink():
                saved.rename(path)


def validate_install_settings(template, hostname, username, password):
    if not re.fullmatch(
        r"[a-z]{2}_[A-Z]{2}\.UTF-8", template.system.locale
    ) or not re.fullmatch(r"[A-Za-z_+-]+(?:/[A-Za-z_+-]+)*", template.system.timezone):
        raise ValueError("Invalid Omarchy locale or timezone")
    if (
        not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username)
        or username == "root"
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}", hostname)
        or not password
        or password.startswith("CHANGE_ME")
        or any(c in password for c in "\n\r\0")
    ):
        raise ValueError(
            "Invalid Omarchy account settings; replace non-production placeholders"
        )


def validate_fresh_target(root, identity=None):
    if (
        root == Path("/")
        or not root.is_absolute()
        or root.resolve() != root
        or not root.is_dir()
        or root.stat().st_uid != os.geteuid()
    ):
        raise ValueError("A separate prepared target is required")
    for path in root.rglob("*"):
        if identity is not None and path == root / "var/lib/arches/provider.json":
            from arches_installer.core.provider import read_provider_identity

            if read_provider_identity(root) != identity | {"status": "installing"}:
                raise ValueError("Omarchy provider identity mismatch")
            continue
        if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.geteuid():
            raise ValueError(
                "Omarchy requires a fresh target; partial provisioning cannot be retried"
            )


def validate_bootstrap_inputs(platform, provider, inputs):
    files = validate_provider_inputs(provider, platform.arch, inputs)
    manifest = json.loads((inputs.root / "manifest.json").read_text())
    lock = load_lock()
    if lock["revision"] != "4.0.2":
        raise ValueError("Unsupported Omarchy bootstrap revision")
    if manifest.get("bootstrap") != {
        "iso_sha256": lock["iso_sha256"],
        "revision": "4.0.2",
    }:
        raise ValueError("Omarchy inputs lack authenticated bootstrap provenance")
    _check_digest(inputs.root / "omarchy/base.packages", lock["base_list_sha256"])
    _check_digest(inputs.root / "omarchy/targets.txt", lock["targets_sha256"])
    for name, digest in lock["databases"].items():
        if files.get(f"sync/{name}.db") != digest:
            raise ValueError("Unsupported Omarchy database snapshot")
    for name, digest in lock["packages"].items():
        if files.get("packages/" + name) != digest:
            raise ValueError("Unsupported Omarchy package revision")
    nodes = [name for name in files if name.startswith("omarchy/node-")]
    if len(nodes) != 1 or "omarchy/targets.txt" not in files:
        raise ValueError(
            "Omarchy inputs require one authenticated Node payload and targets"
        )
    targets = (inputs.root / "omarchy/targets.txt").read_text().splitlines()
    base = [
        p.strip()
        for p in (inputs.root / "omarchy/base.packages").read_text().splitlines()
        if p.strip() and not p.lstrip().startswith("#")
    ]
    from arches_installer.core.install import _check_package_arguments

    _check_package_arguments(targets + base)
    return files, targets, base, nodes


def prepare_stock_engine(tx, files, log=None):
    """Authenticate stock pacman before its first execution or target DB write."""
    archive = "pacman-7.1.0.r9.g54d9411-2-x86_64.pkg.tar.zst"
    tools = tx.work / "stock-engine"
    tools.mkdir(mode=0o700)
    for name in (archive, archive + ".sig"):
        destination = tools / name
        shutil.copyfile(tx.cache / name, destination)
        _check_digest(destination, files["packages/" + name])
    _check_digest(tools / archive, load_lock()["packages"][archive])
    result = run(
        [
            "gpg",
            "--batch",
            "--no-auto-key-retrieve",
            "--homedir",
            str(tx.gpgdir),
            "--status-fd",
            "1",
            "--verify",
            str(tools / (archive + ".sig")),
            str(tools / archive),
        ],
        capture_output=True,
        log=log,
    )
    if not (
        re.search(r"^\[GNUPG:\] VALIDSIG [0-9A-F]{40} ", result.stdout, re.M)
        and re.search(r"^\[GNUPG:\] TRUST_(FULLY|ULTIMATE) ", result.stdout, re.M)
    ):
        raise ValueError("Stock pacman signature is not trusted")
    # Only libalpm accompanies these binaries. Never mix snapshot glibc with the live loader.
    run(
        [
            "bsdtar",
            "-xf",
            str(tools / archive),
            "-C",
            str(tools),
            "usr/bin/pacman",
            "usr/bin/pacman-conf",
            "usr/lib/libalpm.so.16",
            "usr/lib/libalpm.so.16.0.1",
        ],
        log=log,
    )
    bindir = tools / "bin"
    bindir.mkdir(mode=0o700)
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "LANG": "C.UTF-8"}
    for name in ("pacman", "pacman-conf"):
        shim = bindir / name
        shim.write_text(
            "#!/bin/sh\nexec /usr/bin/env "
            + shlex.quote(f"LD_LIBRARY_PATH={tools}/usr/lib")
            + " "
            + shlex.quote(str(tools / "usr/bin" / name))
            + ' "$@"\n'
        )
        shim.chmod(0o700)
        run([name, "--version"], env=env, log=log)
    return env


def _install_omarchy(
    platform,
    template,
    hostname,
    username,
    password,
    *,
    provider,
    inputs,
    root,
    root_uuid,
    disk,
    esp_number,
    log=None,
):
    """Private proof adapter for a fresh, prepared UEFI Btrfs target at /boot.

    No caller in the installation pipeline enables this before Task 6 preflight.
    The caller owns mounting @, @home, @log, @pkg and the separate ESP.
    """
    if provider.id != "omarchy" or provider.lifecycle_owner != "omarchy":
        raise ValueError("Omarchy adapter requires the Omarchy provider")
    validate_selection(
        platform,
        provider,
        template,
        firmware="uefi",
        filesystem="btrfs",
        install_mode="fresh",
    )
    locale, timezone = template.system.locale, template.system.timezone
    validate_install_settings(template, hostname, username, password)
    if (
        not re.fullmatch(r"[0-9a-fA-F-]{36}", root_uuid)
        or not re.fullmatch(r"/dev/[A-Za-z0-9_-]+", disk)
        or type(esp_number) is not int
        or not 1 <= esp_number <= 128
    ):
        raise ValueError("Invalid Omarchy account or boot parameters")
    validate_fresh_target(
        root,
        {
            "provider": "omarchy",
            "platform": platform.name,
            "template": template.name,
            "channel": "stable",
            "bootstrap_revision": "4.0.2",
        },
    )
    files, targets, base, nodes = validate_bootstrap_inputs(platform, provider, inputs)
    # Only this authenticated, fresh target's public paths are installer-owned.
    # Do not widen arbitrary existing directories or the transaction's private leaves.
    for relative in (
        ".",
        "etc",
        "etc/pacman.d",
        "var",
        "var/lib",
        "var/lib/pacman",
        "var/cache",
        "var/cache/pacman",
        "var/cache/pacman/pkg",
        "var/log",
        "opt",
        "home",
        "usr",
        "usr/share",
        "mnt",
    ):
        directory = root / relative
        directory.mkdir(exist_ok=True)
        directory.chmod(0o755)
    environment = [
        "env",
        "-i",
        "PATH=/usr/bin:/bin",
        "HOME=/root",
        f"LANG={locale}",
        "SYSTEMD_IN_CHROOT=1",
        "OMARCHY_USER_NAME=",
        "OMARCHY_USER_EMAIL=",
        "OMARCHY_PATH=/usr/share/omarchy",
        "OMARCHY_INSTALL=/usr/share/omarchy/install",
        f"OMARCHY_INSTALL_USER={username}",
        "OMARCHY_MIRROR=stable",
        "OMARCHY_ISO_REF=quattro",
        "OMARCHY_RUNTIME_PACKAGE=omarchy",
        "OMARCHY_SETTINGS_PACKAGE=omarchy-settings",
        "OMARCHY_NVIM_PACKAGE=omarchy-nvim",
        "OMARCHY_LOG_TO_STDOUT=1",
        "OMARCHY_INSTALL_LOG_FILE=/var/log/omarchy-install.log",
    ]
    chroot = ["arch-chroot", str(root)]
    with (
        package_transaction(
            platform, provider=provider, inputs=inputs, root=root, log=log
        ) as tx,
        ExitStack() as cleanup,
    ):
        # The fresh transaction creates sync after enforcing an empty database.
        (tx.dbpath / "sync").chmod(0o755)
        run(
            ["pacman"] + tx.pacman_args + ["-Sw", "--noconfirm", "--ask=64"] + targets,
            log=log,
        )
        engine_env = prepare_stock_engine(tx, files, log=log)
        hooks = tx.work / "hooks"
        tx.config.write_text(
            tx.config.read_text().replace(
                "[options]\n", f"[options]\nHookDir = {hooks}\n", 1
            )
        )

        def packages(names):
            run(
                [
                    "pacstrap",
                    "-c",
                    "-G",
                    "-M",
                    "-C",
                    str(tx.config),
                    str(root),
                    "--dbpath",
                    str(tx.dbpath),
                    "--gpgdir",
                    str(tx.gpgdir),
                    "--ask=64",
                ]
                + names,
                log=log,
                env=engine_env,
            )

        with _defer_hooks(hooks, _BOOT_HOOKS, tx.work / "bootstrap-hook-backups"):
            packages(
                ["base", "sudo", "linux-firmware", "mkinitcpio", "linux", "btrfs-progs"]
            )
            locale_gen = root / "etc/locale.gen"
            locale_gen.write_text(
                locale_gen.read_text().replace(f"#{locale} UTF-8", f"{locale} UTF-8")
            )
            (root / "etc/locale.conf").write_text(f"LANG={locale}\n")
            run(chroot + ["locale-gen"], log=log)
            packages(["zram-generator"])
            packages(
                [
                    "base-devel",
                    "git",
                    "limine",
                    "efibootmgr",
                    "omarchy-keyring",
                    "omarchy-settings",
                ]
            )
            packages(["lua51", "luarocks"])
            packages(["omarchy-nvim"])
            defaults = root / "usr/share/omarchy/default/limine"
            cmdline = f"root=UUID={root_uuid} zswap.enabled=0 rootflags=subvol=@ rw rootfstype=btrfs"
            for name, text in {
                "etc/default/limine": (defaults / "default.conf")
                .read_text()
                .replace("@@CMDLINE@@", cmdline),
                "etc/kernel/cmdline": cmdline + "\n",
                "boot/limine.conf": (defaults / "limine.conf").read_text(),
                "etc/pacman.d/hooks/99-omarchy-limine.hook": (
                    "[Trigger]\nOperation = Upgrade\nType = Package\nTarget = limine\n\n"
                    "[Action]\nDescription = Deploying Omarchy Limine after upgrade...\nWhen = PostTransaction\n"
                    "Exec = /usr/bin/cp /usr/share/limine/BOOTX64.EFI /boot/EFI/limine/limine_x64.efi\n"
                ),
            }.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            efi = root / "boot/EFI/limine/limine_x64.efi"
            efi.parent.mkdir(parents=True)
            shutil.copy2(root / "usr/share/limine/BOOTX64.EFI", efi)
            run(
                chroot
                + [
                    "efibootmgr",
                    "--create",
                    "--disk",
                    disk,
                    "--part",
                    str(esp_number),
                    "--label",
                    "Limine",
                    "--loader",
                    "\\EFI\\limine\\limine_x64.efi",
                ],
                log=log,
            )
            (root / "home").mkdir(exist_ok=True)
            (root / "home").chmod(0o755)
            run(
                chroot
                + [
                    "useradd",
                    "--create-home",
                    "--user-group",
                    "--groups",
                    "wheel",
                    "--shell",
                    "/usr/bin/bash",
                    "--home-dir",
                    f"/home/{username}",
                    username,
                ],
                log=log,
            )
            # chpasswd's stdin must never enter run() output or command logs.
            subprocess.run(
                chroot + ["chpasswd"],
                input=f"{username}:{password}\n",
                text=True,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            )
            sudo = root / "etc/sudoers.d/10-omarchy-user"
            sudo.parent.mkdir(parents=True, exist_ok=True)
            sudo.write_text(f"{username} ALL=(ALL) ALL\n")
            sudo.chmod(0o440)
            packages(
                [
                    "pipewire",
                    "pipewire-alsa",
                    "pipewire-jack",
                    "pipewire-pulse",
                    "gst-plugin-pipewire",
                    "libpulse",
                    "wireplumber",
                ]
            )
            early = {
                "base-devel",
                "git",
                "limine",
                "efibootmgr",
                "omarchy-keyring",
                "omarchy-settings",
                "lua51",
                "luarocks",
                "omarchy-nvim",
                "omarchy",
            }
            packages(
                list(dict.fromkeys(["omarchy", *(p for p in base if p not in early)]))
            )
        run(chroot + ["systemctl", "enable", "systemd-timesyncd.service"], log=log)
        fstab = run(["genfstab", "-U", str(root)], capture_output=True, log=log).stdout
        records = {
            fields[1]: fields
            for line in fstab.splitlines()
            if (fields := line.split())
            and len(fields) >= 4
            and not line.lstrip().startswith("#")
        }
        if set(records) != {"/", "/home", "/var/log", "/var/cache/pacman/pkg", "/boot"}:
            raise RuntimeError("Omarchy fstab failed the supported layout check")
        for mount, subvolume in (
            ("/", "@"),
            ("/home", "@home"),
            ("/var/log", "@log"),
            ("/var/cache/pacman/pkg", "@pkg"),
        ):
            record = records.get(mount, [])
            if record[:3] != [f"UUID={root_uuid}", mount, "btrfs"] or not {
                f"subvol={subvolume}",
                f"subvol=/{subvolume}",
            }.intersection(record[3].split(",")):
                raise RuntimeError("Omarchy fstab is missing the prepared Btrfs layout")
        if records.get("/boot", [None] * 3)[2] != "vfat":
            raise RuntimeError("Omarchy fstab is missing the prepared ESP")
        (root / "etc/fstab").write_text(fstab)
        (root / "etc/hostname").write_text(hostname + "\n")
        (root / "etc/localtime").unlink(missing_ok=True)
        (root / "etc/localtime").symlink_to(f"/usr/share/zoneinfo/{timezone}")
        # Only the selected snapshot/cache supplies target setup package operations.
        conf = tx.config.read_text().replace(str(root) + "/", "/")
        conf = re.sub(r"^(?:CacheDir|HookDir) = .*\n?", "", conf, flags=re.M)
        conf = conf.replace(
            "[options]\n", "[options]\nCacheDir = /var/cache/pacman/pkg/\n", 1
        )
        target_conf = root / "etc/pacman.conf"
        target_conf.write_text(conf)
        from arches_installer.core.install import _mount_iso_cache_in_target

        _mount_iso_cache_in_target(target_conf, log, transaction=tx)
        run(
            chroot
            + environment
            + ["/usr/bin/omarchy-hibernation-setup", "--force", "--no-rebuild"],
            log=log,
        )
        with _defer_hooks(
            root / "etc/pacman.d/hooks",
            ("90-mkinitcpio-install.hook",),
            tx.work / "system-hook-backups",
        ):
            run(
                chroot
                + environment
                + [
                    "/usr/bin/omarchy-apply-system",
                    "--install-user",
                    username,
                    "--first-install",
                ],
                log=log,
            )
        node = inputs.root / nodes[0]
        for directory in (
            "opt/packages",
            "var/lib/omarchy",
            "var/lib/omarchy/provisioning",
            "var/lib/omarchy/provisioning/packages",
        ):
            destination = root / directory
            destination.mkdir(parents=True, exist_ok=True)
            destination.chmod(0o755)
        for directory in ("opt/packages", "var/lib/omarchy/provisioning/packages"):
            destination = root / directory
            if directory == "opt/packages":
                cleanup.callback((destination / node.name).unlink, missing_ok=True)
            shutil.copyfile(node, destination / node.name)
            (destination / node.name).chmod(0o644)
        run(chroot + ["limine-update"], log=log)
        uki = root / "boot/EFI/Linux/omarchy_linux.efi"
        if not uki.is_file() or not uki.stat().st_size:
            raise RuntimeError("Omarchy final boot image is missing")
        user_env = [e for e in environment if not e.startswith("HOME=")]
        user_env += [
            f"HOME=/home/{username}",
            f"USER={username}",
            f"LOGNAME={username}",
            "SHELL=/usr/bin/bash",
            "OMARCHY_SETUP_CONTEXT=iso-chroot",
        ]
        run(
            ["arch-chroot", "-u", username, str(root)]
            + user_env
            + ["/usr/bin/omarchy-provision-user", "--force", "--first-install"],
            log=log,
        )
        if not (
            root / f"home/{username}/.local/state/omarchy/done/finalize-user"
        ).is_file():
            raise RuntimeError("Omarchy user provisioning did not complete")
        for migration in (root / "usr/share/omarchy/migrations").glob("*.sh"):
            if not (
                root
                / f"home/{username}/.local/state/omarchy/migrations"
                / migration.name
            ).is_file():
                raise RuntimeError("Omarchy shipped migration was not marked complete")
        login = root / "etc/sddm.conf.d/99-omarchy-login.conf"
        login.parent.mkdir(parents=True, exist_ok=True)
        login.write_text(
            "[Theme]\nCurrent=omarchy\n\n[Users]\nRememberLastUser=true\nRememberLastSession=true\n"
        )
        state = root / "var/lib/sddm/state.conf"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(f"[Last]\nSession=omarchy.desktop\nUser={username}\n")
        run(
            chroot
            + ["chown", "sddm:sddm", "/var/lib/sddm", "/var/lib/sddm/state.conf"],
            log=log,
        )
        resolver = root / "etc/resolv.conf"
        resolver.unlink(missing_ok=True)
        resolver.symlink_to("../run/systemd/resolve/stub-resolv.conf")
