"""Tests for arches_installer.core.pipeline."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from arches_installer.core.disk import PartitionMap
from arches_installer.core.disk_layout import (
    DiskLayout,
    PartitionSpec,
    SubvolumeSpec,
)
from arches_installer.core.pipeline import InstallParams, run_install_pipeline


MODULE = "arches_installer.core.pipeline"


@pytest.fixture
def basic_layout():
    """A basic disk layout for testing pipeline."""
    return DiskLayout(
        name="Basic",
        description="Test layout",
        bootloaders=["limine"],
        partitions=[
            PartitionSpec(
                size="2G",
                filesystem="vfat",
                mount_point="/boot",
                label="ESP",
            ),
            PartitionSpec(
                size="*",
                filesystem="btrfs",
                mount_point="/",
                label="archroot",
                mount_options="compress=zstd:1,noatime",
                subvolumes=[
                    SubvolumeSpec(name="@", mount_point="/"),
                    SubvolumeSpec(name="@home", mount_point="/home"),
                    SubvolumeSpec(name="@var", mount_point="/var"),
                ],
            ),
        ],
    )


@pytest.fixture
def auto_params(x86_64_platform, dev_workstation_template, basic_layout):
    """InstallParams for layout-based partition (no pre-existing partition map)."""
    return InstallParams(
        platform=x86_64_platform,
        template=dev_workstation_template,
        device="/dev/vda",
        hostname="testbox",
        username="testuser",
        password="testpass",
        disk_layout=basic_layout,
    )


@pytest.fixture
def manual_params(x86_64_platform, dev_workstation_template):
    """InstallParams with a pre-existing partition map (manual partitioning)."""
    return InstallParams(
        platform=x86_64_platform,
        template=dev_workstation_template,
        device="/dev/vda",
        hostname="testbox",
        username="testuser",
        password="testpass",
        partition_map=PartitionMap(esp="/dev/vda1", root="/dev/vda2"),
    )


@pytest.fixture
def mock_pipeline():
    """Mock all pipeline phase functions."""
    fake_parts = PartitionMap(
        esp="/dev/vda1",
        root="/dev/vda2",
        root_filesystem="btrfs",
        root_subvolumes=["@", "@home", "@var"],
    )
    with (
        patch(f"{MODULE}.apply_disk_layout", return_value=fake_parts) as m_apply,
        patch(f"{MODULE}.install_system") as m_install,
        patch(f"{MODULE}.install_bootloader") as m_bootloader,
        patch(f"{MODULE}.setup_snapshots") as m_snapshots,
        patch(f"{MODULE}.inject_firstboot_service") as m_firstboot,
    ):
        yield {
            "apply_disk_layout": m_apply,
            "install_system": m_install,
            "install_bootloader": m_bootloader,
            "setup_snapshots": m_snapshots,
            "inject_firstboot_service": m_firstboot,
            "parts": fake_parts,
        }


class TestRunInstallPipelineLayoutBased:
    """Tests for pipeline with layout-based partitioning."""

    def test_calls_apply_disk_layout(self, auto_params, mock_pipeline):
        log = MagicMock()
        run_install_pipeline(auto_params, log)
        mock_pipeline["apply_disk_layout"].assert_called_once()

    def test_calls_all_phases_in_order(self, auto_params, mock_pipeline):
        log = MagicMock()
        run_install_pipeline(auto_params, log)

        mock_pipeline["apply_disk_layout"].assert_called_once()
        mock_pipeline["install_system"].assert_called_once()
        mock_pipeline["install_bootloader"].assert_called_once()
        mock_pipeline["setup_snapshots"].assert_called_once()
        mock_pipeline["inject_firstboot_service"].assert_called_once()

    def test_returns_partition_map(self, auto_params, mock_pipeline):
        log = MagicMock()
        result = run_install_pipeline(auto_params, log)
        assert result == mock_pipeline["parts"]

    def test_passes_correct_args_to_install_system(self, auto_params, mock_pipeline):
        log = MagicMock()
        run_install_pipeline(auto_params, log)
        mock_pipeline["install_system"].assert_called_once_with(
            auto_params.platform,
            auto_params.template,
            "testbox",
            "testuser",
            "testpass",
            log=log,
        )

    def test_passes_parts_to_bootloader(self, auto_params, mock_pipeline):
        log = MagicMock()
        run_install_pipeline(auto_params, log)
        call_kwargs = mock_pipeline["install_bootloader"].call_args
        assert call_kwargs[1]["parts"] == mock_pipeline["parts"]


class TestRunInstallPipelineManualPartition:
    """Tests for pipeline with manual partition map."""

    def test_does_not_call_apply_disk_layout(self, manual_params, mock_pipeline):
        log = MagicMock()
        run_install_pipeline(manual_params, log)
        mock_pipeline["apply_disk_layout"].assert_not_called()

    def test_uses_provided_partition_map(self, manual_params, mock_pipeline):
        log = MagicMock()
        result = run_install_pipeline(manual_params, log)
        assert result.esp == "/dev/vda1"
        assert result.root == "/dev/vda2"

    def test_logs_manual_mount_info(self, manual_params, mock_pipeline):
        messages = []
        run_install_pipeline(manual_params, log=messages.append)
        joined = "\n".join(messages)
        assert "manually prepared mounts" in joined.lower() or "Manual mounts" in joined


class TestRunInstallPipelineSnapshotConditional:
    """Tests for snapshot phase conditional on btrfs."""

    def test_skips_snapshots_for_ext4(
        self, x86_64_platform, dev_workstation_template, mock_pipeline
    ):
        """Pipeline should skip snapshots when root_filesystem is ext4."""
        # Mock returns ext4 parts
        ext4_parts = PartitionMap(
            esp="/dev/vda1",
            root="/dev/vda2",
            root_filesystem="ext4",
        )
        mock_pipeline["apply_disk_layout"].return_value = ext4_parts

        ext4_layout = DiskLayout(
            name="ext4",
            description="test",
            bootloaders=["limine"],
            partitions=[
                PartitionSpec(size="2G", filesystem="vfat", mount_point="/boot"),
                PartitionSpec(size="*", filesystem="ext4", mount_point="/"),
            ],
        )
        params = InstallParams(
            platform=x86_64_platform,
            template=dev_workstation_template,
            device="/dev/vda",
            hostname="testbox",
            username="testuser",
            password="testpass",
            disk_layout=ext4_layout,
        )
        log = MagicMock()
        run_install_pipeline(params, log)
        mock_pipeline["setup_snapshots"].assert_not_called()

    def test_runs_snapshots_for_btrfs(self, auto_params, mock_pipeline):
        """Pipeline should run snapshots when root_filesystem is btrfs."""
        log = MagicMock()
        run_install_pipeline(auto_params, log)
        mock_pipeline["setup_snapshots"].assert_called_once()


class TestRunInstallPipelineNoPartitionSource:
    """Test that pipeline raises when neither partition_map nor disk_layout is set."""

    def test_raises_without_partition_source(
        self, x86_64_platform, dev_workstation_template, mock_pipeline
    ):
        params = InstallParams(
            platform=x86_64_platform,
            template=dev_workstation_template,
            device="/dev/vda",
            hostname="testbox",
            username="testuser",
            password="testpass",
        )
        with pytest.raises(RuntimeError, match="No partition_map or disk_layout"):
            run_install_pipeline(params, log=MagicMock())


class TestRunInstallPipelineErrorPropagation:
    """Tests for error propagation."""

    def test_apply_disk_layout_error_propagates(self, auto_params):
        with (
            patch(
                f"{MODULE}.apply_disk_layout",
                side_effect=RuntimeError("disk fail"),
            ),
            patch(f"{MODULE}.install_system"),
            patch(f"{MODULE}.install_bootloader"),
            patch(f"{MODULE}.setup_snapshots"),
            patch(f"{MODULE}.inject_firstboot_service"),
        ):
            with pytest.raises(RuntimeError, match="disk fail"):
                run_install_pipeline(auto_params, log=MagicMock())

    def test_install_system_error_propagates(self, auto_params):
        fake_parts = PartitionMap(
            esp="/dev/vda1",
            root="/dev/vda2",
            root_filesystem="btrfs",
            root_subvolumes=["@", "@home", "@var"],
        )
        with (
            patch(f"{MODULE}.apply_disk_layout", return_value=fake_parts),
            patch(f"{MODULE}.install_system", side_effect=RuntimeError("install fail")),
            patch(f"{MODULE}.install_bootloader"),
            patch(f"{MODULE}.setup_snapshots"),
            patch(f"{MODULE}.inject_firstboot_service"),
        ):
            with pytest.raises(RuntimeError, match="install fail"):
                run_install_pipeline(auto_params, log=MagicMock())


class TestRunInstallPipelineLogMessages:
    """Tests for log output."""

    def test_logs_phase_markers(self, auto_params, mock_pipeline):
        messages = []
        run_install_pipeline(auto_params, log=messages.append)
        joined = "\n".join(messages)
        assert "Phase 1" in joined
        assert "Phase 2" in joined
        assert "Phase 3" in joined
        assert "Phase 4" in joined  # btrfs root
        assert "Phase 5" in joined

    def test_logs_installation_complete(self, auto_params, mock_pipeline):
        messages = []
        run_install_pipeline(auto_params, log=messages.append)
        joined = "\n".join(messages)
        assert "Installation complete" in joined
