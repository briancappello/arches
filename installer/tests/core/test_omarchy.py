"""Omarchy boundaries use temporary files and no executable host fallback."""

import subprocess
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from .test_install import selected_inputs as _selected_inputs

selected_inputs = _selected_inputs


@pytest.fixture(autouse=True)
def blocked_processes(tmp_path, monkeypatch):
    with (
        patch.dict(
            os.environ,
            {
                "PATH": str(tmp_path / "no-executables"),
                "HOME": str(tmp_path),
                "TMPDIR": str(tmp_path),
                "LANG": "C.UTF-8",
            },
            clear=True,
        ),
        patch("subprocess.Popen", side_effect=AssertionError("Unstubbed process")),
        patch("subprocess.run", side_effect=AssertionError("Unstubbed process")),
        patch("arches_installer.core.run._get_log_files", return_value=[]),
    ):
        old_umask = os.umask(0o077)
        try:
            yield
        finally:
            os.umask(old_umask)


def test_process_boundary_is_closed():
    with pytest.raises(AssertionError, match="Unstubbed process"):
        subprocess.run(["makepkg"])


def test_omarchy_template_has_no_arches_policy():
    from pathlib import Path
    from arches_installer.core.template import load_template, resolve_and_merge_modules

    template = resolve_and_merge_modules(
        load_template(
            Path(__file__).resolve().parents[3] / "templates/omarchy-workstation.toml"
        ),
        provider_id="omarchy",
    )
    assert template.providers == ["omarchy"]
    assert template.workload == "workstation"
    assert not template.module_slugs
    assert not template.install.all_packages
    assert not template.ansible.firstboot_roles
    assert not template.services


@pytest.mark.parametrize("provider_id", ["omarchy", "arch", "cachyos"])
@pytest.mark.parametrize("selection", ["explicit", "staged"])
def test_pipeline_rejects_omarchy_before_disk_mutation(
    x86_64_platform, provider_id, selection
):
    from dataclasses import replace
    from arches_installer.core.pipeline import InstallParams, run_install_pipeline
    from arches_installer.core.provider import resolve_provider
    from arches_installer.core.template import InstallTemplate

    params = InstallParams(
        x86_64_platform,
        InstallTemplate.from_dict({}),
        "/dev/vda",
        "proof",
        "proof",
        "secret",
    )
    params.template.providers = ["omarchy"]
    provider = resolve_provider(x86_64_platform, provider_id)
    if selection == "explicit":
        params.provider = provider
    else:
        params.platform = replace(
            x86_64_platform,
            provider_id=provider.id,
            kernel=provider.kernel,
            base_packages=provider.base_packages,
            hardware_detection=provider.hardware_detection,
        )
    params.disk_layout = object()
    with (
        patch("arches_installer.core.pipeline.apply_disk_layout") as disk,
        patch(
            "arches_installer.core.pipeline.install_system",
            side_effect=AssertionError("Unexpected installer boundary"),
        ),
    ):
        with pytest.raises(ValueError, match="preflight"):
            run_install_pipeline(params, log=lambda _: None)
    disk.assert_not_called()


@pytest.mark.parametrize(
    "failure", [None, "digest", "signature", "fingerprint", "revision"]
)
def test_bootstrap_authentication_rejects_bad_artifacts(tmp_path, monkeypatch, failure):
    import hashlib
    from arches_installer.core import omarchy

    iso = tmp_path / "release.iso"
    iso.write_bytes(b"authenticated ISO fixture")
    signature = tmp_path / "release.iso.sig"
    signature.write_bytes(b"signature")
    key = tmp_path / "public.gpg"
    key.write_bytes(b"public key")
    lock = {
        "revision": "4.0.2",
        "iso_sha256": hashlib.sha256(iso.read_bytes()).hexdigest(),
        "key_sha256": hashlib.sha256(key.read_bytes()).hexdigest(),
        "fingerprint": "40DFB630FF42BCFFB047046CF0134EE680CAC571",
    }
    monkeypatch.setattr(omarchy, "load_lock", lambda: lock)
    status = (
        "[GNUPG:] VALIDSIG " + lock["fingerprint"] + " 2026-08-31 1 0 4 0 22 10 00\n"
    )
    if failure == "digest":
        iso.write_bytes(b"tampered")
    elif failure == "signature":
        signature.unlink()
    elif failure == "fingerprint":
        status = status.replace(lock["fingerprint"], "A" * 40)
    elif failure == "revision":
        lock["revision"] = "4.0.3"
    with patch.object(
        omarchy, "run", return_value=subprocess.CompletedProcess([], 0, status)
    ) as verifier:
        if failure:
            with pytest.raises(ValueError):
                omarchy.authenticate_iso(iso, signature, key)
        else:
            assert omarchy.authenticate_iso(iso, signature, key) == lock
            assert verifier.call_args.args[0][-3:] == ["--", str(signature), str(iso)]


@pytest.mark.parametrize("explicit", [False, True])
def test_firstboot_does_not_write_omarchy_policy(tmp_path, x86_64_platform, explicit):
    from arches_installer.core.firstboot import inject_firstboot_service
    from arches_installer.core.provider import resolve_provider
    from arches_installer.core.template import InstallTemplate

    with patch("arches_installer.core.firstboot.MOUNT_ROOT", tmp_path):
        with pytest.raises(ValueError, match="Omarchy"):
            inject_firstboot_service(
                InstallTemplate.from_dict({"meta": {"providers": ["omarchy"]}}),
                "proof",
                provider=resolve_provider(x86_64_platform, "omarchy")
                if explicit
                else None,
            )
    assert list(tmp_path.iterdir()) == []


def test_signature_subprocess_failure_is_not_authorized(tmp_path, monkeypatch):
    import hashlib
    from arches_installer.core import omarchy

    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"fixture")
    digest = hashlib.sha256(b"fixture").hexdigest()
    monkeypatch.setattr(
        omarchy,
        "load_lock",
        lambda: {
            "revision": "4.0.2",
            "iso_sha256": digest,
            "key_sha256": digest,
            "fingerprint": "40DFB630FF42BCFFB047046CF0134EE680CAC571",
        },
    )
    with patch.object(
        omarchy, "run", side_effect=subprocess.CalledProcessError(1, ["gpgv"])
    ):
        with pytest.raises(subprocess.CalledProcessError):
            omarchy.authenticate_iso(artifact, artifact, artifact)


def test_bootstrap_builder_has_native_local_entrypoint():
    from arches_installer.core import omarchy

    assert callable(getattr(omarchy, "stage_bootstrap", None))


def test_private_adapter_exists_but_install_system_guard_stays_closed(x86_64_platform):
    from arches_installer.core import omarchy, install
    from arches_installer.core.provider import resolve_provider
    from arches_installer.core.template import InstallTemplate

    assert callable(getattr(omarchy, "_install_omarchy", None))
    with patch.object(omarchy, "_install_omarchy") as adapter:
        with pytest.raises(ValueError, match="adapter"):
            install.install_system(
                x86_64_platform,
                InstallTemplate.from_dict({}),
                "proof",
                "proof",
                "secret",
                provider=resolve_provider(x86_64_platform, "omarchy"),
            )
    adapter.assert_not_called()


def test_install_system_cannot_install_cachyos_for_omarchy_template(x86_64_platform):
    from arches_installer.core import install
    from arches_installer.core.template import InstallTemplate

    template = InstallTemplate.from_dict({"meta": {"providers": ["omarchy"]}})
    with patch.object(
        install,
        "package_transaction",
        side_effect=AssertionError("Unapproved packages"),
    ) as transaction:
        with pytest.raises(ValueError, match="provider"):
            install.install_system(
                x86_64_platform, template, "proof", "proof", "secret"
            )
    transaction.assert_not_called()


def test_transaction_accepts_explicit_build_root(
    tmp_path, x86_64_platform, selected_inputs
):
    from arches_installer.core import install

    provider, inputs, _, _ = selected_inputs
    root = tmp_path / "build-root"
    root.mkdir()
    with patch.object(install, "_preseed_pacman_keyring"):
        with install.package_transaction(
            x86_64_platform, provider=provider, inputs=inputs, root=root
        ) as tx:
            assert tx.root == root
            assert tx.dbpath.is_relative_to(root)
            assert tx.gpgdir.is_relative_to(root)


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "snapshot",
        "targets",
        "runtime-digest",
        "early",
        "fstab",
        "extra-var",
        "system",
        "missing-boot",
        "user",
    ],
)
def test_private_adapter_order_and_failure_cleanup(
    tmp_path,
    x86_64_platform,
    selected_inputs,
    monkeypatch,
    failure,
):
    import hashlib
    import shutil
    from arches_installer.core import omarchy, install
    from arches_installer.core.template import InstallTemplate

    provider, inputs, manifest, seal = selected_inputs
    root = tmp_path / "target"
    root.mkdir()
    for relative in ("home", "boot", "var/log", "var/cache/pacman/pkg"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    (root / "etc/private").mkdir(parents=True, mode=0o700)
    bootstrap = inputs.root / "omarchy"
    bootstrap.mkdir()
    for name, data in {
        "node-v22.0.0-linux-x64.tar.gz": b"ISO node fixture",
        "base.packages": b"omarchy\n",
        "targets.txt": b"base\nomarchy\n",
    }.items():
        (bootstrap / name).write_bytes(data)
        manifest["files"][f"omarchy/{name}"] = hashlib.sha256(data).hexdigest()
    manifest["bootstrap"] = {"iso_sha256": "fixture-iso", "revision": "4.0.2"}
    seal()
    lock = {
        "iso_sha256": "fixture-iso",
        "revision": "4.0.2",
        "base_list_sha256": hashlib.sha256(b"omarchy\n").hexdigest(),
        "targets_sha256": hashlib.sha256(b"base\nomarchy\n").hexdigest(),
        "databases": {
            name: manifest["files"][f"sync/{name}.db"]
            for name in ("core", "extra", "multilib", "omarchy")
        },
        "packages": {
            "base-2-1-x86_64.pkg.tar.zst": manifest["files"][
                "packages/base-2-1-x86_64.pkg.tar.zst"
            ]
        },
    }
    monkeypatch.setattr(omarchy, "load_lock", lambda: lock)
    invalid_inputs = {
        "snapshot": "sync/core.db",
        "targets": "omarchy/targets.txt",
        "runtime-digest": "packages/base-2-1-x86_64.pkg.tar.zst",
    }
    if failure in invalid_inputs:
        name = invalid_inputs[failure]
        (inputs.root / name).write_bytes(b"base\n")
        manifest["files"][name] = hashlib.sha256(b"base\n").hexdigest()
        seal()
    events = []
    configs = []

    def command(argv, **kwargs):
        assert "secret" not in " ".join(argv)
        if argv[0] == "pacman-key":
            gpgdir = Path(argv[argv.index("--gpgdir") + 1])
            assert gpgdir == root / "etc/pacman.d/gnupg"
            for name in ("pubring.gpg", "trustdb.gpg"):
                (gpgdir / name).write_bytes(b"mock keyring")
            (gpgdir / "private-keys-v1.d").mkdir(exist_ok=True)
            (gpgdir / "private-keys-v1.d/key").write_bytes(b"private fixture")
        elif argv[0] == "pacman":
            events.append("authenticate-closure")
            configs.append(argv[argv.index("--config") + 1])
            assert Path(configs[0]).resolve().is_relative_to(root)
            assert not (Path(configs[0]).parent / "omarchy").exists()
        elif argv[0] == "pacstrap":
            assert kwargs["env"] == {
                "PATH": "/selected-engine:/usr/bin:/bin", "LANG": "C.UTF-8"
            }
            # Check before simulated package extraction can repair any permissions.
            for relative in (
                "etc/pacman.d",
                "var/lib/pacman/sync",
                "var/cache/pacman/pkg",
                "var/log",
                "opt",
                "home",
                "usr/share",
            ):
                path = root / relative
                while path.is_relative_to(root):
                    assert path.stat().st_mode & 0o777 == 0o755, str(path)
                    path = path.parent
            assert Path(configs[0]).parent.stat().st_mode & 0o777 == 0o700
            gpgdir = root / "etc/pacman.d/gnupg"
            assert gpgdir.stat().st_mode & 0o777 == 0o700
            assert (gpgdir / "private-keys-v1.d").stat().st_mode & 0o777 == 0o700
            assert (gpgdir / "private-keys-v1.d/key").stat().st_mode & 0o777 == 0o600
            assert (root / "etc/private").stat().st_mode & 0o777 == 0o700
            assert argv[argv.index("-C") + 1] == configs[0]
            assert argv[argv.index("--dbpath") + 1] == str(root / "var/lib/pacman")
            assert argv[argv.index("--gpgdir") + 1] == str(root / "etc/pacman.d/gnupg")
            config = Path(configs[0]).read_text()
            assert "SigLevel = Required DatabaseOptional" in config
            assert "Never" not in config and "TrustAll" not in config
            packages = argv[argv.index("--ask=64") + 1 :]
            event = (
                "base"
                if "base" in packages
                else (
                    "early"
                    if "omarchy-settings" in packages
                    else (
                        "lua"
                        if "lua51" in packages
                        else ("skel" if packages == ["omarchy-nvim"] else "runtime")
                    )
                )
            )
            if packages == ["zram-generator"]:
                event = "zram"
            if "pipewire" in packages and "omarchy" not in packages:
                event = "audio"
            events.append(event)
            assert "chwd" not in packages and not any("cachyos" in p for p in packages)
            if event == "base":
                (root / "etc").mkdir(exist_ok=True)
                (root / "etc/locale.gen").write_text("#en_US.UTF-8 UTF-8\n")
            if event == "early":
                if failure == "early":
                    raise RuntimeError("early failed")
                skel = root / "etc/skel"
                skel.mkdir(parents=True)
                (skel / ".bashrc").write_text("upstream bash")
                defaults = root / "usr/share/omarchy/default/limine"
                defaults.mkdir(parents=True)
                (defaults / "default.conf").write_text(
                    'ESP_PATH="/boot"\nKERNEL_CMDLINE[default]+="@@CMDLINE@@"\n'
                )
                (defaults / "limine.conf").write_text("Omarchy")
                binary = root / "usr/share/limine/BOOTX64.EFI"
                binary.parent.mkdir(parents=True)
                binary.write_bytes(b"efi")
            if event == "skel":
                (root / "etc/skel/.nvim").write_text("upstream nvim")
        elif "efibootmgr" in argv:
            events.append("initial-boot")
        elif "useradd" in argv:
            events.append("useradd")
            assert "--shell" in argv and "/usr/bin/bash" in argv and "wheel" in argv
            assert (root / "home").stat().st_mode & 0o777 == 0o755
            shutil.copytree(root / "etc/skel", root / "home/proof")
            (root / "home/proof").chmod(0o700)
            (root / "etc/passwd").write_text(
                "proof:x:1000:1000::/home/proof:/usr/bin/bash\n"
            )
        elif "chpasswd" in argv:
            events.append("password")
            assert kwargs["input"] == "proof:secret\n"
        elif "/usr/bin/omarchy-hibernation-setup" in argv:
            events.append("hibernation")
        elif "/usr/bin/omarchy-apply-system" in argv:
            events.append("system")
            assert argv[:2] == ["arch-chroot", str(root)]
            assert argv[-4:] == [
                "/usr/bin/omarchy-apply-system",
                "--install-user",
                "proof",
                "--first-install",
            ]
            assert "SYSTEMD_IN_CHROOT=1" in argv
            if failure == "system":
                raise RuntimeError("system failed")
            (root / "etc/pacman.conf").write_text("upstream required signing policy\n")
            (root / "etc/group").write_text("wheel:x:998:proof\nvideo:x:985:proof\n")
        elif "limine-update" in argv:
            events.append("final-boot")
            if failure != "missing-boot":
                uki = root / "boot/EFI/Linux/omarchy_linux.efi"
                uki.parent.mkdir(parents=True)
                uki.write_bytes(b"generated uki")
        elif "/usr/bin/omarchy-provision-user" in argv:
            events.append("user")
            assert argv[:4] == ["arch-chroot", "-u", "proof", str(root)]
            assert "HOME=/home/proof" in argv and "SHELL=/usr/bin/bash" in argv
            assert (
                root / "opt/packages/node-v22.0.0-linux-x64.tar.gz"
            ).read_bytes() == b"ISO node fixture"
            assert (
                root
                / "var/lib/omarchy/provisioning/packages/node-v22.0.0-linux-x64.tar.gz"
            ).exists()
            for relative in (
                "opt/packages",
                "var/lib/omarchy",
                "var/lib/omarchy/provisioning",
                "var/lib/omarchy/provisioning/packages",
            ):
                path = root / relative
                while path.is_relative_to(root):
                    assert path.stat().st_mode & 0o777 == 0o755, str(path)
                    path = path.parent
            if failure == "user":
                raise RuntimeError("user failed")
            state = root / "home/proof/.local/state/omarchy"
            (state / "done").mkdir(parents=True)
            (state / "done/finalize-user").touch()
            (state / "migrations").mkdir()
        elif argv[0] == "genfstab":
            events.append("fstab")
            records = "\n".join(
                f"UUID=12345678-1234-1234-1234-123456789abc {mount} btrfs subvol=/{subvol} 0 0"
                for mount, subvol in (
                    ("/", "@"),
                    ("/home", "@home"),
                    ("/var/log", "@log"),
                    ("/var/cache/pacman/pkg", "@pkg"),
                )
            )
            if failure == "extra-var":
                records += "\nUUID=12345678-1234-1234-1234-123456789abc /var btrfs subvol=/@var 0 0"
            return subprocess.CompletedProcess(
                argv,
                0,
                ""
                if failure == "fstab"
                else records + "\nUUID=esp /boot vfat umask=0077 0 2\n",
            )
        elif argv[2:] == ["systemctl", "enable", "systemd-timesyncd.service"]:
            events.append("ntp")
        elif argv[0] in ("mount", "umount") or argv[2:] == ["locale-gen"]:
            pass
        elif argv[2:] == [
            "chown",
            "sddm:sddm",
            "/var/lib/sddm",
            "/var/lib/sddm/state.conf",
        ]:
            pass
        else:
            raise AssertionError(f"Unexpected adapter command: {argv}")
        return subprocess.CompletedProcess(argv, 0, "")

    template = InstallTemplate.from_dict(
        {
            "meta": {"workload": "workstation", "providers": ["omarchy"]},
            "system": {"timezone": "UTC"},
        }
    )
    def selected_engine(tx, files, log=None):
        assert events == ["authenticate-closure"]
        return {"PATH": "/selected-engine:/usr/bin:/bin", "LANG": "C.UTF-8"}

    with (
        patch.object(omarchy, "prepare_stock_engine", side_effect=selected_engine) as engine,
        patch.object(omarchy, "run", side_effect=command),
        patch.object(omarchy, "subprocess") as process,
        patch.object(install, "run", side_effect=command),
    ):
        process.run.side_effect = command
        args = dict(
            provider=provider,
            inputs=inputs,
            root=root,
            root_uuid="12345678-1234-1234-1234-123456789abc",
            disk="/dev/vda",
            esp_number=1,
        )
        if failure:
            with pytest.raises(
                (RuntimeError, ValueError),
                match="(?i)failed|missing|unsupported|digest",
            ):
                omarchy._install_omarchy(
                    x86_64_platform, template, "proof", "proof", "secret", **args
                )
        else:
            omarchy._install_omarchy(
                x86_64_platform, template, "proof", "proof", "secret", **args
            )
    hooks = root / "etc/pacman.d/hooks"
    assert not any(
        p.is_symlink() and str(p.readlink()) == "/dev/null" for p in hooks.glob("*")
    )
    assert not list(root.glob(".arches-packages-*"))
    assert not (root / "opt/packages/node-v22.0.0-linux-x64.tar.gz").exists()
    if failure in invalid_inputs:
        engine.assert_not_called()
        assert events == []
        assert not any(p.is_file() for p in root.rglob("*"))
    elif failure in ("early", "fstab", "extra-var", "system"):
        assert "final-boot" not in events and "user" not in events
    elif failure == "user":
        assert not (
            root / "home/proof/.local/state/omarchy/done/finalize-user"
        ).exists()
    elif failure == "missing-boot":
        assert "user" not in events
    else:
        assert events == [
            "authenticate-closure",
            "base",
            "zram",
            "early",
            "lua",
            "skel",
            "initial-boot",
            "useradd",
            "password",
            "audio",
            "runtime",
            "ntp",
            "fstab",
            "hibernation",
            "system",
            "final-boot",
            "user",
        ]
        assert (root / "home/proof/.bashrc").read_text() == "upstream bash"
        assert (root / "home/proof/.nvim").read_text() == "upstream nvim"
        assert (root / "home/proof").stat().st_mode & 0o777 == 0o700
        assert (
            root / "etc/sudoers.d/10-omarchy-user"
        ).read_text() == "proof ALL=(ALL) ALL\n"
        assert (
            root / "etc/pacman.conf"
        ).read_text() == "upstream required signing policy\n"
        assert (root / "etc/locale.conf").read_text() == "LANG=en_US.UTF-8\n"
        assert (
            root / "etc/localtime"
        ).readlink().as_posix() == "/usr/share/zoneinfo/UTC"
        assert (root / "etc/resolv.conf").is_symlink()
        assert (
            root / "etc/passwd"
        ).read_text() == "proof:x:1000:1000::/home/proof:/usr/bin/bash\n"
        assert (
            root / "etc/group"
        ).read_text() == "wheel:x:998:proof\nvideo:x:985:proof\n"
    if failure and failure not in invalid_inputs:
        engine.assert_called_once()
        count = len(events)
        with pytest.raises(ValueError, match="fresh target"):
            omarchy._install_omarchy(
                x86_64_platform, template, "proof", "proof", "secret", **args
            )
        assert len(events) == count


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "signature",
        "node-symlink",
        "node-missing",
        "manifest",
        "cache-digest",
        "unprotected-output",
        "tsv-race",
    ],
)
def test_native_staging_authenticates_before_returning_manifest(
    tmp_path,
    x86_64_platform,
    selected_inputs,
    monkeypatch,
    failure,
):
    import hashlib
    import shutil
    from arches_installer.core import omarchy, install
    from arches_installer.core.provider import validate_provider_inputs

    provider, old_inputs, manifest, _ = selected_inputs
    closure = tmp_path / "closure"
    closure.mkdir()
    shutil.copytree(old_inputs.root / "packages", closure / "cache")
    shutil.copytree(old_inputs.root / "sync", closure / "db/sync")
    archive = "base-2-1-x86_64.pkg.tar.zst"
    checksum = manifest["files"]["packages/" + archive]
    selection = f"core\tbase\t2-1\tx86_64\t{archive}\thttps://stable-mirror.omarchy.org/core/os/x86_64/{archive}\t{checksum}\n"
    (closure / "selected-manifest.tsv").write_text(selection)
    (closure / "targets.txt").write_text("base\n")

    def digest(data):
        return hashlib.sha256(data).hexdigest()

    lock = {
        "revision": "4.0.2",
        "iso_sha256": "fixture-iso",
        "selected_manifest_sha256": digest(selection.encode()),
        "targets_sha256": digest(b"base\n"),
        "base_list_sha256": digest(b"base\n"),
        "squashfs_sha256": digest(b"squashfs fixture"),
        "sources": {},
        "databases": {
            name: manifest["files"][f"sync/{name}.db"]
            for name in ("core", "extra", "multilib", "omarchy")
        },
        "arch_public_roots": {},
        "packages": {},
    }
    monkeypatch.setattr(omarchy, "authenticate_iso", lambda *args: lock)
    output = tmp_path / "staged"
    if failure == "manifest":
        (closure / "selected-manifest.tsv").write_text("tampered")
    if failure == "cache-digest":
        (closure / "cache" / archive).write_bytes(b"tampered")
    if failure == "unprotected-output":
        parent = tmp_path / "unprotected"
        parent.mkdir(mode=0o777)
        parent.chmod(0o777)
        output = parent / "staged"
    events = []

    def command(argv, **kwargs):
        events.append(argv[0])
        if argv[0] == "bsdtar":
            if failure == "tsv-race":
                closure.chmod(0o777)
                injected = "injected-1-1-x86_64.pkg.tar.zst"
                (closure / "cache" / injected).write_bytes(b"not in selected targets")
                (closure / "selected-manifest.tsv").write_text(
                    selection
                    + f"extra\tinjected\t1-1\tx86_64\t{injected}\thttps://example.invalid/{injected}\t{digest(b'not in selected targets')}\n"
                )
            destination = Path(argv[argv.index("-C") + 1])
            sfs = destination / "arch/x86_64/airootfs.sfs"
            sfs.parent.mkdir(parents=True)
            sfs.write_bytes(b"squashfs fixture")
        elif argv[0] == "unsquashfs":
            destination = Path(argv[argv.index("-d") + 1])
            shutil.copytree(
                old_inputs.root / "keyrings", destination / "usr/share/pacman/keyrings"
            )
            node = destination / "opt/packages/node-v22.0.0-linux-x64.tar.gz"
            node.parent.mkdir(parents=True)
            node.write_bytes(b"authenticated node")
            if failure in ("node-missing", "node-symlink"):
                node.unlink()
            if failure == "node-symlink":
                outside = tmp_path / "outside-node"
                outside.write_bytes(b"not from the signed image")
                node.symlink_to(outside)
            base = destination / "usr/share/omarchy-iso/omarchy-base.packages"
            base.parent.mkdir(parents=True)
            base.write_text("base\n")
        elif argv[0] == "pacman":
            assert "-Sw" in argv and "--ask=64" in argv
            if failure == "signature":
                raise subprocess.CalledProcessError(1, argv)
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, 0, "")

    with (
        patch.object(omarchy, "run", side_effect=command),
        patch.object(install, "_preseed_pacman_keyring"),
    ):
        if failure and failure != "tsv-race":
            with pytest.raises(
                subprocess.CalledProcessError if failure == "signature" else ValueError
            ):
                omarchy.stage_bootstrap(
                    x86_64_platform,
                    provider,
                    tmp_path / "iso",
                    tmp_path / "sig",
                    tmp_path / "key",
                    closure,
                    output,
                )
            assert not output.exists()
        else:
            inputs = omarchy.stage_bootstrap(
                x86_64_platform,
                provider,
                tmp_path / "iso",
                tmp_path / "sig",
                tmp_path / "key",
                closure,
                output,
            )
            files = validate_provider_inputs(provider, "x86_64", inputs)
            assert "omarchy/node-v22.0.0-linux-x64.tar.gz" in files
            assert "packages/" + archive in files
            assert not any("injected" in name for name in files)
            assert not list((inputs.root / "packages").glob("injected-*"))
    if failure in ("manifest", "unprotected-output"):
        assert events == []
    elif failure in ("node-missing", "node-symlink", "cache-digest"):
        assert events == ["bsdtar", "unsquashfs"]
    else:
        assert events == ["bsdtar", "unsquashfs", "pacman"]


@pytest.mark.parametrize("fail", [False, True])
def test_boot_masks_restore_existing_hook_bytes(tmp_path, fail):
    from arches_installer.core.omarchy import _defer_hooks

    hooks = tmp_path / "hooks"
    hooks.mkdir()
    original = hooks / "90-mkinitcpio-install.hook"
    original.write_bytes(b"upstream hook")
    try:
        with _defer_hooks(hooks, (original.name,), tmp_path / "backups"):
            assert original.is_symlink()
            if fail:
                raise RuntimeError("failure")
    except RuntimeError:
        assert fail
    assert not original.is_symlink()
    assert original.read_bytes() == b"upstream hook"


@pytest.mark.parametrize(
    "unsafe", ["partial", "symlink", "foreign-root", "foreign-directory"]
)
def test_adapter_rejects_target_before_any_package_command(
    tmp_path, x86_64_platform, selected_inputs, unsafe, monkeypatch
):
    from arches_installer.core import omarchy
    from arches_installer.core.template import InstallTemplate

    provider, inputs, _, _ = selected_inputs
    target = tmp_path / "target"
    target.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if unsafe == "symlink":
        (target / "etc").symlink_to(outside)
    elif unsafe == "partial":
        (target / "etc").mkdir()
        (target / "etc/passwd").write_text("partially installed")
    else:
        (target / "etc").mkdir()
        foreign = target if unsafe == "foreign-root" else target / "etc"
        original_stat = Path.stat

        def foreign_stat(path, *args, **kwargs):
            info = original_stat(path, *args, **kwargs)
            if path == foreign:
                fields = list(info)
                fields[4] = os.geteuid() + 1
                return os.stat_result(fields)
            return info

        monkeypatch.setattr(Path, "stat", foreign_stat)
    with (
        patch.object(omarchy, "run") as command,
        patch.object(
            omarchy,
            "validate_provider_inputs",
            side_effect=AssertionError("Unexpected input validation"),
        ),
    ):
        with pytest.raises(ValueError, match="target"):
            omarchy._install_omarchy(
                x86_64_platform,
                InstallTemplate.from_dict({"meta": {"workload": "workstation"}}),
                "proof",
                "proof",
                "secret",
                provider=provider,
                inputs=inputs,
                root=target,
                root_uuid="12345678-1234-1234-1234-123456789abc",
                disk="/dev/vda",
                esp_number=1,
            )
    command.assert_not_called()
    assert list(outside.iterdir()) == []
    assert target.stat().st_mode & 0o777 == 0o700
    if unsafe == "partial":
        assert (target / "etc").stat().st_mode & 0o777 == 0o700
        assert (target / "etc/passwd").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("command", ["omarchy-bootstrap", "target-packages"])
def test_build_cli_exposes_authentication_not_installation(
    tmp_path, monkeypatch, capsys, command
):
    import importlib.util
    import sys
    from pathlib import Path
    from arches_installer.core import omarchy
    from arches_installer.core.provider import ProviderInputs
    from arches_installer.core import provider, module, gpu_stacks

    for owner, name in (
        (provider, "_PROVIDERS_SEARCH"),
        (provider, "_PLATFORMS_DIR"),
        (module, "_MODULES_SEARCH"),
        (gpu_stacks, "_STACKS_SEARCH"),
    ):
        monkeypatch.setattr(owner, name, getattr(owner, name))

    path = Path(__file__).resolve().parents[3] / "scripts/iso-config.py"
    spec = importlib.util.spec_from_file_location("task5_iso_config", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setenv("ARCHES_PROVIDER", "omarchy")
    monkeypatch.setenv("ARCHES_TEMPLATE", "omarchy-workstation")
    args = [str(path), command, str(path.parents[1] / "platforms/x86-64/platform.toml")]
    if command == "omarchy-bootstrap":
        args += [
            str(tmp_path / name) for name in ("iso", "sig", "key", "closure", "output")
        ]
    monkeypatch.setattr(sys, "argv", args)
    with patch.object(
        omarchy,
        "stage_bootstrap",
        return_value=ProviderInputs(tmp_path / "output", "a" * 64),
    ) as stage:
        cli.main()
    if command == "omarchy-bootstrap":
        stage.assert_called_once()
        assert stage.call_args.args[1].id == "omarchy"
    else:
        stage.assert_not_called()
        output = capsys.readouterr().out.splitlines()
        assert "linux" in output and "omarchy-settings" in output
        assert not any("cachyos" in p or p == "chwd" for p in output)


def test_upstream_failure_never_dispatches_automatic_success_reboot(x86_64_platform):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from arches_installer.core.template import InstallTemplate
    from arches_installer.tui.progress import InstallProgressScreen

    app = SimpleNamespace(
        selected_template=InstallTemplate.from_dict({}),
        selected_device="/dev/vda",
        platform=x86_64_platform,
        partition_mode="auto",
        selected_layout=None,
        hostname="proof",
        username="proof",
        password="secret",
        raid_config=None,
        hardware_config=None,
        ansible_vars=None,
        auto_install=True,
        auto_reboot=True,
        install_success=False,
        provider=None,
        inputs=None,
        call_from_thread=lambda callback: callback(),
    )
    screen = SimpleNamespace(
        app=app,
        log_msg=MagicMock(),
        _on_install_complete=MagicMock(),
        _on_install_failed_auto=MagicMock(),
    )
    with patch(
        "arches_installer.tui.progress.run_install_pipeline",
        side_effect=RuntimeError("upstream setup failed"),
    ):
        InstallProgressScreen._run_install(screen)
    assert app.install_success is False
    screen._on_install_complete.assert_not_called()
    screen._on_install_failed_auto.assert_called_once()
    subprocess.run.assert_not_called()


def test_post_system_pipeline_does_not_overwrite_omarchy_policy(x86_64_platform):
    from contextlib import ExitStack
    from unittest.mock import MagicMock
    from arches_installer.core import pipeline
    from arches_installer.core.disk import PartitionMap
    from arches_installer.core.provider import resolve_provider
    from arches_installer.core.template import InstallTemplate

    provider = resolve_provider(x86_64_platform, "omarchy")
    params = pipeline.InstallParams(
        x86_64_platform,
        InstallTemplate.from_dict({}),
        "/dev/vda",
        "proof",
        "proof",
        "secret",
        provider=provider,
    )
    params.resolved_disk_roles = MagicMock()
    parts = PartitionMap(
        esp="/dev/vda1",
        root="/dev/vda2",
        root_filesystem="btrfs",
        swap_partitions=["/dev/vda3"],
    )
    with ExitStack() as stack:
        forbidden = [
            stack.enter_context(patch.object(pipeline, name))
            for name in (
                "install_bootloader",
                "setup_snapshots",
                "inject_firstboot_service",
                "append_swap_fstab_entries",
                "write_disk_roles_state",
                "mirror_esp",
            )
        ]
        stack.enter_context(patch.object(pipeline, "_persist_install_log"))
        assert (
            pipeline._finish_install(params, parts, provider, lambda _: None) == parts
        )
    for command in forbidden:
        command.assert_not_called()
