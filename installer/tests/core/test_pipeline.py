"""Tests for arches_installer.core.pipeline."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from arches_installer.core.disk import PartitionMap
from arches_installer.core.pipeline import InstallParams, run_install_pipeline


MODULE = "arches_installer.core.pipeline"


@pytest.fixture
def auto_params(x86_64_platform, dev_workstation_template):
    """InstallParams for auto-partition (no pre-existing partition map)."""
    return InstallParams(
        platform=x86_64_platform,
        template=dev_workstation_template,
        device="/dev/vda",
        hostname="testbox",
        username="testuser",
        password="testpass",
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
    fake_parts = PartitionMap(esp="/dev/vda1", root="/dev/vda2")
    with (
        patch(f"{MODULE}.prepare_disk", return_value=fake_parts) as m_prepare,
        patch(f"{MODULE}.install_system") as m_install,
        patch(f"{MODULE}.install_bootloader") as m_bootloader,
        patch(f"{MODULE}.setup_snapshots") as m_snapshots,
        patch(f"{MODULE}.inject_firstboot_service") as m_firstboot,
    ):
        yield {
            "prepare_disk": m_prepare,
            "install_system": m_install,
            "install_bootloader": m_bootloader,
            "setup_snapshots": m_snapshots,
            "inject_firstboot_service": m_firstboot,
            "parts": fake_parts,
        }


class TestRunInstallPipelineAutoPartition:
    """Tests for pipeline with auto-partition (no pre-existing partition map)."""

    def test_calls_prepare_disk(self, auto_params, mock_pipeline):
        log = MagicMock()
        run_install_pipeline(auto_params, log)
        mock_pipeline["prepare_disk"].assert_called_once_with(
            "/dev/vda", auto_params.platform
        )

    def test_calls_all_phases_in_order(self, auto_params, mock_pipeline):
        log = MagicMock()
        run_install_pipeline(auto_params, log)

        mock_pipeline["prepare_disk"].assert_called_once()
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

    def test_passes_correct_args_to_bootloader(self, auto_params, mock_pipeline):
        log = MagicMock()
        run_install_pipeline(auto_params, log)
        mock_pipeline["install_bootloader"].assert_called_once_with(
            auto_params.platform,
            "/dev/vda",
            "/dev/vda1",
            "/dev/vda2",
            log=log,
        )


class TestRunInstallPipelineManualPartition:
    """Tests for pipeline with manual partition map."""

    def test_does_not_call_prepare_disk(self, manual_params, mock_pipeline):
        log = MagicMock()
        run_install_pipeline(manual_params, log)
        mock_pipeline["prepare_disk"].assert_not_called()

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

    def test_skips_snapshots_for_ext4(self, dev_workstation_template, mock_pipeline):
        """Pipeline should skip snapshots when filesystem is ext4."""
        from arches_installer.core.platform import (
            BootloaderPlatformConfig,
            DiskLayoutConfig,
            HardwareDetectionConfig,
            KernelConfig,
            KernelVariant,
            PlatformConfig,
        )

        ext4_platform = PlatformConfig(
            name="test-ext4",
            description="Test ext4 platform",
            arch="x86_64",
            kernel=KernelConfig(
                variants=[KernelVariant(package="linux", headers="linux-headers")]
            ),
            bootloader=BootloaderPlatformConfig(type="limine"),
            disk_layout=DiskLayoutConfig(filesystem="ext4"),
            hardware_detection=HardwareDetectionConfig(enabled=False),
            base_packages=["base"],
        )
        params = InstallParams(
            platform=ext4_platform,
            template=dev_workstation_template,
            device="/dev/vda",
            hostname="testbox",
            username="testuser",
            password="testpass",
        )
        log = MagicMock()
        run_install_pipeline(params, log)
        mock_pipeline["setup_snapshots"].assert_not_called()

    def test_runs_snapshots_for_btrfs(self, auto_params, mock_pipeline):
        """Pipeline should run snapshots when filesystem is btrfs."""
        log = MagicMock()
        run_install_pipeline(auto_params, log)
        mock_pipeline["setup_snapshots"].assert_called_once()


class TestRunInstallPipelineErrorPropagation:
    """Tests for error propagation."""

    def test_prepare_disk_error_propagates(self, auto_params):
        with (
            patch(f"{MODULE}.prepare_disk", side_effect=RuntimeError("disk fail")),
            patch(f"{MODULE}.install_system"),
            patch(f"{MODULE}.install_bootloader"),
            patch(f"{MODULE}.setup_snapshots"),
            patch(f"{MODULE}.inject_firstboot_service"),
        ):
            with pytest.raises(RuntimeError, match="disk fail"):
                run_install_pipeline(auto_params, log=MagicMock())

    def test_install_system_error_propagates(self, auto_params):
        with (
            patch(
                f"{MODULE}.prepare_disk",
                return_value=PartitionMap(esp="/dev/vda1", root="/dev/vda2"),
            ),
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
        assert "Phase 4" in joined  # btrfs platform
        assert "Phase 5" in joined

    def test_logs_installation_complete(self, auto_params, mock_pipeline):
        messages = []
        run_install_pipeline(auto_params, log=messages.append)
        joined = "\n".join(messages)
        assert "Installation complete" in joined
