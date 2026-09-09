"""Task 6 boundaries: no inherited environment or executable host fallback."""

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from arches_installer.core import provider as policy
from arches_installer.core.auto import AutoInstallConfig
from arches_installer.core import pipeline as _pipeline
from .test_install import selected_inputs as selected_inputs

_real_disk_identity_check = getattr(_pipeline, "check_existing_disk_identity", None)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "STAGED_ROOT", tmp_path / "stage", raising=False)
    with (
        patch.dict(
            os.environ,
            {"PATH": str(tmp_path / "bin"), "HOME": str(tmp_path), "LANG": "C.UTF-8"},
            clear=True,
        ),
        patch("subprocess.run", side_effect=AssertionError("Unstubbed subprocess")),
        patch("subprocess.Popen", side_effect=AssertionError("Unstubbed subprocess")),
        patch("arches_installer.core.run._get_log_files", return_value=[]),
    ):
        try:
            yield
        finally:
            # Restore test-level patches before closing the process deny boundary.
            monkeypatch.undo()


def stage(platform, selected):
    provider = policy.resolve_provider(platform, selected)
    policy.STAGED_ROOT.mkdir()
    (policy.STAGED_ROOT / "target-provider.json").write_text(
        json.dumps(
            {
                "provider": selected,
                "platform": platform.name,
                "architecture": platform.arch,
                "channel": "stable",
                "config_sha256": hashlib.sha256(
                    provider.pacman_conf.encode()
                ).hexdigest(),
            }
        )
    )
    return provider


@pytest.mark.parametrize(
    "cli,config,staged,expected",
    [
        (None, None, None, "cachyos"),
        ("cachyos", None, None, "cachyos"),
        (None, None, "omarchy", "omarchy"),
        (None, "omarchy", "omarchy", "omarchy"),
        ("omarchy", "cachyos", "omarchy", "omarchy"),
        ("cachyos", "omarchy", "cachyos", "cachyos"),
        ("omarchy", None, "cachyos", None),
        ("bogus", None, None, None),
        (None, "bogus", None, None),
        ("", None, None, None),
    ],
)
def test_selection_precedence(x86_64_platform, cli, config, staged, expected):
    if staged:
        stage(x86_64_platform, staged)
    if expected is None:
        with pytest.raises(ValueError):
            policy.resolve_install_provider(x86_64_platform, cli, config)
    else:
        selected, inputs = policy.resolve_install_provider(x86_64_platform, cli, config)
        assert selected.id == expected
        assert inputs is None


@pytest.mark.parametrize("fault", ["missing", "digest", "escape", "writable"])
def test_staged_pin_cannot_come_from_cache(x86_64_platform, fault):
    stage(x86_64_platform, "omarchy")
    path = policy.STAGED_ROOT / "target-provider.json"
    metadata = json.loads(path.read_text())
    if fault != "missing":
        metadata["manifest_sha256"] = "not-a-digest" if fault == "digest" else "a" * 64
        metadata["inputs"] = (
            "../../cache"
            if fault == "escape"
            else "provider-inputs/omarchy/" + "a" * 64
        )
    path.write_text(json.dumps(metadata))
    if fault == "writable":
        path.chmod(0o666)
    if fault == "missing":
        assert policy.resolve_install_provider(x86_64_platform)[1] is None
    else:
        with pytest.raises(ValueError):
            policy.resolve_install_provider(x86_64_platform)


@pytest.mark.parametrize("status", ["installing", "failed", "complete"])
def test_identity_roundtrip_and_rerun_rejection(tmp_path, status):
    root = tmp_path / "target"
    root.mkdir()
    identity = dict(
        provider="omarchy",
        platform="x86-64",
        template="Omarchy",
        channel="stable",
        bootstrap_revision="4.0.2",
    )
    policy.write_provider_identity(root, identity, "installing")
    if status != "installing":
        policy.write_provider_identity(root, identity, status)
    assert policy.read_provider_identity(root) == identity | {"status": status}
    with pytest.raises(ValueError, match="identity|existing"):
        policy.check_provider_identity(root, identity)
    with pytest.raises(ValueError):
        policy.write_provider_identity(
            root, identity | {"provider": "cachyos"}, "complete"
        )
    assert not list(root.rglob("*.tmp"))


@pytest.mark.parametrize(
    "content", ["{", "[]", '{"provider":"omarchy"}', '{"status":"complete"}']
)
def test_identity_tampering_fails_closed(tmp_path, content):
    path = tmp_path / "var/lib/arches/provider.json"
    path.parent.mkdir(parents=True)
    path.write_text(content)
    with pytest.raises(ValueError):
        policy.read_provider_identity(tmp_path)


def test_legacy_arm_omission(aarch64_platform):
    provider, inputs = policy.resolve_install_provider(aarch64_platform)
    assert provider.id == "alarm" and inputs is None


def test_identity_symlink_never_writes_outside(tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "var").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        policy.write_provider_identity(root, {}, "installing")
    assert not list(outside.iterdir())


@pytest.fixture
def omarchy_params(x86_64_platform, tmp_path, monkeypatch):
    from arches_installer.core import pipeline
    from arches_installer.core.disk_layout import (
        DiskLayout,
        PartitionSpec,
        SubvolumeSpec,
    )
    from arches_installer.core.template import InstallTemplate

    selected = stage(x86_64_platform, "omarchy")
    root = tmp_path / "target"
    root.mkdir()
    monkeypatch.setattr(pipeline, "MOUNT_ROOT", root, raising=False)
    layout = DiskLayout(
        "Omarchy",
        "",
        ["limine"],
        [
            PartitionSpec("2G", "vfat", "/boot"),
            PartitionSpec(
                "*",
                "btrfs",
                "/",
                mount_options="compress=zstd:1,noatime",
                subvolumes=[
                    SubvolumeSpec("@", "/"),
                    SubvolumeSpec("@home", "/home"),
                    SubvolumeSpec("@log", "/var/log"),
                    SubvolumeSpec("@pkg", "/var/cache/pacman/pkg"),
                ],
            ),
        ],
    )
    return pipeline.InstallParams(
        x86_64_platform,
        InstallTemplate.from_dict(
            {
                "meta": {
                    "name": "Omarchy",
                    "providers": ["omarchy"],
                    "workload": "workstation",
                }
            }
        ),
        "/dev/vda",
        "test",
        "test",
        "fixture-only",
        provider=selected,
        disk_layout=layout,
    )


@pytest.mark.parametrize("resolved", [False, True])
@pytest.mark.parametrize(
    "fault",
    [
        "firmware",
        "bootloader",
        "kernel",
        "filesystem",
        "layout",
        "esp",
        "raid",
        "hardware",
        "modules",
        "mode",
        "inputs",
        "arch",
        "identity",
        "password",
        "username",
        "locale",
    ],
)
def test_omarchy_rejections_precede_all_disks(
    omarchy_params, monkeypatch, fault, resolved
):
    from arches_installer.core import pipeline
    from arches_installer.core.disk_layout import SubvolumeSpec
    from arches_installer.core.hardware import HardwareConfig

    params = omarchy_params
    if resolved:
        from types import SimpleNamespace

        params.resolved_disk_roles = SimpleNamespace(
            assignments={"primary": [SimpleNamespace(path="/dev/vda")]}
        )
    if fault == "kernel":
        from arches_installer.core.platform import KernelConfig, KernelVariant

        params.platform = replace(
            params.platform,
            provider_id="omarchy",
            kernel=KernelConfig(
                variants=[
                    KernelVariant(package="linux-other", headers="linux-other-headers")
                ]
            ),
            base_packages=params.provider.base_packages,
            hardware_detection=params.provider.hardware_detection,
        )
        params.provider = policy.resolve_provider(params.platform)
    elif fault == "bootloader":
        params.platform.bootloader.type = "grub"
    elif fault == "filesystem":
        params.disk_layout.partitions[1].filesystem = "ext4"
    elif fault == "layout":
        params.disk_layout.partitions[1].subvolumes.append(
            SubvolumeSpec("@var", "/var")
        )
    elif fault == "esp":
        params.disk_layout.partitions[0].size = "1G"
    elif fault == "raid":
        params.raid_config = object()
    elif fault == "hardware":
        params.hardware = HardwareConfig(machine=object())
    elif fault == "modules":
        params.template.module_slugs = ["base"]
    elif fault == "mode":
        params.install_mode = "replace"
    elif fault == "arch":
        params.provider = policy.resolve_provider(params.platform, "arch")
    elif fault == "identity":
        path = pipeline.MOUNT_ROOT / "var/lib/arches/provider.json"
        path.parent.mkdir(parents=True)
        path.write_text("{")
    elif fault == "password":
        params.password = "CHANGE_ME_NOT_A_PRODUCTION_PASSWORD"
    elif fault == "username":
        params.username = "root"
    elif fault == "locale":
        params.template.system.locale = "invalid"
    monkeypatch.setattr(
        pipeline,
        "detect_firmware",
        lambda: "bios" if fault == "firmware" else "uefi",
        raising=False,
    )
    with (
        patch.object(pipeline, "apply_disk_layout") as single,
        patch.object(pipeline, "apply_disk_layout_resolved") as multi,
        patch.object(pipeline, "setup_raid_mdadm") as raid,
        patch.object(pipeline, "install_system") as install,
    ):
        expected = {
            "firmware": "firmware",
            "bootloader": "Limine",
            "kernel": "kernel",
            "filesystem": "filesystem",
            "layout": "bindings",
            "esp": "2 GiB",
            "raid": "RAID",
            "hardware": "hardware",
            "modules": "owns the workload",
            "mode": "install mode",
            "inputs": "authenticated staged",
            "arch": "not staged",
            "identity": "identity",
            "password": "account",
            "username": "account",
            "locale": "locale",
        }[fault]
        with pytest.raises(ValueError, match=expected):
            pipeline.run_install_pipeline(params, lambda _: None)
        for call in (single, multi, raid, install):
            call.assert_not_called()


def test_preflight_hook_precedes_disks(omarchy_params):
    from arches_installer.core import pipeline

    with (
        patch.object(
            pipeline,
            "preflight_provider",
            side_effect=RuntimeError("provider unavailable"),
        ) as preflight,
        patch.object(pipeline, "apply_disk_layout") as disk,
    ):
        with pytest.raises(RuntimeError, match="provider unavailable"):
            pipeline.run_install_pipeline(omarchy_params, lambda _: None)
        preflight.assert_called_once_with(omarchy_params)
        disk.assert_not_called()


@pytest.mark.parametrize("failure", [False, True])
def test_dispatch_identity_and_failure_status(omarchy_params, failure):
    from arches_installer.core import pipeline, omarchy
    from arches_installer.core.disk import PartitionMap

    params = omarchy_params
    params.inputs = policy.ProviderInputs(policy.STAGED_ROOT / "cache", "a" * 64)
    parts = PartitionMap(esp="/dev/vda1", root="/dev/vda2")
    messages = []

    def adapter(*args, **kwargs):
        assert kwargs["provider"] is params.provider
        assert kwargs["inputs"] is params.inputs
        assert (
            policy.read_provider_identity(pipeline.MOUNT_ROOT)["status"] == "installing"
        )
        if failure:
            raise RuntimeError("package failure")

    with (
        patch.object(pipeline, "preflight_provider", create=True),
        patch.object(
            pipeline, "inspect_omarchy_mounts", return_value="a" * 36, create=True
        ),
        patch.object(pipeline, "apply_disk_layout", return_value=parts),
        patch.object(pipeline, "_persist_install_log"),
        patch.object(omarchy, "_install_omarchy", side_effect=adapter) as dispatch,
        patch.object(pipeline, "install_system") as legacy,
    ):
        if failure:
            with pytest.raises(RuntimeError, match="package failure"):
                pipeline.run_install_pipeline(params, messages.append)
        else:
            assert pipeline.run_install_pipeline(params, messages.append) is parts
        dispatch.assert_called_once()
        legacy.assert_not_called()
    assert policy.read_provider_identity(pipeline.MOUNT_ROOT)["status"] == (
        "failed" if failure else "complete"
    )
    assert any("Installation complete" in line for line in messages) is not failure


@pytest.mark.parametrize(
    "cli,configured,expected",
    [
        (None, None, "cachyos"),
        ("cachyos", "omarchy", "cachyos"),
        (None, "bogus", None),
        ("bogus", None, None),
    ],
)
def test_auto_cli_propagation_and_summary(
    auto_config_file, x86_64_platform, cli, configured, expected, monkeypatch, capsys
):
    from arches_installer.__main__ import main

    if configured:
        auto_config_file.write_text(
            auto_config_file.read_text().replace(
                "[install]", f'[install]\nprovider = "{configured}"'
            )
        )
    argv = ["arches-install", "--auto", str(auto_config_file), "--dry-run"]
    if cli is not None:
        argv += ["--provider", cli]
    monkeypatch.setattr("sys.argv", argv)
    with patch(
        "arches_installer.__main__._load_platform", return_value=x86_64_platform
    ):
        assert main() == (0 if expected else 1)
    output = capsys.readouterr().out
    assert "testpass" not in output
    if expected:
        for field in (
            "Provider:",
            "Channel:",
            "Support:",
            "Inputs:",
            "Kernel:",
            "Template:",
        ):
            assert field in output


def test_tui_carries_same_selection(x86_64_platform):
    from arches_installer.__main__ import _run_tui
    from arches_installer.tui.app import ArchesApp

    selected, inputs = policy.resolve_install_provider(x86_64_platform)
    app = ArchesApp(platform=x86_64_platform, provider=selected, inputs=inputs)
    assert app.provider is selected and app.inputs is inputs
    with (
        patch("arches_installer.__main__._load_platform", return_value=x86_64_platform),
        patch("arches_installer.tui.app.ArchesApp") as app_class,
    ):
        assert _run_tui(provider_id="cachyos") == 0
        assert app_class.call_args.kwargs["provider"].id == "cachyos"


@pytest.mark.parametrize("mode", ["alongside", "replace"])
def test_host_rejects_before_even_cleanup(omarchy_params, mode):
    from arches_installer.core import host_install

    params = omarchy_params
    config = host_install.HostInstallConfig(
        params.template,
        "test",
        "test",
        "fixture",
        "/dev/vda2",
        "/dev/vda1",
        mode,
        "@arches",
        False,
        False,
    )
    config.provider = params.provider
    with (
        patch.object(host_install, "cleanup_mounts") as cleanup,
        patch.object(host_install, "prepare_subvolume") as prepare,
    ):
        assert host_install.run_host_install(params.platform, config) == 1
        cleanup.assert_not_called()
        prepare.assert_not_called()


def test_module_free_discovery_and_explicit_layout():
    from arches_installer.core.template import discover_templates
    from arches_installer.core.auto import AutoInstallConfig

    root = Path(__file__).resolve().parents[3]
    templates = discover_templates(provider_id="omarchy")
    assert [t.providers for t in templates] == [["omarchy"]]
    config = AutoInstallConfig.from_file(root / "templates/auto-install-omarchy.toml")
    assert config.provider_id == "omarchy"
    assert config.password.startswith("CHANGE_ME")
    assert [s.name for s in config.disk_layout.partitions[1].subvolumes] == [
        "@",
        "@home",
        "@log",
        "@pkg",
    ]


def test_runtime_rescan_reads_target_identity_not_iso(tmp_path):
    from arches_installer.core.hardware_rescan import run_rescan

    identity = dict(
        provider="omarchy",
        platform="x86-64",
        template="Omarchy",
        channel="stable",
        bootstrap_revision="4.0.2",
    )
    policy.write_provider_identity(tmp_path, identity, "installing")
    policy.write_provider_identity(tmp_path, identity, "complete")
    with patch(
        "arches_installer.core.hardware.detect_pci_ids",
        side_effect=AssertionError("Nonowned hardware phase"),
    ):
        assert run_rescan(target_root=tmp_path) == 0


@pytest.mark.parametrize(
    "fault",
    [None, "virtual", "artifact", "trust", "packages", "signature", "resolver", "engine", "hardware", "gpu"],
)
def test_authenticated_preflight_uses_only_scratch(
    omarchy_params, selected_inputs, tmp_path, monkeypatch, fault
):
    import shutil
    import subprocess
    import tempfile
    from types import SimpleNamespace
    from arches_installer.core import pipeline, omarchy, install

    params = omarchy_params
    _, source, manifest, _ = selected_inputs
    path = policy.STAGED_ROOT / "target-provider.json"
    metadata = json.loads(path.read_text())
    relative = "provider-inputs/omarchy/" + source.manifest_sha256
    destination = policy.STAGED_ROOT / relative
    shutil.copytree(source.root, destination)
    path.write_text(
        json.dumps(
            metadata | {"inputs": relative, "manifest_sha256": source.manifest_sha256}
        )
    )
    if fault == "artifact":
        (destination / "sync/core.db").write_bytes(b"changed")
    actual_temporary_directory = tempfile.TemporaryDirectory
    scratch_roots = []

    def scratch(**kwargs):
        if kwargs.get("dir") == "/run":
            kwargs["dir"] = tmp_path
        result = actual_temporary_directory(**kwargs)
        assert not Path(result.name).is_relative_to(pipeline.MOUNT_ROOT)
        scratch_roots.append(Path(result.name))
        return result

    def bootstrap(platform, provider, inputs):
        # Artifact integrity is real. Lock/provenance is covered by Task 5 tests.
        files = policy.validate_provider_inputs(provider, platform.arch, inputs)
        return files, ["nvim"] if fault == "virtual" else ["base"], [], []

    commands = []

    def process(command, **kwargs):
        commands.append(command)
        assert str(pipeline.MOUNT_ROOT) not in " ".join(command)
        assert command[0] in ("pacman", "pacman-key", "lspci")
        if command[0] == "pacman-key":
            if fault == "trust":
                raise ValueError("Invalid bootstrap trust")
            gpg = Path(command[command.index("--gpgdir") + 1])
            (gpg / "pubring.gpg").write_bytes(b"fixture")
            (gpg / "trustdb.gpg").write_bytes(b"fixture")
        if "-Sw" in command and fault in ("signature", "packages", "resolver"):
            raise subprocess.CalledProcessError(1, command, stderr=fault)
        output = "other\n" if fault == "packages" else "base\n"
        if command[0] == "lspci":
            output = (
                "0000:00:01.0 0300: "
                + ("10de:1234" if fault == "gpu" else "1af4:1050")
                + "\n"
            )
        return subprocess.CompletedProcess(command, 0, output)

    monkeypatch.setattr(pipeline, "detect_firmware", lambda: "uefi")
    monkeypatch.setattr(omarchy, "validate_bootstrap_inputs", bootstrap)
    def engine(tx, files, **kwargs):
        assert any("-Sw" in command for command in commands)
        assert not list((tx.dbpath / "local").glob("*/desc"))
        if fault == "engine":
            raise ValueError("Stock engine ABI unavailable")
        commands.append(["stock-engine-checked"])
        return {"PATH": "/selected-engine:/usr/bin:/bin", "LANG": "C.UTF-8"}

    monkeypatch.setattr(omarchy, "prepare_stock_engine", engine, raising=False)
    monkeypatch.setattr(tempfile, "TemporaryDirectory", scratch)
    monkeypatch.setattr(install, "run", process)
    monkeypatch.setattr("arches_installer.core.run.run", process)
    monkeypatch.setattr(subprocess, "run", process)
    monkeypatch.setattr(
        "arches_installer.core.hardware.get_dmi_info",
        lambda: SimpleNamespace(
            sys_vendor="Unknown" if fault == "hardware" else "QEMU"
        ),
    )
    monkeypatch.setattr(
        pipeline, "_inspect_disk", lambda _: {"mountpoints": [], "children": []}
    )
    with patch.object(pipeline, "apply_disk_layout") as disk:
        if fault not in (None, "virtual"):
            with pytest.raises(ValueError):
                pipeline.run_install_pipeline(params, lambda _: None)
            disk.assert_not_called()
        else:
            pipeline.preflight_provider(params)
            assert params.inputs.manifest_sha256 == source.manifest_sha256
            assert any("-Sw" in command for command in commands)
            assert ["stock-engine-checked"] in commands
            if fault == "virtual":
                assert any("-Sw" in command and "nvim" in command for command in commands)
    assert not list(pipeline.MOUNT_ROOT.iterdir())
    assert all(not root.exists() for root in scratch_roots)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "esp-size",
        "parent",
        "encrypted",
        "extra-mount",
        "binding",
        "uuid",
        "readonly",
        "btrfs-raid",
        "hidden-var",
    ],
)
def test_actual_mount_inspection_is_read_only(omarchy_params, monkeypatch, fault):
    import subprocess
    from arches_installer.core import pipeline
    from arches_installer.core.disk import PartitionMap

    root = pipeline.MOUNT_ROOT
    uuid = "12345678-1234-1234-1234-123456789abc"
    disk = {
        "path": "/dev/vda",
        "type": "disk",
        "children": [
            {
                "path": "/dev/vda1",
                "partn": 1,
                "type": "part",
                "parttype": "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
                "size": 2 * 1024**3,
                "fstype": "vfat",
            },
            {
                "path": "/dev/vda2",
                "partn": 2,
                "type": "part",
                "fstype": "btrfs",
                "uuid": uuid,
            },
        ],
    }
    records = [
        {
            "target": str(root / mount.lstrip("/")),
            "source": "/dev/vda2",
            "fstype": "btrfs",
            "uuid": uuid,
            "fsroot": "/" + subvol,
            "options": "rw,noatime",
        }
        for mount, subvol in [
            ("/", "@"),
            ("/home", "@home"),
            ("/var/log", "@log"),
            ("/var/cache/pacman/pkg", "@pkg"),
        ]
    ]
    records.append(
        {
            "target": str(root / "boot"),
            "source": "/dev/vda1",
            "fstype": "vfat",
            "fsroot": "/",
            "options": "rw",
        }
    )
    if fault == "esp-size":
        disk["children"][0]["size"] = 1024**3
    elif fault == "parent":
        disk["children"][0]["path"] = "/dev/vdb1"
    elif fault == "encrypted":
        disk["children"][1]["children"] = [{"type": "crypt"}]
    elif fault == "extra-mount":
        records.append(records[0] | {"target": str(root / "var")})
    elif fault == "binding":
        records[1]["fsroot"] = "/@var"
    elif fault == "uuid":
        records[2]["uuid"] = "other"
    elif fault == "readonly":
        records[0]["options"] = "ro"

    def process(command, **kwargs):
        assert command[0] in ("lsblk", "findmnt", "btrfs")
        if command[0] == "btrfs":
            if "subvolume" in command:
                subvolumes = ["@", "@home", "@log", "@pkg"]
                if fault == "hidden-var":
                    subvolumes.append("@var")
                return subprocess.CompletedProcess(
                    command,
                    0,
                    "\n".join(
                        f"ID {n} gen 1 top level 5 path {name}"
                        for n, name in enumerate(subvolumes, 256)
                    ),
                )
            return subprocess.CompletedProcess(
                command,
                0,
                "Total devices "
                + ("2" if fault == "btrfs-raid" else "1")
                + " FS bytes used 123\n",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {"blockdevices": [disk]}
                if command[0] == "lsblk"
                else {"filesystems": records}
            ),
        )

    monkeypatch.setattr(subprocess, "run", process)
    parts = PartitionMap(esp="/dev/vda1", root="/dev/vda2")
    if fault:
        with pytest.raises(ValueError):
            pipeline.inspect_omarchy_mounts(omarchy_params, parts)
    else:
        assert pipeline.inspect_omarchy_mounts(omarchy_params, parts) == uuid
    assert not list(root.iterdir())


def test_existing_unmounted_identity_rejected_before_wipe(tmp_path, monkeypatch):
    import subprocess
    import tempfile

    assert _real_disk_identity_check is not None
    actual_mkdtemp = tempfile.mkdtemp
    monkeypatch.setattr(
        tempfile, "mkdtemp", lambda **kwargs: actual_mkdtemp(dir=tmp_path)
    )
    commands = []

    def process(command, **kwargs):
        commands.append(command)
        if command[0] == "lsblk":
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "blockdevices": [
                            {
                                "path": "/dev/vda",
                                "fstype": None,
                                "children": [{"path": "/dev/vda2", "fstype": "btrfs"}],
                            }
                        ]
                    }
                ),
            )
        if command[0] == "btrfs":
            return subprocess.CompletedProcess(command, 0, "ID 256 gen 1 top level 5 path <FS_TREE>/@var\n")
        if command[0] == "mount":
            assert "ro,nologreplay,subvolid=5" in command
            state = Path(command[-1]) / "@var/lib/arches/provider.json"
            state.parent.mkdir(parents=True)
            state.write_text("{")
        else:
            assert command[0] == "umount"
            import shutil

            shutil.rmtree(Path(command[-1]) / "@var")
        return subprocess.CompletedProcess(command, 0, "")

    monkeypatch.setattr(subprocess, "run", process)
    with pytest.raises(ValueError, match="identity"):
        _real_disk_identity_check("/dev/vda")
    assert commands[-1][0] == "umount"


def test_omarchy_rejects_environment_only_package_overrides(
    omarchy_params, monkeypatch
):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "arches_installer.core.gpu_stacks.resolve_gpu_stacks",
        lambda _: SimpleNamespace(
            excluded_modules=[], extra_modules=[], extra_packages=["unreviewed-driver"]
        ),
    )
    with pytest.raises(ValueError, match="Omarchy owns"):
        policy.validate_selection(
            omarchy_params.platform,
            omarchy_params.provider,
            omarchy_params.template,
            firmware="uefi",
            filesystem="btrfs",
            install_mode="fresh",
        )


def test_auto_to_progress_preserves_selected_objects(auto_config_file, omarchy_params):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from arches_installer.__main__ import _run_auto
    from arches_installer.tui.progress import InstallProgressScreen
    from arches_installer.core.hardware import DMIInfo, load_machine

    config = replace(
        AutoInstallConfig.from_file(auto_config_file),
        template=omarchy_params.template,
        disk_layout=omarchy_params.disk_layout,
        provider_id="omarchy",
    )
    app = SimpleNamespace(install_success=True, run=lambda: None)
    with (
        patch(
            "arches_installer.__main__._load_platform",
            return_value=omarchy_params.platform,
        ),
        patch.object(AutoInstallConfig, "from_file", return_value=config),
        patch(
            "arches_installer.core.disk.detect_single_disk",
            return_value=SimpleNamespace(path="/dev/vda"),
        ),
        patch("arches_installer.core.hardware.detect_pci_ids", return_value=set()),
        patch(
            "arches_installer.core.hardware.get_dmi_info",
            return_value=DMIInfo(sys_vendor="QEMU", product_name="Standard PC (Q35 + ICH9, 2009)"),
        ),
        patch("arches_installer.core.hardware.discover_quirks", return_value=[]),
        patch(
            "arches_installer.core.hardware.discover_machines",
            return_value=[load_machine(Path(__file__).resolve().parents[3] / "hardware/machines/vm.toml")],
        ),
        patch("arches_installer.tui.app.ArchesApp", return_value=app) as constructor,
    ):
        assert _run_auto(auto_config_file) == 0
    app.platform = omarchy_params.platform
    app.provider = constructor.call_args.kwargs["provider"]
    app.inputs = constructor.call_args.kwargs["inputs"]
    app.raid_config = None
    app.call_from_thread = lambda callback: None
    screen = SimpleNamespace(
        app=app,
        log_msg=MagicMock(),
        _on_install_complete=MagicMock(),
        _on_install_failed_auto=MagicMock(),
    )
    with patch(
        "arches_installer.tui.progress.run_install_pipeline", return_value=object()
    ) as pipeline:
        InstallProgressScreen._run_install(screen)
    params = pipeline.call_args.args[0]
    assert params.provider is app.provider
    assert params.inputs is app.inputs
    assert params.provider.id == "omarchy"
    assert params.hardware.machine is None
    assert not params.hardware.quirks
