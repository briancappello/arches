"""Task 6 review regressions with deny-by-default process boundaries."""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from arches_installer.core import pipeline
from .test_provider_entrypoints import isolated as isolated
from .test_provider_entrypoints import omarchy_params as omarchy_params

_inspect_identity = pipeline.check_existing_disk_identity


@pytest.fixture
def identity_disk(tmp_path, monkeypatch):
    original_mkdtemp = tempfile.mkdtemp
    monkeypatch.setattr(
        tempfile, "mkdtemp", lambda **kwargs: original_mkdtemp(dir=tmp_path)
    )
    state = {
        "fs": "btrfs",
        "subvolume": "systems/production root",
        "identity": "var/lib/arches/provider.json",
        "failure": None,
    }
    commands = []

    def process(command, **kwargs):
        commands.append(command)
        assert command[0] in ("lsblk", "mount", "btrfs", "umount")
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
                                "children": [
                                    {"path": "/dev/vda2", "fstype": state["fs"]}
                                ],
                            }
                        ]
                    }
                ),
            )
        if command[0] == "mount":
            assert command[command.index("-o") + 1] in (
                "ro,nologreplay,subvolid=5",
                "ro,noload",
                "ro,norecovery",
                "ro",
            )
            if state["failure"] == "mount":
                raise subprocess.CalledProcessError(1, command)
            root = Path(command[-1])
            if (
                state["identity"] is None
                and state["subvolume"] == "systems/production root"
            ):
                (root / state["subvolume"]).mkdir(parents=True)
            # Deliberately do not materialize unsafe names supplied by bad enumeration.
            if state["identity"] is not None:
                if state["fs"] == "btrfs":
                    root /= state["subvolume"]
                record = root / state["identity"]
                record.parent.mkdir(parents=True, exist_ok=True)
                record.write_text("{")
            if state["failure"] == "symlink":
                (root / "escape").symlink_to(tmp_path)
        if command[0] == "btrfs":
            assert command[1:4] == ["subvolume", "list", "-a"]
            if state["failure"] == "enumeration":
                raise subprocess.CalledProcessError(1, command)
            output = (
                "ID 256 gen 1 top level 5 path <FS_TREE>/" + state["subvolume"] + "\n"
            )
            if state["failure"] == "malformed":
                output = "unexpected output\n"
            return subprocess.CompletedProcess(command, 0, output)
        if command[0] == "umount":
            if state["failure"] == "unmount":
                raise subprocess.CalledProcessError(1, command)
            for child in Path(command[-1]).iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        return subprocess.CompletedProcess(command, 0, "")

    monkeypatch.setattr(subprocess, "run", process)
    monkeypatch.setattr(pipeline, "check_existing_disk_identity", _inspect_identity)
    return state, commands


@pytest.mark.parametrize(
    "filesystem,subvolume,relative",
    [
        ("btrfs", "systems/production root", "var/lib/arches/provider.json"),
        ("btrfs", "data/var", "lib/arches/provider.json"),
        ("ext4", "", "lib/arches/provider.json"),
        ("ext4", "", "arches/provider.json"),
        ("xfs", "", "provider.json"),
        ("vfat", "", "lib/arches/provider.json"),
    ],
)
def test_identity_locations_prevent_disk_mutation(
    identity_disk,
    filesystem,
    subvolume,
    relative,
    x86_64_platform,
    dev_workstation_template,
    basic_disk_layout,
):
    state, commands = identity_disk
    state.update(fs=filesystem, subvolume=subvolume, identity=relative)
    params = pipeline.InstallParams(
        x86_64_platform,
        dev_workstation_template,
        "/dev/vda",
        "test",
        "tester",
        "fixture-only",
        disk_layout=basic_disk_layout,
    )
    with patch.object(
        pipeline,
        "apply_disk_layout",
        side_effect=AssertionError("Disk mutation reached"),
    ) as disk:
        with pytest.raises(ValueError, match="identity"):
            pipeline.run_install_pipeline(params, lambda _: None)
        disk.assert_not_called()
    assert commands[-1][0] == "umount"


@pytest.mark.parametrize(
    "failure,subvolume",
    [
        ("enumeration", "root"),
        ("malformed", "root"),
        (None, "../escape"),
        (None, "/absolute"),
        ("symlink", "escape/root"),
        ("mount", "root"),
        ("unmount", "root"),
    ],
)
def test_identity_inspection_errors_fail_closed(identity_disk, failure, subvolume):
    state, commands = identity_disk
    state.update(identity=None, failure=failure, subvolume=subvolume)
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        _inspect_identity("/dev/vda")
    if failure != "mount":
        assert commands[-1][0] == "umount"


@pytest.mark.parametrize("filesystem", ["crypto_LUKS", "unknown", None])
def test_unsupported_identity_filesystem_fails_closed(identity_disk, filesystem):
    state, commands = identity_disk
    state.update(fs=filesystem, identity=None)
    with pytest.raises(ValueError, match="inspect"):
        _inspect_identity("/dev/vda")
    assert [c[0] for c in commands] == ["lsblk"]


def test_inspectable_identity_free_filesystem(identity_disk):
    state, commands = identity_disk
    state.update(identity=None)
    _inspect_identity("/dev/vda")
    assert [c[0] for c in commands] == ["lsblk", "mount", "btrfs", "umount"]


@pytest.mark.parametrize(
    "failure", ["create-config", "limine-snapper-sync.service", "grub-btrfsd.service"]
)
def test_snapshot_failure_marks_pipeline_failed(
    x86_64_platform, dev_workstation_template, basic_disk_layout, monkeypatch, failure
):
    from arches_installer.core import snapper, provider
    from arches_installer.core.disk import PartitionMap

    params = pipeline.InstallParams(
        x86_64_platform,
        dev_workstation_template,
        "/dev/vda",
        "test",
        "tester",
        "fixture-only",
        disk_layout=basic_disk_layout,
    )
    if failure == "grub-btrfsd.service":
        params.platform.bootloader.type = "grub"
    monkeypatch.setattr(snapper, "MOUNT_ROOT", pipeline.MOUNT_ROOT)

    def chroot(command, **kwargs):
        if failure in command:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, "")

    messages = []
    with (
        patch.object(snapper, "chroot_run", side_effect=chroot),
        patch.object(
            pipeline,
            "apply_disk_layout",
            return_value=PartitionMap(
                "/dev/vda1", "/dev/vda2", root_filesystem="btrfs"
            ),
        ),
        patch.object(pipeline, "install_system"),
        patch.object(pipeline, "install_bootloader"),
        patch.object(pipeline, "inject_firstboot_service") as firstboot,
        patch.object(pipeline, "_persist_install_log"),
    ):
        with pytest.raises(subprocess.CalledProcessError):
            pipeline.run_install_pipeline(params, messages.append)
        firstboot.assert_not_called()
    assert provider.read_provider_identity(pipeline.MOUNT_ROOT)["status"] == "failed"
    assert not any("Installation complete" in line for line in messages)


@pytest.mark.parametrize(
    "existing", ["valid", "missing", "wrong-root", "wrong-fstype", "not-subvolume"]
)
def test_existing_snapper_requires_actual_postcondition(
    tmp_path, monkeypatch, x86_64_platform, existing
):
    from arches_installer.core import snapper
    from arches_installer.core.disk import PartitionMap

    monkeypatch.setattr(snapper, "MOUNT_ROOT", tmp_path)
    config = tmp_path / "etc/snapper/configs/root"
    config.parent.mkdir(parents=True)
    content = 'SUBVOLUME="/"\nFSTYPE="btrfs"\n'
    if existing == "wrong-root":
        content = content.replace('"/"', '"/home"')
    elif existing == "wrong-fstype":
        content = content.replace('"btrfs"', '"ext4"')
    if existing != "missing":
        config.write_text(content)

    def chroot(command, **kwargs):
        if "create-config" in command or existing == "not-subvolume":
            raise subprocess.CalledProcessError(1, command)
        assert command == ["btrfs", "subvolume", "show", "/.snapshots"]
        return subprocess.CompletedProcess(command, 0, "snapshot subvolume")

    with patch.object(snapper, "chroot_run", side_effect=chroot) as command:
        if existing == "valid":
            snapper.configure_snapper(
                x86_64_platform,
                parts=PartitionMap("/dev/vda1", "/dev/vda2", root_filesystem="btrfs"),
            )
            assert command.call_count == 2
        else:
            with pytest.raises(subprocess.CalledProcessError):
                snapper.configure_snapper(x86_64_platform)
            assert (
                config.read_text() == content
                if config.exists()
                else existing == "missing"
            )


@pytest.mark.parametrize("filesystem", ["btrfs", "ext4"])
@pytest.mark.parametrize("boot", ["", "/boot", "/boot/efi"])
def test_detected_mounts_carry_actual_root_filesystem(
    tmp_path, monkeypatch, filesystem, boot
):
    import io
    from arches_installer.core import disk

    monkeypatch.setattr(disk, "MOUNT_ROOT", tmp_path)
    mounts = f"/dev/vda2 {tmp_path} {filesystem} rw 0 0\n"
    if boot:
        mounts += f"/dev/vda1 {tmp_path}{boot} vfat rw 0 0\n"
    with patch("builtins.open", return_value=io.StringIO(mounts)):
        parts = disk.detect_mounts()
    assert parts.root_filesystem == filesystem
    assert parts.root == "/dev/vda2"


def test_manual_omarchy_detection_passes_filesystem_gate(omarchy_params, monkeypatch):
    import io
    from arches_installer.core import disk

    monkeypatch.setattr(disk, "MOUNT_ROOT", pipeline.MOUNT_ROOT)
    mounts = f"/dev/vda2 {pipeline.MOUNT_ROOT} btrfs rw,subvol=/@ 0 0\n/dev/vda1 {pipeline.MOUNT_ROOT}/boot vfat rw 0 0\n"
    with patch("builtins.open", return_value=io.StringIO(mounts)):
        omarchy_params.partition_map = disk.detect_mounts()
    omarchy_params.disk_layout = None
    monkeypatch.setattr(pipeline, "detect_firmware", lambda: "uefi")
    # This media fixture deliberately lacks authenticated inputs. Detection must
    # pass the filesystem gate and reach that independent authentication gate.
    with pytest.raises(ValueError, match="authenticated staged inputs"):
        pipeline.preflight_provider(omarchy_params)


@pytest.mark.parametrize(
    "failure", [None, "preflight", "finalize", "metadata", "cancel"]
)
def test_tui_exit_reflects_install_result(
    x86_64_platform, dev_workstation_template, basic_disk_layout, failure
):
    from contextlib import ExitStack
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from arches_installer.__main__ import _run_tui
    from arches_installer.tui.progress import InstallProgressScreen
    from arches_installer.core.disk import PartitionMap
    from arches_installer.core.provider import read_provider_identity

    app = SimpleNamespace(
        platform=x86_64_platform,
        selected_template=dev_workstation_template,
        selected_device="/dev/vda",
        selected_layout=basic_disk_layout,
        partition_mode="auto",
        hostname="test",
        username="tester",
        password="fixture-only",
        raid_config=None,
        hardware_config=None,
        ansible_vars=None,
        provider=None,
        inputs=None,
        install_started=False,
        install_success=False,
        auto_install=False,
        call_from_thread=lambda callback: None,
    )
    screen = SimpleNamespace(
        app=app,
        log_msg=MagicMock(),
        _on_install_complete=MagicMock(),
        _enable_reboot=MagicMock(),
    )
    app.run = lambda: (
        InstallProgressScreen._run_install(screen) if failure != "cancel" else None
    )
    real_write = pipeline.write_provider_identity

    def write(root, identity, status):
        if failure == "metadata" and status == "complete":
            raise OSError("identity publication failed")
        return real_write(root, identity, status)

    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "arches_installer.__main__._load_platform", return_value=x86_64_platform
            )
        )
        stack.enter_context(
            patch("arches_installer.tui.app.ArchesApp", return_value=app)
        )
        stack.enter_context(
            patch.object(
                pipeline,
                "apply_disk_layout",
                return_value=PartitionMap(
                    "/dev/vda1", "/dev/vda2", root_filesystem="btrfs"
                ),
            )
        )
        for name in (
            "install_system",
            "install_bootloader",
            "setup_snapshots",
            "inject_firstboot_service",
            "_persist_install_log",
        ):
            stack.enter_context(patch.object(pipeline, name))
        if failure == "preflight":
            stack.enter_context(
                patch.object(
                    pipeline,
                    "preflight_provider",
                    side_effect=ValueError("preflight failed"),
                )
            )
        if failure == "finalize":
            stack.enter_context(
                patch.object(
                    pipeline,
                    "_finish_install",
                    side_effect=RuntimeError("finalization failed"),
                )
            )
        stack.enter_context(
            patch.object(pipeline, "write_provider_identity", side_effect=write)
        )
        result = _run_tui()
    assert result == (1 if failure in ("preflight", "finalize", "metadata") else 0)
    assert app.install_started is (failure != "cancel")
    assert app.install_success is (failure is None)
    if failure in ("finalize", "metadata"):
        assert read_provider_identity(pipeline.MOUNT_ROOT)["status"] == "failed"
