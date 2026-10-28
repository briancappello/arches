"""The QEMU p1 harness (scripts/qemu-p1/) stays runnable."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parents[3] / "scripts" / "qemu-p1"


@pytest.mark.parametrize("script", sorted(p.name for p in _DIR.glob("*.sh")))
def test_shell_scripts_parse(script: str) -> None:
    assert subprocess.run(["bash", "-n", str(_DIR / script)]).returncode == 0


def test_vm_state_is_not_on_tmp() -> None:
    # tmpfs /tmp filled up under the qcow2 disks (write faults in the guest).
    text = (_DIR / "vm.sh").read_text()
    assert 'DIR="${P1VM_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/arches-p1vm}"' in text


def test_checksums_name_disks_by_serial() -> None:
    text = (_DIR / "checksums.sh").read_text()
    assert "SERIAL" in text and '"raw $serial-p$n' in text


def test_old_system_fixture_refuses_real_hardware() -> None:
    text = (_DIR / "make-old-system.sh").read_text()
    assert "systemd-detect-virt --vm" in text and "sys_vendor" in text


def test_qmp_typing() -> None:
    spec = importlib.util.spec_from_file_location("qmp", _DIR / "qmp.py")
    qmp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(qmp)
    assert qmp.keys_for("a") == ["a"]
    assert qmp.keys_for("A") == ["shift", "a"]
    assert qmp.keys_for("7") == ["7"]
    assert qmp.keys_for("-") == ["minus"]
    assert qmp.keys_for(" ") == ["spc"]
