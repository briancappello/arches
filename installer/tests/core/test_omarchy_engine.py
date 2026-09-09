"""Stock bootstrap tools: authenticated bytes, scoped loader, no host packages."""

import hashlib
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from arches_installer.core import omarchy


@pytest.mark.parametrize("fault", [None, "digest", "signature", "untrusted", "abi"])
def test_stock_engine_authentication_and_path(tmp_path, monkeypatch, fault):
    archive = "pacman-7.1.0.r9.g54d9411-2-x86_64.pkg.tar.zst"
    cache = tmp_path / "cache"
    cache.mkdir()
    files = {}
    for name in (archive, archive + ".sig"):
        (cache / name).write_bytes(b"signed fixture")
        files["packages/" + name] = hashlib.sha256(b"signed fixture").hexdigest()
    monkeypatch.setattr(
        omarchy,
        "load_lock",
        lambda: {"packages": {archive: files["packages/" + archive]}},
    )
    work = tmp_path / "private tools with spaces"
    work.mkdir(mode=0o700)
    tx = SimpleNamespace(cache=cache, work=work, gpgdir=tmp_path / "trusted")
    if fault == "digest":
        (cache / archive).write_bytes(b"tampered")
    events = []
    real_run = subprocess.run

    def command(argv, **kwargs):
        events.append(argv[0])
        if argv[0] == "gpg":
            assert "--no-auto-key-retrieve" in argv
            assert argv[argv.index("--homedir") + 1] == str(tx.gpgdir)
            if fault == "signature":
                raise subprocess.CalledProcessError(1, argv)
            return subprocess.CompletedProcess(
                argv,
                0,
                "[GNUPG:] VALIDSIG "
                + "A" * 40
                + " rest\n"
                + ("" if fault == "untrusted" else "[GNUPG:] TRUST_FULLY 0 pgp\n"),
            )
        if argv[0] == "bsdtar":
            assert events == ["gpg", "bsdtar"]
            destination = Path(argv[argv.index("-C") + 1])
            members = argv[argv.index("-C") + 2 :]
            assert set(members) == {
                "usr/bin/pacman",
                "usr/bin/pacman-conf",
                "usr/lib/libalpm.so.16",
                "usr/lib/libalpm.so.16.0.1",
            }
            for member in members:
                target = destination / member
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    '#!/bin/sh\nprintf "%s|%s\\n" "$0" "$LD_LIBRARY_PATH"\n'
                )
                target.chmod(0o700)
            return subprocess.CompletedProcess(argv, 0, "")
        # The only executed tools are our shell fixtures, never native packages.
        assert argv[0] in ("pacman", "pacman-conf") and argv[1:] == ["--version"]
        assert "LD_LIBRARY_PATH" not in kwargs["env"]
        if fault == "abi":
            raise subprocess.CalledProcessError(127, argv)
        path = kwargs["env"]["PATH"].split(":")[0]
        assert Path(path) == work / "stock-engine/bin"
        assert (Path(path) / argv[0]).is_file()
        return real_run(
            argv,
            env=kwargs["env"] | {"PATH": path},
            check=True,
            text=True,
            capture_output=True,
        )

    with (
        patch.dict(
            os.environ,
            {"PATH": str(tmp_path / "deny"), "LD_LIBRARY_PATH": "/untrusted"},
            clear=True,
        ),
        patch.object(omarchy, "run", side_effect=command),
    ):
        if fault:
            with pytest.raises((ValueError, subprocess.CalledProcessError)):
                omarchy.prepare_stock_engine(tx, files)
            assert "pacman-conf" not in events
            if fault in ("digest", "signature", "untrusted"):
                assert "bsdtar" not in events
        else:
            env = omarchy.prepare_stock_engine(tx, files)
            assert os.environ["PATH"] == str(tmp_path / "deny")
            # Exercise PATH dispatch as pacstrap does, not an absolute argv substitute.
            result = real_run(
                ["/bin/sh", "-c", "pacman --version && pacman-conf --version"],
                env=env | {"PATH": env["PATH"].split(":")[0]},
                check=True,
                text=True,
                capture_output=True,
            )
            tools = work / "stock-engine"
            assert result.stdout.splitlines() == [
                f"{tools}/usr/bin/{name}|{tools}/usr/lib"
                for name in ("pacman", "pacman-conf")
            ]
