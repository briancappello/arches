"""Tests for arches_installer.core.install — core install pipeline."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

from arches_installer.core.platform import PlatformConfig
from arches_installer.core.template import InstallTemplate

# ---------------------------------------------------------------------------
# Module under test — imported with absolute paths mocked where needed
# ---------------------------------------------------------------------------
MODULE = "arches_installer.core.install"


@pytest.fixture
def selected_inputs(tmp_path, x86_64_platform, request):
    import hashlib
    import json
    from types import SimpleNamespace
    from arches_installer.core.provider import resolve_provider

    provider_id = getattr(request, "param", "omarchy")
    provider = resolve_provider(x86_64_platform, provider_id)
    root = tmp_path / "selected"
    root.mkdir()
    files = {}
    for name in (
        ("core", "extra", "multilib", "omarchy")
        if provider_id == "omarchy"
        else ("core", "extra", "multilib")
    ):
        files[f"sync/{name}.db"] = f"{provider_id}-{name}-version-2".encode()
    for name in (
        ("archlinux", "omarchy") if provider_id == "omarchy" else ("archlinux",)
    ):
        for suffix in (".gpg", "-trusted", "-revoked"):
            files[f"keyrings/{name}{suffix}"] = b"selected-public-material"
    files["packages/base-2-1-x86_64.pkg.tar.zst"] = b"signed-package"
    if provider_id == "arch":
        files["mirrorlist"] = (
            b"Server = https://geo.mirror.pkgbuild.com/$repo/os/$arch\n"
        )
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = {
        "provider": provider_id,
        "channel": "stable",
        "architecture": "x86_64",
        "config_sha256": hashlib.sha256(provider.pacman_conf.encode()).hexdigest(),
        "files": {
            name: hashlib.sha256(data).hexdigest() for name, data in files.items()
        },
    }
    inputs = SimpleNamespace(root=root, manifest_sha256="")

    def seal():
        data = json.dumps(manifest, sort_keys=True).encode()
        (root / "manifest.json").write_bytes(data)
        inputs.manifest_sha256 = hashlib.sha256(data).hexdigest()

    seal()
    return provider, inputs, manifest, seal


class TestProviderTransactions:
    @pytest.mark.parametrize("chroot", [False, True])
    def test_native_hook_fixture_rejects_unstubbed_absolute_commands(
        self, tmp_path, chroot
    ):
        argv = [
            "/bin/bash",
            str(Path(__file__).with_name("arch_chroot_api_fixture.sh")),
        ]
        if chroot:
            argv += ["arch-chroot", str(tmp_path)]
        # Deliberately harmless even before the fixture guard exists.
        result = subprocess.run(
            argv + ["/bin/true"],
            capture_output=True,
            text=True,
            env={
                "PATH": str(tmp_path / "no-executables"),
                "HOME": str(tmp_path),
                "TEST_ROOT": str(tmp_path),
                "LANG": "C.UTF-8",
            },
            timeout=10,
            start_new_session=True,
        )
        assert result.returncode != 0
        assert "Unstubbed" in result.stderr

    @pytest.mark.parametrize("relative", ["var/log", "var/log/pacman.log"])
    def test_log_symlink_rejected_before_any_target_mutation(
        self, tmp_path, mnt, x86_64_platform, selected_inputs, relative
    ):
        import arches_installer.core.install as mod

        provider, inputs, _, _ = selected_inputs
        outside = tmp_path / "outside-log"
        outside.mkdir()
        sentinel = outside / "pacman.log"
        sentinel.write_bytes(b"unchanged-host-log")
        link = mnt / relative
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside if relative == "var/log" else sentinel)
        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}.run") as command,
            patch(
                f"{MODULE}.tempfile.TemporaryDirectory",
                side_effect=AssertionError("temporary target mutation"),
            ) as temporary,
            patch.object(
                Path, "mkdir", side_effect=AssertionError("target mkdir")
            ) as mkdir,
        ):
            with pytest.raises(ValueError, match="Symlink in target package path"):
                with mod.package_transaction(
                    x86_64_platform, provider=provider, inputs=inputs
                ):
                    pytest.fail("accepted target log escape")
        command.assert_not_called()
        temporary.assert_not_called()
        mkdir.assert_not_called()
        assert sentinel.read_bytes() == b"unchanged-host-log"
        assert not (mnt / "var/cache").exists()

    def test_override_rejects_unmounted_selected_cache(self, transaction):
        import arches_installer.core.install as mod

        template = InstallTemplate.from_dict({})
        template.install.override = ["selected-override"]
        mod._make_pacman_conf_with_cache(transaction)
        with patch(f"{MODULE}.run") as command:
            with pytest.raises(ValueError, match="cache.*mounted"):
                mod.install_override_packages(template, transaction=transaction)
        command.assert_not_called()

    @pytest.mark.parametrize("provider_id", ["cachyos", "arch"])
    @pytest.mark.parametrize("hook_fails", [False, True])
    def test_override_hook_runs_with_arch_chroot_api_mounts(
        self, transaction, x86_64_platform, provider_id, hook_fails
    ):
        import arches_installer.core.install as mod
        from arches_installer.core.provider import resolve_provider

        transaction.provider = resolve_provider(x86_64_platform, provider_id)
        transaction.final_conf = transaction.provider.pacman_conf
        transaction.legacy = provider_id == "cachyos"
        transaction.mounted = (
            True  # install_pacman_conf already mounted the selected cache.
        )
        mod._make_pacman_conf_with_cache(transaction)
        original_config = transaction.config.read_bytes()
        snapshot = (transaction.work / "mirror/core/core.db").read_bytes()
        template = InstallTemplate.from_dict({})
        template.install.override = ["selected-override"]
        results = []

        def command(argv, **kwargs):
            # Run captured upstream API setup with mocked syscalls, not a mock
            # asserting only the arch-chroot argv. The simulated hook needs all APIs.
            result = subprocess.run(
                ["/bin/bash", str(Path(__file__).with_name("arch_chroot_api_fixture.sh"))]
                + argv,
                capture_output=True,
                text=True,
                env={
                    "PATH": str(transaction.work / "no-executables"),
                    "HOME": str(transaction.work),
                    "LANG": "C.UTF-8",
                    "TEST_ROOT": str(transaction.root),
                    "HOOK_EXIT": "47" if hook_fails else "0",
                },
                timeout=10,
                start_new_session=True,
            )
            results.append(result)
            if result.returncode:
                result.check_returncode()
            return result

        with patch(f"{MODULE}.run", side_effect=command) as run_command:
            if hook_fails:
                with pytest.raises(subprocess.CalledProcessError) as error:
                    mod.install_override_packages(template, transaction=transaction)
                assert error.value.returncode == 47
            else:
                mod.install_override_packages(template, transaction=transaction)
        output = results[0].stdout
        assert "HOOK_API_OK" in output
        for path, source in (
            ("proc", "proc"),
            ("sys", "sys"),
            ("dev", "udev"),
            ("run", "/run"),
        ):
            assert f"MOUNT {source} {transaction.root}/{path}" in output
            assert f"UMOUNT {transaction.root}/{path}" in output
        argv = run_command.call_args.args[0]
        assert argv[:3] == ["arch-chroot", str(transaction.root), "pacman"]
        assert "--root" not in argv and "--ask=68" in argv
        assert argv[argv.index("--dbpath") + 1] == "/var/lib/pacman"
        assert argv[argv.index("--gpgdir") + 1] == "/etc/pacman.d/gnupg"
        conf = transaction.root / argv[argv.index("--config") + 1].lstrip("/")
        text = conf.read_text()
        assert str(transaction.root) not in text
        assert str(transaction.cache) not in text
        assert "CacheDir = /var/cache/pacman/pkg/" in text
        assert "CacheDir = /mnt/arches-pkg-cache/" in text
        assert "SigLevel" in text and "Required DatabaseOptional" in text
        assert (
            f"Server = file:///{transaction.work.relative_to(transaction.root)}/mirror/core"
            in text
        )
        assert transaction.config.read_bytes() == original_config
        assert (transaction.work / "mirror/core/core.db").read_bytes() == snapshot

    @pytest.mark.parametrize("sync_fails", [False, True])
    def test_online_legacy_syncs_before_real_availability_query(
        self, transaction, x86_64_platform, sync_fails
    ):
        import arches_installer.core.install as mod

        transaction.sync_source = transaction.work / "absent-snapshot"
        transaction.cache = transaction.work / "absent-cache"
        from arches_installer.core.provider import resolve_provider

        transaction.final_conf = resolve_provider(x86_64_platform).pacman_conf
        mod._make_pacman_conf_with_cache(transaction)
        mod._preseed_pacman_databases(
            source=transaction.sync_source, target=transaction.dbpath / "sync"
        )
        template = InstallTemplate.from_dict({})
        template.install.pacstrap = ["unbuilt-local-package"]
        packages = transaction.provider.base_packages + [
            p
            for v in transaction.provider.kernel.variants
            for p in (v.package, v.headers)
        ]
        calls = []

        def query(argv, **kwargs):
            calls.append(argv)
            assert argv[argv.index("--dbpath") + 1] == str(transaction.dbpath)
            assert argv[argv.index("--gpgdir") + 1] == str(transaction.gpgdir)
            if not (transaction.dbpath / "sync/core.db").exists():
                raise subprocess.CalledProcessError(1, argv)
            return subprocess.CompletedProcess(argv, 0, stdout="\n".join(packages))

        def command(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "pacman":
                assert argv == ["pacman"] + transaction.pacman_args + [
                    "-Sy",
                    "--noconfirm",
                    "--ask=64",
                ]
                # A nonexistent unbuilt file repo must not make the upstream sync fail.
                assert "[arches-local]" not in transaction.config.read_text()
                assert "[cachyos]" in transaction.config.read_text()
                if sync_fails:
                    raise subprocess.CalledProcessError(1, argv)
                sync = transaction.dbpath / "sync"
                sync.mkdir(parents=True, exist_ok=True)
                (sync / "core.db").write_bytes(b"selected-online-core")
            return subprocess.CompletedProcess(argv, 0)

        with (
            patch(f"{MODULE}.run", side_effect=command),
            patch(f"{MODULE}.subprocess.run", side_effect=query),
            patch(
                f"{MODULE}.Path",
                side_effect=lambda p: (
                    transaction.work / "missing-local-repo"
                    if p == "/opt/arches-repo"
                    else Path(p)
                ),
            ),
        ):
            # Exercise the actual helper's empty-database failure, not a stubbed set.
            assert (
                mod._query_available_packages(
                    transaction.config,
                    dbpath=transaction.dbpath,
                    gpgdir=transaction.gpgdir,
                )
                is None
            )
            calls.clear()
            if sync_fails:
                with pytest.raises(subprocess.CalledProcessError):
                    mod._pacstrap(x86_64_platform, template, None, None, transaction)
                assert len(calls) == 1 and "-Sy" in calls[0]
            else:
                mod._pacstrap(x86_64_platform, template, None, None, transaction)
                assert ["-Sy" in c for c in calls] == [True, False, False]
                assert "-Ssq" in calls[1] and calls[2][0] == "pacstrap"
                assert "unbuilt-local-package" not in calls[2]
                assert "base" in calls[2]
                assert (
                    transaction.work / "mirror/core/core.db"
                ).read_bytes() == b"selected-online-core"

    def test_sync_and_override_share_the_selected_context(self, transaction):
        import arches_installer.core.install as mod

        template = InstallTemplate.from_dict({})
        template.install.override = ["selected-override"]
        transaction.mounted = True
        mod._make_pacman_conf_with_cache(transaction)
        with patch(f"{MODULE}.run") as command, patch(f"{MODULE}.chroot_run") as chroot:
            mod.sync_chroot_databases(transaction=transaction)
            mod.install_override_packages(template, transaction=transaction)
        chroot.assert_not_called()
        commands = [call.args[0] for call in command.call_args_list]
        assert commands == [
            ["pacman"] + transaction.pacman_args + ["-Sy", "--noconfirm", "--ask=64"],
            [
                "arch-chroot",
                str(transaction.root),
                "pacman",
                "--config",
                "/"
                + (transaction.work / "chroot-pacman.conf")
                .relative_to(transaction.root)
                .as_posix(),
                "--dbpath",
                "/var/lib/pacman",
                "--gpgdir",
                "/etc/pacman.d/gnupg",
            ]
            + [
                "-S",
                "--noconfirm",
                "--overwrite",
                "*",
                "--ask=68",
                "selected-override",
            ],
        ]

    def test_authenticated_manifest_cannot_weaken_signature_policy(
        self, mnt, x86_64_platform, selected_inputs
    ):
        import hashlib
        import arches_installer.core.install as mod

        provider, inputs, manifest, seal = selected_inputs
        provider.pacman_conf = provider.pacman_conf.replace("Required", "Optional")
        manifest["config_sha256"] = hashlib.sha256(
            provider.pacman_conf.encode()
        ).hexdigest()
        seal()
        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}._preseed_pacman_keyring"),
            patch(f"{MODULE}.run") as command,
        ):
            with pytest.raises(ValueError, match="repository policy"):
                with mod.package_transaction(
                    x86_64_platform, provider=provider, inputs=inputs
                ):
                    pytest.fail("accepted unsigned policy")
        command.assert_not_called()

    @pytest.mark.parametrize("phase", ["pacstrap", "override"])
    def test_package_arguments_cannot_override_isolation(
        self, transaction, x86_64_platform, phase
    ):
        import arches_installer.core.install as mod

        template = InstallTemplate.from_dict({})
        setattr(template.install, phase, ["--gpgdir=/etc/pacman.d/gnupg"])
        mod._make_pacman_conf_with_cache(transaction)
        with (
            patch(f"{MODULE}.run") as command,
            patch(f"{MODULE}._query_available_packages", return_value=None),
        ):
            with pytest.raises(ValueError, match="package"):
                if phase == "pacstrap":
                    mod._pacstrap(x86_64_platform, template, None, None, transaction)
                else:
                    mod.install_override_packages(template, transaction=transaction)
        command.assert_not_called()

    @pytest.mark.parametrize("selected_inputs", ["arch"], indirect=True)
    def test_arch_requires_usable_isolated_mirrors(
        self, mnt, x86_64_platform, selected_inputs
    ):
        import hashlib
        import arches_installer.core.install as mod

        provider, inputs, manifest, seal = selected_inputs
        data = b"# no mirrors selected\n"
        (inputs.root / "mirrorlist").write_bytes(data)
        manifest["files"]["mirrorlist"] = hashlib.sha256(data).hexdigest()
        seal()
        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}._preseed_pacman_keyring"),
            patch(f"{MODULE}.run") as command,
        ):
            with pytest.raises(ValueError, match="mirrorlist"):
                with mod.package_transaction(
                    x86_64_platform, provider=provider, inputs=inputs
                ):
                    pytest.fail("accepted empty mirrorlist")
        command.assert_not_called()

    @pytest.mark.parametrize("explicit", [False, True])
    def test_matching_legacy_default_uses_cached_snapshot(
        self, tmp_path, mnt, iso_pkg_cache, x86_64_platform, explicit
    ):
        import arches_installer.core.install as mod
        from arches_installer.core.provider import resolve_provider

        provider = resolve_provider(x86_64_platform)
        trust = tmp_path / "legacy-trust"
        trust.mkdir()
        (trust / "trustdb.gpg").write_text("trusted-default")
        (trust / "pubring.gpg").write_text("cachyos-default")
        live_db = tmp_path / "live-sync"
        live_db.mkdir()
        (live_db / "core.db").write_text("unrelated-new-live-core")
        seen = []

        def command(argv, **kwargs):
            seen.append(argv)
            conf = Path(argv[argv.index("-C") + 1])
            text = conf.read_text()
            assert str(iso_pkg_cache) in text
            assert "Server = file://" in text
            assert "CacheDir = /var/cache/pacman/pkg" not in text
            return subprocess.CompletedProcess(argv, 0)

        def path(value):
            return {"/etc/pacman.d/gnupg": trust, "/var/lib/pacman/sync": live_db}.get(
                str(value), Path(value)
            )

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}.ISO_PKG_CACHE", iso_pkg_cache),
            patch(f"{MODULE}.load_platform_from_iso", return_value=x86_64_platform),
            patch(f"{MODULE}.Path", side_effect=path),
            patch(f"{MODULE}._wait_for_keyring"),
            patch(f"{MODULE}.run", side_effect=command),
        ):
            mod.pacstrap(
                x86_64_platform,
                InstallTemplate.from_dict({}),
                **({"provider": provider} if explicit else {}),
            )
        assert len(seen) == 1
        assert "linux-cachyos" in seen[0]
        assert (mnt / "var/lib/pacman/sync/core.db").read_bytes() == (
            iso_pkg_cache / "sync/core.db"
        ).read_bytes()
        assert (mnt / "etc/pacman.d/gnupg/pubring.gpg").read_text() == "cachyos-default"
        assert not list(mnt.glob(".arches-packages-*"))

    def test_cleanup_still_unmounts_when_config_write_fails(self, transaction):
        import arches_installer.core.install as mod

        transaction.mounted = True
        (transaction.root / "etc/pacman.conf").write_text(
            "[options]\nCacheDir = /mnt/arches-pkg-cache/\n"
        )
        with (
            patch.object(Path, "write_text", side_effect=OSError("read-only target")),
            patch(f"{MODULE}.run") as command,
        ):
            with pytest.raises(OSError, match="read-only"):
                mod._unmount_iso_cache_from_target(transaction=transaction)
        command.assert_called_once_with(
            ["umount", str(transaction.root / "mnt/arches-pkg-cache")], log=None
        )
        assert not transaction.mounted

    @pytest.mark.parametrize("failure", ["finalization", "unmount", "remount"])
    def test_cleanup_preserves_provider_policy(
        self, mnt, x86_64_platform, selected_inputs, failure
    ):
        import arches_installer.core.install as mod

        provider, inputs, _, _ = selected_inputs
        commands = []

        def command(argv, **kwargs):
            commands.append(argv)
            if argv[0] == "pacman-key":
                gpg = Path(argv[argv.index("--gpgdir") + 1])
                for name in ("pubring.gpg", "trustdb.gpg"):
                    (gpg / name).write_text("selected")
            if failure == "unmount" and argv[0] == "umount":
                raise subprocess.CalledProcessError(1, argv)
            if failure == "remount" and "remount,bind,ro" in argv:
                raise subprocess.CalledProcessError(1, argv)
            return subprocess.CompletedProcess(argv, 0)

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}.run", side_effect=command),
        ):
            with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
                with mod.package_transaction(
                    x86_64_platform, provider=provider, inputs=inputs
                ) as tx:
                    mod.install_pacman_conf(transaction=tx)
                    mod.sync_chroot_databases(transaction=tx)
                    if failure == "finalization":
                        raise RuntimeError("finalization")
        assert any(c[0] == "umount" for c in commands)
        assert tx.mounted == (failure == "unmount")
        assert not tx.work.exists()
        assert (mnt / "etc/pacman.conf").read_text() == provider.pacman_conf
        for argv in (c for c in commands if c[0] == "pacman"):
            assert "--dbpath" in argv and "--gpgdir" in argv and "--config" in argv
            assert str(tx.config) in argv
        assert not (mnt / "opt/arches-repo").exists()

    def test_target_symlink_rejected(
        self, tmp_path, mnt, x86_64_platform, selected_inputs
    ):
        import arches_installer.core.install as mod

        provider, inputs, _, _ = selected_inputs
        live = tmp_path / "live-var"
        live.mkdir()
        (mnt / "var").symlink_to(live, target_is_directory=True)
        with patch(f"{MODULE}.MOUNT_ROOT", mnt), patch(f"{MODULE}.run") as command:
            with pytest.raises(ValueError, match="Symlink"):
                with mod.package_transaction(
                    x86_64_platform, provider=provider, inputs=inputs
                ):
                    pytest.fail("accepted target symlink")
        command.assert_not_called()
        assert not list(live.iterdir())

    @pytest.mark.parametrize("contamination", ["config", "cache-identity"])
    def test_legacy_cache_requires_matching_default(
        self, mnt, iso_pkg_cache, x86_64_platform, contamination
    ):
        from dataclasses import replace
        import arches_installer.core.install as mod
        from arches_installer.core.provider import resolve_provider

        live = x86_64_platform
        provider = resolve_provider(live)
        if contamination == "config":
            live = replace(
                live, pacman_conf="[options]\n[core]\nServer = https://wrong\n"
            )
        else:
            (iso_pkg_cache / "manifest.json").write_text('{"provider": "omarchy"}')
        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}.load_platform_from_iso", return_value=live),
            patch(f"{MODULE}.ISO_PKG_CACHE", iso_pkg_cache),
            patch(f"{MODULE}._wait_for_keyring"),
            patch(f"{MODULE}._preseed_pacman_keyring"),
            patch(f"{MODULE}.run") as command,
        ):
            with pytest.raises(ValueError, match="cache"):
                with mod.package_transaction(x86_64_platform, provider=provider):
                    pytest.fail("accepted mismatched default")
        command.assert_not_called()

    def test_absent_alternative_inputs_fail_before_commands(
        self, mnt, x86_64_platform, selected_inputs
    ):
        from arches_installer.core.install import pacstrap

        provider, _, _, _ = selected_inputs
        with patch(f"{MODULE}.MOUNT_ROOT", mnt), patch(f"{MODULE}.run") as command:
            with pytest.raises(ValueError, match="isolated provider inputs"):
                pacstrap(
                    x86_64_platform, InstallTemplate.from_dict({}), provider=provider
                )
        command.assert_not_called()
        assert not (mnt / "var").exists()

    @pytest.mark.parametrize(
        "failure",
        [
            "provider",
            "channel",
            "architecture",
            "missing",
            "digest",
            "symlink",
            "writable",
            "bootstrap",
            "bootstrap-digest",
            "empty-trust",
        ],
    )
    def test_rejects_unbound_inputs(
        self, mnt, x86_64_platform, selected_inputs, failure
    ):
        from arches_installer.core.install import pacstrap

        provider, inputs, manifest, seal = selected_inputs
        if failure in ("provider", "channel", "architecture"):
            manifest[failure] = "wrong"
            seal()
        elif failure == "missing":
            (inputs.root / "manifest.json").unlink()
        elif failure == "digest":
            (inputs.root / "sync/core.db").write_text("live-cachyos-core-version-1")
        elif failure == "symlink":
            path = inputs.root / "sync/core.db"
            data = path.read_bytes()
            path.unlink()
            outside = inputs.root.parent / "live-core.db"
            outside.write_bytes(data)
            path.symlink_to(outside)
        elif failure == "writable":
            inputs.root.chmod(0o777)
        elif failure == "bootstrap-digest":
            (inputs.root / "keyrings/omarchy.gpg").write_text("invalid bootstrap trust")
        elif failure == "empty-trust":
            import hashlib

            (inputs.root / "keyrings/omarchy-trusted").write_bytes(b"")
            manifest["files"]["keyrings/omarchy-trusted"] = hashlib.sha256(
                b""
            ).hexdigest()
            seal()
        else:
            del manifest["files"]["keyrings/omarchy-trusted"]
            seal()
        with patch(f"{MODULE}.MOUNT_ROOT", mnt), patch(f"{MODULE}.run") as command:
            with pytest.raises(ValueError):
                pacstrap(
                    x86_64_platform,
                    InstallTemplate.from_dict({}),
                    provider=provider,
                    inputs=inputs,
                )
        command.assert_not_called()
        assert not (mnt / "var").exists()

    @pytest.mark.parametrize("selected_inputs", ["omarchy", "arch"], indirect=True)
    @pytest.mark.parametrize(
        "failure",
        [None, "signature", "missing-package", "query", "pacstrap", "populate"],
    )
    def test_isolated_callpath_and_cleanup(
        self, tmp_path, mnt, x86_64_platform, selected_inputs, failure
    ):
        import arches_installer.core.install as mod

        provider, inputs, _, _ = selected_inputs
        live = tmp_path / "live"
        (live / "sync").mkdir(parents=True)
        (live / "sync/core.db").write_text("live-cachyos-core-version-1")
        (live / "live-only-key.gpg").write_text("live-trust")
        packages = provider.base_packages + [
            p for v in provider.kernel.variants for p in (v.package, v.headers)
        ]
        commands, configs = [], []

        def command(argv, **kwargs):
            commands.append(argv)
            if "--config" in argv:
                conf = Path(argv[argv.index("--config") + 1])
                configs.append((conf, conf.read_text()))
            if argv[0] == "pacman-key":
                if failure == "populate" and "--populate" in argv:
                    raise subprocess.CalledProcessError(1, argv)
                gpg = Path(argv[argv.index("--gpgdir") + 1])
                gpg.mkdir(parents=True, exist_ok=True)
                (gpg / "pubring.gpg").write_bytes(b"selected")
                (gpg / "trustdb.gpg").write_bytes(b"selected")
            if failure == "signature" and "-Sw" in argv:
                raise subprocess.CalledProcessError(1, argv, stderr="invalid signature")
            if failure == "pacstrap" and argv[0] == "pacstrap":
                raise subprocess.CalledProcessError(1, argv)
            if failure == "query" and "-Ssq" in argv:
                raise subprocess.CalledProcessError(1, argv)
            names = packages if failure != "missing-package" else ["base"]
            return subprocess.CompletedProcess(argv, 0, stdout="\n".join(names))

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}.ISO_PKG_CACHE", live),
            patch(f"{MODULE}._wait_for_keyring") as wait,
            patch(f"{MODULE}.run", side_effect=command),
            patch(f"{MODULE}.subprocess.run", side_effect=command),
        ):
            if failure:
                with pytest.raises((ValueError, subprocess.CalledProcessError)):
                    mod.pacstrap(
                        x86_64_platform,
                        InstallTemplate.from_dict({}),
                        provider=provider,
                        inputs=inputs,
                    )
            else:
                mod.pacstrap(
                    x86_64_platform,
                    InstallTemplate.from_dict({}),
                    provider=provider,
                    inputs=inputs,
                )
        wait.assert_not_called()
        for argv in [c for c in commands if c[0] == "pacman"]:
            for flag, forbidden in (
                ("--config", "/etc/pacman.conf"),
                ("--dbpath", "/var/lib/pacman"),
                ("--gpgdir", "/etc/pacman.d/gnupg"),
            ):
                assert flag in argv
                assert argv[argv.index(flag) + 1] != forbidden
        assert (mnt / "var/lib/pacman/sync/core.db").read_bytes() == (
            inputs.root / "sync/core.db"
        ).read_bytes()
        assert not (mnt / "etc/pacman.d/gnupg/live-only-key.gpg").exists()
        for conf, text in configs:
            assert str(live) not in text
            assert "cachyos" not in text and "arches-local" not in text
            assert "Include" not in text
            assert not conf.exists()
        assert not list(mnt.glob(".arches-packages-*"))
        straps = [c for c in commands if c[0] == "pacstrap"]
        if failure in ("signature", "missing-package", "query", "populate"):
            assert not straps
        elif straps:
            assert "-G" in straps[0] and "-M" in straps[0] and "-c" in straps[0]
            assert "--ask=64" in straps[0]
            assert "linux-cachyos" not in straps[0]
        if failure is None:
            final = (mnt / "etc/pacman.conf").read_text()
            assert "arches-local" not in final and "file://" not in final
            assert "CacheDir" not in final
            assert (
                "omarchy" in final
                if provider.id == "omarchy"
                else "geo.mirror.pkgbuild.com" in final
            )

    def test_omarchy_full_lifecycle_is_not_accidentally_enabled(
        self, mnt, x86_64_platform, selected_inputs
    ):
        from arches_installer.core.install import install_system

        provider, inputs, _, _ = selected_inputs
        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}._pre_pacstrap_setup") as setup,
        ):
            with pytest.raises(ValueError, match="lifecycle adapter"):
                install_system(
                    x86_64_platform,
                    InstallTemplate.from_dict({}),
                    "host",
                    "user",
                    "pw",
                    provider=provider,
                    inputs=inputs,
                )
        setup.assert_not_called()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def legacy_environment(x86_64_platform):
    """The shipped default must match the selected profile; never use host trust."""
    with (
        patch(f"{MODULE}.load_platform_from_iso", return_value=x86_64_platform),
        patch(f"{MODULE}._preseed_pacman_keyring"),
        patch(f"{MODULE}._wait_for_keyring"),
        patch(f"{MODULE}._query_available_packages", return_value=None),
        patch(f"{MODULE}.ISO_PKG_CACHE", Path("/nonexistent")),
    ):
        yield


@pytest.fixture
def mnt(tmp_path: Path) -> Path:
    """Provide a temporary MOUNT_ROOT and patch it into the module."""
    mnt = tmp_path / "mnt"
    mnt.mkdir()
    # Pre-create common sub-dirs that the code writes into
    (mnt / "etc").mkdir(parents=True, exist_ok=True)
    return mnt


@pytest.fixture
def iso_pkg_cache(tmp_path: Path) -> Path:
    """Provide a temporary ISO_PKG_CACHE with a fake package."""
    cache = tmp_path / "pkg-cache"
    cache.mkdir()
    (cache / "some-package-1.0-1-x86_64.pkg.tar.zst").write_text("fake")
    sync = cache / "sync"
    sync.mkdir()
    (sync / "core.db").write_text("fake-db")
    (sync / "extra.db").write_text("fake-db")
    return cache


@pytest.fixture
def iso_pacman_conf(tmp_path: Path) -> Path:
    """Provide a temporary ISO_PACMAN_CONF file."""
    conf = tmp_path / "pacman.conf"
    conf.write_text(
        "[options]\nHoldPkg = pacman glibc\n\n"
        "[core]\nInclude = /etc/pacman.d/mirrorlist\n\n"
        "[extra]\nInclude = /etc/pacman.d/mirrorlist\n"
    )
    return conf


@pytest.fixture
def transaction(mnt, iso_pkg_cache, iso_pacman_conf, x86_64_platform):
    from dataclasses import replace
    from arches_installer.core.install import PackageTransaction
    from arches_installer.core.provider import resolve_provider

    provider = replace(
        resolve_provider(x86_64_platform), pacman_conf=iso_pacman_conf.read_text()
    )
    work = mnt / ".test-transaction"
    work.mkdir()
    return PackageTransaction(
        provider,
        mnt,
        work,
        iso_pkg_cache,
        iso_pkg_cache / "sync",
        work / "keyring",
        True,
        provider.pacman_conf,
    )


@pytest.fixture
def iso_ansible_dir(tmp_path: Path) -> Path:
    """Provide a temporary ISO_ANSIBLE_DIR with a fake playbook."""
    d = tmp_path / "ansible"
    d.mkdir()
    (d / "site.yml").write_text("---\n- hosts: all\n")
    return d


@pytest.fixture
def iso_build_host_pubkey(tmp_path: Path) -> Path:
    """Provide a temporary ISO_BUILD_HOST_PUBKEY file."""
    key = tmp_path / "build-host.pub"
    key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItest test@build")
    return key


def _patch_constants(
    mnt: Path,
    iso_pkg_cache: Path | None = None,
    iso_pacman_conf: Path | None = None,
    iso_ansible_dir: Path | None = None,
    iso_build_host_pubkey: Path | None = None,
    iso_platform_dir: Path | None = None,
):
    """Return a context manager that patches all module-level Path constants."""
    patches = {
        f"{MODULE}.MOUNT_ROOT": mnt,
    }
    if iso_pkg_cache is not None:
        patches[f"{MODULE}.ISO_PKG_CACHE"] = iso_pkg_cache
    if iso_ansible_dir is not None:
        patches[f"{MODULE}.ISO_ANSIBLE_DIR"] = iso_ansible_dir
    if iso_build_host_pubkey is not None:
        patches[f"{MODULE}.ISO_BUILD_HOST_PUBKEY"] = iso_build_host_pubkey

    # Stack all patches
    from contextlib import ExitStack

    stack = ExitStack()
    mocks = {}
    for target, value in patches.items():
        m = stack.enter_context(patch(target, value))
        mocks[target] = m
    return stack


# ---------------------------------------------------------------------------
# Tests: _setup_local_repo_mirror
# ---------------------------------------------------------------------------


class TestSetupLocalRepoMirror:
    """Tests for _setup_local_repo_mirror."""

    def test_creates_repo_dirs_from_cache_sync(
        self, tmp_path: Path, iso_pkg_cache: Path
    ):
        from arches_installer.core.install import _setup_local_repo_mirror

        with patch(f"{MODULE}.ISO_PKG_CACHE", iso_pkg_cache):
            result = _setup_local_repo_mirror(
                sync_dir=iso_pkg_cache / "sync", mirror_dir=tmp_path / "mirror"
            )

        assert result is not None
        # Should have created a dir per .db file
        assert (result / "core" / "core.db").exists()
        assert (result / "extra" / "extra.db").exists()

    def test_never_falls_back_to_live_sync(self, tmp_path: Path):
        from arches_installer.core.install import _setup_local_repo_mirror

        empty_cache = tmp_path / "empty-cache"
        empty_cache.mkdir()
        host_sync = tmp_path / "host-sync"
        host_sync.mkdir()
        (host_sync / "core.db").write_text("live-cachyos")
        with (
            patch(
                f"{MODULE}.Path",
                side_effect=lambda p: (
                    host_sync if p == "/var/lib/pacman/sync" else Path(p)
                ),
            ),
        ):
            assert (
                _setup_local_repo_mirror(
                    sync_dir=empty_cache, mirror_dir=tmp_path / "mirror"
                )
                is None
            )
        assert not (tmp_path / "mirror").exists()

    def test_returns_none_when_no_databases(self, tmp_path: Path):
        from arches_installer.core.install import _setup_local_repo_mirror

        empty_cache = tmp_path / "empty-cache"
        empty_cache.mkdir()
        (empty_cache / "sync").mkdir()  # sync dir exists but no .db files

        with patch(f"{MODULE}.ISO_PKG_CACHE", empty_cache):
            # Host sync Path("/var/lib/pacman/sync") won't exist in test env
            result = _setup_local_repo_mirror(
                sync_dir=empty_cache / "sync", mirror_dir=tmp_path / "mirror"
            )

        assert result is None

    def test_returns_none_when_cache_missing(self, tmp_path: Path):
        from arches_installer.core.install import _setup_local_repo_mirror

        nonexistent = tmp_path / "nope"
        with patch(f"{MODULE}.ISO_PKG_CACHE", nonexistent):
            assert (
                _setup_local_repo_mirror(
                    sync_dir=nonexistent / "sync", mirror_dir=tmp_path / "mirror"
                )
                is None
            )


# ---------------------------------------------------------------------------
# Tests: _make_pacman_conf_with_cache
# ---------------------------------------------------------------------------


class TestMakePacmanConfWithCache:
    """Tests for _make_pacman_conf_with_cache."""

    def test_online_build_still_redirects_cache(
        self, tmp_path: Path, iso_pacman_conf: Path, transaction
    ):
        """Even without an offline cache, CacheDir should point at the target."""
        from arches_installer.core.install import _make_pacman_conf_with_cache

        empty_cache = tmp_path / "no-cache"
        empty_cache.mkdir()
        fake_mnt = transaction.root

        with (
            patch(f"{MODULE}.ISO_PKG_CACHE", empty_cache),
            patch(f"{MODULE}.MOUNT_ROOT", fake_mnt),
            patch(f"{MODULE}._setup_local_repo_mirror", return_value=None),
        ):
            transaction.cache = empty_cache
            transaction.root = fake_mnt
            result = _make_pacman_conf_with_cache(transaction)

        # Should always return a custom config (never the original)
        assert result != iso_pacman_conf
        text = result.read_text()
        # No offline cache line
        assert str(empty_cache) not in text
        # Target cache IS present
        target_cache = fake_mnt / "var" / "cache" / "pacman" / "pkg"
        assert f"CacheDir = {target_cache}/" in text
        assert "CacheDir = /var/cache/pacman/pkg/" not in text

    def test_creates_temp_conf_with_cachedir(
        self, tmp_path: Path, iso_pkg_cache: Path, iso_pacman_conf: Path, transaction
    ):
        from arches_installer.core.install import _make_pacman_conf_with_cache

        fake_mnt = transaction.root

        with (
            patch(f"{MODULE}.ISO_PKG_CACHE", iso_pkg_cache),
            patch(f"{MODULE}.MOUNT_ROOT", fake_mnt),
            patch(f"{MODULE}._setup_local_repo_mirror", return_value=None),
        ):
            transaction.root = fake_mnt
            result = _make_pacman_conf_with_cache(transaction)

        assert result != iso_pacman_conf
        text = result.read_text()
        assert f"CacheDir = {iso_pkg_cache}/" in text
        target_cache = fake_mnt / "var" / "cache" / "pacman" / "pkg"
        assert f"CacheDir = {target_cache}/" in text
        assert "CacheDir = /var/cache/pacman/pkg/" not in text

    def test_inserts_local_mirror_server_lines(
        self, tmp_path: Path, iso_pkg_cache: Path, iso_pacman_conf: Path, transaction
    ):
        from arches_installer.core.install import _make_pacman_conf_with_cache

        fake_mnt = transaction.root
        mirror_dir = tmp_path / "mirror"
        (mirror_dir / "core").mkdir(parents=True)
        (mirror_dir / "extra").mkdir(parents=True)

        with (
            patch(f"{MODULE}.ISO_PKG_CACHE", iso_pkg_cache),
            patch(f"{MODULE}.MOUNT_ROOT", fake_mnt),
            patch(f"{MODULE}._setup_local_repo_mirror", return_value=mirror_dir),
        ):
            result = _make_pacman_conf_with_cache(transaction)

        text = result.read_text()
        assert f"Server = file://{mirror_dir / 'core'}" in text
        assert f"Server = file://{mirror_dir / 'extra'}" in text


# ---------------------------------------------------------------------------
# Tests: _mount_iso_cache_in_target / _unmount_iso_cache_from_target
# ---------------------------------------------------------------------------


class TestMountIsoCacheInTarget:
    """Tests for bind-mount management of ISO package cache."""

    def test_noop_when_no_cache(self, tmp_path: Path, mnt: Path, transaction):
        from arches_installer.core.install import _mount_iso_cache_in_target

        empty_cache = tmp_path / "no-cache"
        empty_cache.mkdir()

        target_conf = mnt / "etc" / "pacman.conf"
        target_conf.write_text("[options]\nHoldPkg = pacman\n")

        with (
            patch(f"{MODULE}.ISO_PKG_CACHE", empty_cache),
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}.run") as mock_run,
        ):
            transaction.cache = empty_cache
            _mount_iso_cache_in_target(target_conf, transaction=transaction)

        mock_run.assert_not_called()

    def test_bind_mounts_and_updates_conf(
        self, tmp_path: Path, mnt: Path, iso_pkg_cache: Path, transaction
    ):
        from arches_installer.core.install import _mount_iso_cache_in_target

        target_conf = mnt / "etc" / "pacman.conf"
        target_conf.write_text("[options]\nHoldPkg = pacman\n")

        with (
            patch(f"{MODULE}.ISO_PKG_CACHE", iso_pkg_cache),
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}.run") as mock_run,
        ):
            _mount_iso_cache_in_target(target_conf, transaction=transaction)

        # Should have called mount --bind
        assert mock_run.call_count == 2
        args = mock_run.call_args_list[0][0][0]
        assert args[0] == "mount"
        assert "--bind" in args

        assert "remount,bind,ro" in mock_run.call_args_list[1][0][0]

        # pacman.conf updated with CacheDir
        text = target_conf.read_text()
        assert "/mnt/arches-pkg-cache/" in text

    def test_handles_mount_failure(
        self, tmp_path: Path, mnt: Path, iso_pkg_cache: Path, transaction
    ):
        from arches_installer.core.install import _mount_iso_cache_in_target

        target_conf = mnt / "etc" / "pacman.conf"
        target_conf.write_text("[options]\nHoldPkg = pacman\n")

        with (
            patch(f"{MODULE}.ISO_PKG_CACHE", iso_pkg_cache),
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(
                f"{MODULE}.run",
                side_effect=subprocess.CalledProcessError(1, "mount"),
            ),
        ):
            with pytest.raises(subprocess.CalledProcessError):
                _mount_iso_cache_in_target(target_conf, transaction=transaction)

        # pacman.conf should NOT be updated
        text = target_conf.read_text()
        assert "/mnt/arches-pkg-cache/" not in text


class TestUnmountIsoCacheFromTarget:
    """Tests for _unmount_iso_cache_from_target."""

    def test_noop_when_not_mounted(self, transaction):
        from arches_installer.core.install import _unmount_iso_cache_from_target

        with (
            patch(f"{MODULE}.run") as mock_run,
            patch.object(Path, "is_mount", return_value=False),
        ):
            _unmount_iso_cache_from_target(transaction=transaction)

        mock_run.assert_not_called()

    def test_unmounts_and_cleans_conf(self, mnt: Path, transaction):
        from arches_installer.core.install import _unmount_iso_cache_from_target

        # Create the mount point dir and target pacman.conf
        mount_point = mnt / "mnt" / "arches-pkg-cache"
        mount_point.mkdir(parents=True)
        target_conf = mnt / "etc" / "pacman.conf"
        target_conf.write_text(
            "[options]\nCacheDir = /mnt/arches-pkg-cache/\nHoldPkg = pacman\n"
        )

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch.object(Path, "is_mount", return_value=True),
            patch(f"{MODULE}.run") as mock_run,
        ):
            transaction.mounted = True
            _unmount_iso_cache_from_target(transaction=transaction)

        # Should have called umount
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "umount"

        # CacheDir line removed from conf
        text = target_conf.read_text()
        assert "/mnt/arches-pkg-cache/" not in text

        # Mount point cleaned up
        assert not mount_point.exists()

    def test_reports_umount_failure_without_removing_mountpoint(
        self, mnt: Path, transaction
    ):
        from arches_installer.core.install import _unmount_iso_cache_from_target

        mount_point = mnt / "mnt" / "arches-pkg-cache"
        mount_point.mkdir(parents=True)

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch.object(Path, "is_mount", return_value=True),
            patch(
                f"{MODULE}.run",
                side_effect=subprocess.CalledProcessError(1, "umount"),
            ),
        ):
            with pytest.raises(subprocess.CalledProcessError):
                transaction.mounted = True
                _unmount_iso_cache_from_target(transaction=transaction)

        assert mount_point.exists()


# ---------------------------------------------------------------------------
# Tests: _query_available_packages
# ---------------------------------------------------------------------------


class TestQueryAvailablePackages:
    """Tests for _query_available_packages."""

    def test_returns_package_set(self, tmp_path: Path):
        from arches_installer.core.install import _query_available_packages

        conf = tmp_path / "pacman.conf"
        conf.write_text("[options]\n")

        fake_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="base\nlinux\ngit\n"
        )
        with (
            patch(f"{MODULE}._wait_for_keyring"),
            patch("subprocess.run", return_value=fake_result) as mock_run,
        ):
            result = _query_available_packages(
                conf, dbpath=tmp_path / "db", gpgdir=tmp_path / "gpg"
            )

        assert result == {"base", "linux", "git"}
        mock_run.assert_called_once()

    def test_returns_none_on_failure(self, tmp_path: Path):
        from arches_installer.core.install import _query_available_packages

        conf = tmp_path / "pacman.conf"
        conf.write_text("[options]\n")

        with (
            patch(f"{MODULE}._wait_for_keyring"),
            patch(
                "subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "pacman"),
            ),
        ):
            result = _query_available_packages(
                conf, dbpath=tmp_path / "db", gpgdir=tmp_path / "gpg"
            )

        assert result is None

    def test_returns_none_on_file_not_found(self, tmp_path: Path):
        from arches_installer.core.install import _query_available_packages

        conf = tmp_path / "pacman.conf"
        conf.write_text("[options]\n")

        with (
            patch(f"{MODULE}._wait_for_keyring"),
            patch("subprocess.run", side_effect=FileNotFoundError),
        ):
            result = _query_available_packages(
                conf, dbpath=tmp_path / "db", gpgdir=tmp_path / "gpg"
            )

        assert result is None


# ---------------------------------------------------------------------------
# Tests: _preseed_pacman_databases
# ---------------------------------------------------------------------------


class TestPreseedPacmanDatabases:
    """Tests for _preseed_pacman_databases."""

    def test_copies_db_files_to_target(self, tmp_path: Path, mnt: Path):
        from arches_installer.core.install import _preseed_pacman_databases

        host_sync = tmp_path / "host-sync"
        host_sync.mkdir()
        (host_sync / "core.db").write_text("db-content")
        (host_sync / "extra.db").write_text("db-content-2")

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(
                f"{MODULE}.Path",
                side_effect=lambda p: (
                    host_sync if p == "/var/lib/pacman/sync" else Path(p)
                ),
            ),
        ):
            _preseed_pacman_databases(
                source=host_sync, target=mnt / "var/lib/pacman/sync"
            )

        target_sync = mnt / "var" / "lib" / "pacman" / "sync"
        assert (target_sync / "core.db").exists()
        assert (target_sync / "extra.db").exists()

    def test_noop_when_host_sync_missing(self, mnt: Path):
        from arches_installer.core.install import _preseed_pacman_databases

        # Don't mock Path — /var/lib/pacman/sync won't have .db files in CI
        with patch(f"{MODULE}.MOUNT_ROOT", mnt):
            # Should not raise
            _preseed_pacman_databases(
                source=mnt / "missing-sync", target=mnt / "var/lib/pacman/sync"
            )


# ---------------------------------------------------------------------------
# Tests: _preseed_pacman_keyring
# ---------------------------------------------------------------------------


class TestPreseedPacmanKeyring:
    """Tests for _preseed_pacman_keyring."""

    def _make_host_keyring(self, root: Path, *, dir_mode: int = 0o700) -> Path:
        """Create a synthetic /etc/pacman.d/gnupg layout under ``root``."""
        host_gnupg = root / "host-gnupg"
        host_gnupg.mkdir()
        # Required: trustdb.gpg presence is how the preseed detects
        # "host keyring exists".
        (host_gnupg / "trustdb.gpg").write_bytes(b"trust")
        (host_gnupg / "pubring.gpg").write_bytes(b"pub")
        (host_gnupg / "gpg.conf").write_text("# placeholder\n")
        host_gnupg.chmod(dir_mode)
        return host_gnupg

    def test_target_gnupg_is_0755_after_preseed(
        self, tmp_path: Path, mnt: Path, transaction
    ) -> None:
        """The copied target /etc/pacman.d/gnupg must end up 0755.

        Regression test: pacstrap on the LIVE ISO chmods the host
        gnupg dir to 0700 (gpg2's homedir paranoia). copytree preserves
        that mode, but on the installed system 0700 prevents
        unprivileged users from running even `pacman -Q` (it needs to
        traverse the dir + read pubring.gpg to validate db signatures).
        """
        from arches_installer.core.install import _preseed_pacman_keyring

        host_gnupg = self._make_host_keyring(tmp_path, dir_mode=0o700)

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(
                f"{MODULE}.Path",
                side_effect=lambda p: (
                    host_gnupg if p == "/etc/pacman.d/gnupg" else Path(p)
                ),
            ),
        ):
            transaction.keyring_source = host_gnupg
            _preseed_pacman_keyring(transaction=transaction)

        target_gnupg = mnt / "etc" / "pacman.d" / "gnupg"
        assert target_gnupg.is_dir()
        mode = target_gnupg.stat().st_mode & 0o777
        assert mode == 0o755, (
            f"Expected target /etc/pacman.d/gnupg at 0755 (so non-root "
            f"pacman can traverse it), got 0o{mode:03o}"
        )

        # The file contents from the host should still be present.
        assert (target_gnupg / "trustdb.gpg").read_bytes() == b"trust"
        assert (target_gnupg / "pubring.gpg").read_bytes() == b"pub"

    def test_noop_when_host_keyring_missing(self, mnt: Path, transaction) -> None:
        """If no host keyring exists, the function must not raise."""
        from arches_installer.core.install import _preseed_pacman_keyring

        nonexistent = Path("/does/not/exist/gnupg")
        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(
                f"{MODULE}.Path",
                side_effect=lambda p: (
                    nonexistent if p == "/etc/pacman.d/gnupg" else Path(p)
                ),
            ),
        ):
            # Should not raise.
            transaction.keyring_source = nonexistent
            _preseed_pacman_keyring(transaction=transaction)

        target_gnupg = mnt / "etc" / "pacman.d" / "gnupg"
        # No keyring on host = nothing copied to target.
        assert not target_gnupg.exists()


# ---------------------------------------------------------------------------
# Tests: pacstrap
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("legacy_environment")
class TestPacstrap:
    """Tests for the pacstrap function."""

    def test_assembles_correct_package_list(
        self,
        mnt: Path,
        x86_64_platform: PlatformConfig,
        dev_workstation_template: InstallTemplate,
        iso_pacman_conf: Path,
    ):
        from arches_installer.core.install import pacstrap

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}._wait_for_keyring"),
            patch(f"{MODULE}.ISO_PKG_CACHE", Path("/nonexistent")),
            patch(f"{MODULE}.run") as mock_run,
        ):
            pacstrap(x86_64_platform, dev_workstation_template)

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "pacstrap"
        assert "-c" in cmd
        assert "-M" in cmd

        # Verify package list includes base, kernel, headers, template
        packages = cmd[cmd.index(str(mnt)) + 1 :]
        assert "base" in packages
        assert "linux-cachyos" in packages
        assert "linux-cachyos-headers" in packages
        assert "linux-cachyos-lts" in packages
        assert "linux-cachyos-lts-headers" in packages
        assert "git" in packages
        assert "neovim" in packages
        assert "plasma-meta" in packages

    def test_retry_on_first_failure(
        self,
        mnt: Path,
        x86_64_platform: PlatformConfig,
        dev_workstation_template: InstallTemplate,
        iso_pacman_conf: Path,
    ):
        from arches_installer.core.install import pacstrap

        call_count = 0

        def _run_side_effect(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise subprocess.CalledProcessError(1, "pacstrap")
            return subprocess.CompletedProcess(cmd, 0)

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}._wait_for_keyring"),
            patch(f"{MODULE}.ISO_PKG_CACHE", Path("/nonexistent")),
            patch(f"{MODULE}.run", side_effect=_run_side_effect),
        ):
            pacstrap(x86_64_platform, dev_workstation_template)

        assert call_count == 2

    def test_raises_after_max_retries(
        self,
        mnt: Path,
        x86_64_platform: PlatformConfig,
        dev_workstation_template: InstallTemplate,
        iso_pacman_conf: Path,
    ):
        from arches_installer.core.install import pacstrap

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}._wait_for_keyring"),
            patch(f"{MODULE}.ISO_PKG_CACHE", Path("/nonexistent")),
            patch(
                f"{MODULE}.run",
                side_effect=subprocess.CalledProcessError(1, "pacstrap"),
            ),
        ):
            with pytest.raises(subprocess.CalledProcessError):
                pacstrap(x86_64_platform, dev_workstation_template)

    def test_defers_unavailable_packages(
        self,
        mnt: Path,
        x86_64_platform: PlatformConfig,
        dev_workstation_template: InstallTemplate,
        iso_pacman_conf: Path,
        tmp_path: Path,
    ):
        from arches_installer.core.install import pacstrap

        # Local repo is empty => triggers filtering
        empty_local = tmp_path / "arches-repo"
        # Don't create it — so it doesn't exist

        # Available packages — everything except 'plasma-meta' (a template pkg)
        available = set(x86_64_platform.base_packages)
        for v in x86_64_platform.kernel.variants:
            available.add(v.package)
            available.add(v.headers)
        available.add("git")
        available.add("neovim")
        # plasma-meta is NOT in available

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}._wait_for_keyring"),
            patch(f"{MODULE}.ISO_PKG_CACHE", Path("/nonexistent")),
            patch(f"{MODULE}._query_available_packages", return_value=available),
            patch(
                f"{MODULE}.Path",
                side_effect=lambda p: (
                    empty_local if p == "/opt/arches-repo" else Path(p)
                ),
            ),
            patch(f"{MODULE}.run") as mock_run,
        ):
            pacstrap(x86_64_platform, dev_workstation_template)

        cmd = mock_run.call_args[0][0]
        packages = cmd[cmd.index(str(mnt)) + 1 :]
        # plasma-meta should have been deferred (not in command)
        assert "plasma-meta" not in packages
        assert "git" in packages


# ---------------------------------------------------------------------------
# Tests: generate_fstab
# ---------------------------------------------------------------------------


class TestGenerateFstab:
    """Tests for generate_fstab."""

    def test_writes_fstab_file(self, mnt: Path):
        from arches_installer.core.install import generate_fstab

        fstab_content = "# /dev/vda2\nUUID=abc-123  /  btrfs  defaults  0 0\n"
        fake_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=fstab_content, stderr=""
        )

        # Don't create .system-efi as mount => is_mount() returns False

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}.run", return_value=fake_result),
        ):
            generate_fstab()

        fstab = mnt / "etc" / "fstab"
        assert fstab.exists()
        text = fstab.read_text()
        assert "UUID=abc-123" in text

    def test_filters_stale_entries(self, mnt: Path):
        from arches_installer.core.install import generate_fstab

        fstab_content = (
            "# /dev/vda2\n"
            "UUID=abc-123  /  btrfs  defaults  0 0\n"
            "\n"
            "# stale\n"
            "/run/.system-efi  /boot  vfat  defaults  0 0\n"
            "\n"
            "# swap\n"
            "/var/swap/swapfile  none  swap  defaults  0 0\n"
            "\n"
            "UUID=def-456  /home  btrfs  defaults  0 0\n"
        )
        fake_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=fstab_content, stderr=""
        )

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}.run", return_value=fake_result),
        ):
            generate_fstab()

        text = (mnt / "etc" / "fstab").read_text()
        assert "/run/.system-efi" not in text
        assert "/var/swap/swapfile" not in text
        assert "UUID=abc-123" in text
        assert "UUID=def-456" in text


# ---------------------------------------------------------------------------
# Tests: configure_var_bind_mounts
# ---------------------------------------------------------------------------


class TestConfigureVarBindMounts:
    """Tests for configure_var_bind_mounts and _bind_mount_var_path."""

    def test_binds_pacman_and_dkms(self, mnt: Path):
        from arches_installer.core.install import configure_var_bind_mounts

        # Set up both /var/lib/pacman and /var/lib/dkms
        (mnt / "var" / "lib" / "pacman" / "local").mkdir(parents=True)
        (mnt / "var" / "lib" / "dkms").mkdir(parents=True)

        fstab = mnt / "etc" / "fstab"
        fstab.write_text("UUID=abc-123  /  btrfs  defaults  0 0\n")

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch.object(Path, "is_mount", lambda self: self == mnt / "var"),
            patch(f"{MODULE}.run") as mock_run,
        ):
            configure_var_bind_mounts()

        calls = [c.args[0] for c in mock_run.call_args_list]

        # Verify both cp + mount --bind were called for pacman
        assert any(
            "cp" in cmd and str(mnt / "usr" / "lib" / "pacman") in cmd for cmd in calls
        ), f"Expected pacman cp call, got: {calls}"

        # Verify both cp + mount --bind were called for dkms
        assert any(
            "cp" in cmd and str(mnt / "usr" / "lib" / "dkms") in cmd for cmd in calls
        ), f"Expected dkms cp call, got: {calls}"

        # Verify mount --bind called twice (once per path)
        bind_calls = [c for c in calls if "mount" in c and "--bind" in c]
        assert len(bind_calls) == 2

        # Verify fstab has both entries
        text = fstab.read_text()
        assert "/usr/lib/pacman" in text
        assert "/var/lib/pacman" in text
        assert "/usr/lib/dkms" in text
        assert "/var/lib/dkms" in text
        # Original content preserved
        assert "UUID=abc-123" in text

    def test_skips_when_var_not_separate_mount(self, mnt: Path):
        from arches_installer.core.install import configure_var_bind_mounts

        (mnt / "var" / "lib" / "pacman").mkdir(parents=True)

        fstab = mnt / "etc" / "fstab"
        fstab.write_text("UUID=abc-123  /  btrfs  defaults  0 0\n")

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch.object(Path, "is_mount", return_value=False),
            patch(f"{MODULE}.run") as mock_run,
        ):
            configure_var_bind_mounts()

        mock_run.assert_not_called()
        assert "bind" not in fstab.read_text()

    def test_skips_missing_dirs_individually(self, mnt: Path):
        from arches_installer.core.install import configure_var_bind_mounts

        # Only pacman exists, dkms does not
        (mnt / "var" / "lib" / "pacman").mkdir(parents=True)

        fstab = mnt / "etc" / "fstab"
        fstab.write_text("")

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch.object(Path, "is_mount", lambda self: self == mnt / "var"),
            patch(f"{MODULE}.run") as mock_run,
        ):
            configure_var_bind_mounts()

        calls = [c.args[0] for c in mock_run.call_args_list]
        # pacman should have been processed
        assert any("cp" in cmd and "pacman" in str(cmd) for cmd in calls), (
            f"Expected pacman cp call, got: {calls}"
        )
        # dkms should NOT have been processed
        assert not any("cp" in cmd and "dkms" in str(cmd) for cmd in calls), (
            f"Did not expect dkms cp call, got: {calls}"
        )

        text = fstab.read_text()
        assert "/usr/lib/pacman" in text
        assert "dkms" not in text

    def test_creates_dest_directories(self, mnt: Path):
        from arches_installer.core.install import configure_var_bind_mounts

        (mnt / "var" / "lib" / "pacman").mkdir(parents=True)
        (mnt / "var" / "lib" / "dkms").mkdir(parents=True)

        fstab = mnt / "etc" / "fstab"
        fstab.write_text("")

        assert not (mnt / "usr" / "lib" / "pacman").exists()
        assert not (mnt / "usr" / "lib" / "dkms").exists()

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch.object(Path, "is_mount", lambda self: self == mnt / "var"),
            patch(f"{MODULE}.run"),
        ):
            configure_var_bind_mounts()

        assert (mnt / "usr" / "lib" / "pacman").is_dir()
        assert (mnt / "usr" / "lib" / "dkms").is_dir()


# ---------------------------------------------------------------------------
# Tests: install_pacman_conf
# ---------------------------------------------------------------------------


class TestInstallPacmanConf:
    """Tests for install_pacman_conf."""

    def test_copies_platform_conf(
        self, mnt: Path, tmp_path: Path, iso_pacman_conf: Path, transaction
    ):
        from arches_installer.core.install import install_pacman_conf

        platform_dir = tmp_path / "platform"
        platform_dir.mkdir()
        platform_conf = platform_dir / "pacman.conf"
        platform_conf.write_text("[options]\n# platform specific\n")

        iso_repo = tmp_path / "arches-repo-nonexistent"

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}.ISO_PKG_CACHE", Path("/nonexistent")),
            patch(f"{MODULE}._mount_iso_cache_in_target"),
            patch(
                f"{MODULE}.Path",
                side_effect=lambda p: iso_repo if p == "/opt/arches-repo" else Path(p),
            ),
        ):
            transaction.final_conf = platform_conf.read_text()
            install_pacman_conf(transaction=transaction)

        target = mnt / "etc" / "pacman.conf"
        assert target.exists()
        assert "# platform specific" in target.read_text()


# ---------------------------------------------------------------------------
# Tests: configure_locale
# ---------------------------------------------------------------------------


class TestConfigureLocale:
    """Tests for configure_locale."""

    def test_uncomments_locale_and_writes_conf(self, mnt: Path):
        from arches_installer.core.install import configure_locale

        locale_gen = mnt / "etc" / "locale.gen"
        locale_gen.write_text("#en_US.UTF-8 UTF-8\n#de_DE.UTF-8 UTF-8\n")

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}.chroot_run") as mock_chroot,
        ):
            configure_locale("en_US.UTF-8")

        # locale.gen should have en_US uncommented
        text = locale_gen.read_text()
        assert "en_US.UTF-8 UTF-8" in text
        assert text.startswith("en_US")  # no leading #
        # de_DE should still be commented
        assert "#de_DE.UTF-8" in text

        # locale-gen called
        mock_chroot.assert_called_once_with(["locale-gen"], log=None)

        # locale.conf written
        locale_conf = mnt / "etc" / "locale.conf"
        assert locale_conf.read_text() == "LANG=en_US.UTF-8\n"

        # vconsole.conf written
        vconsole = mnt / "etc" / "vconsole.conf"
        assert "KEYMAP=us" in vconsole.read_text()


# ---------------------------------------------------------------------------
# Tests: configure_timezone
# ---------------------------------------------------------------------------


class TestConfigureTimezone:
    """Tests for configure_timezone."""

    def test_creates_symlink_and_runs_hwclock(self):
        from arches_installer.core.install import configure_timezone

        with patch(f"{MODULE}.chroot_run") as mock_chroot:
            configure_timezone("America/Denver")

        assert mock_chroot.call_count == 2
        # First call: ln -sf
        ln_call = mock_chroot.call_args_list[0]
        assert "ln" in ln_call[0][0]
        assert "/usr/share/zoneinfo/America/Denver" in ln_call[0][0]
        # Second call: hwclock
        hw_call = mock_chroot.call_args_list[1]
        assert "hwclock" in hw_call[0][0]


# ---------------------------------------------------------------------------
# Tests: configure_hostname
# ---------------------------------------------------------------------------


class TestConfigureHostname:
    """Tests for configure_hostname."""

    def test_writes_hostname_hosts_and_osrelease(self, mnt: Path):
        from arches_installer.core.install import configure_hostname

        with patch(f"{MODULE}.MOUNT_ROOT", mnt):
            configure_hostname("myhost")

        assert (mnt / "etc" / "hostname").read_text() == "myhost\n"

        hosts = (mnt / "etc" / "hosts").read_text()
        assert "127.0.0.1" in hosts
        assert "myhost" in hosts

        os_release = (mnt / "etc" / "os-release").read_text()
        assert "Arches Linux" in os_release
        assert "ID=arches" in os_release


# ---------------------------------------------------------------------------
# Tests: create_user
# ---------------------------------------------------------------------------


class TestCreateUser:
    """Tests for create_user."""

    def test_creates_user_and_sets_password(self, mnt: Path):
        from arches_installer.core.install import create_user

        (mnt / "etc" / "sudoers.d").mkdir(parents=True, exist_ok=True)

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}.chroot_run") as mock_chroot,
            patch("subprocess.run") as mock_subproc,
        ):
            create_user("testuser", "testpass")

        # useradd called
        useradd_call = mock_chroot.call_args_list[0]
        cmd = useradd_call[0][0]
        assert cmd[0] == "useradd"
        assert "testuser" in cmd
        assert "wheel" in cmd
        assert "/bin/zsh" in cmd

        # chpasswd called via subprocess.run (arch-chroot)
        mock_subproc.assert_called_once()
        chpasswd_call = mock_subproc.call_args
        assert "chpasswd" in chpasswd_call[0][0]
        assert chpasswd_call[1]["input"] == "testuser:testpass\n"

        # sudoers file written
        sudoers = mnt / "etc" / "sudoers.d" / "wheel"
        assert sudoers.exists()
        assert "NOPASSWD" in sudoers.read_text()

    def test_idempotent_user_already_exists(self, mnt: Path):
        from arches_installer.core.install import create_user

        (mnt / "etc" / "sudoers.d").mkdir(parents=True, exist_ok=True)

        call_count = 0

        def _chroot_side_effect(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # useradd fails — user exists
                raise subprocess.CalledProcessError(9, "useradd")
            return subprocess.CompletedProcess(cmd, 0)

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(
                f"{MODULE}.chroot_run", side_effect=_chroot_side_effect
            ) as mock_chroot,
            patch("subprocess.run"),
        ):
            create_user("testuser", "testpass")

        # Should have called useradd (failed) then usermod
        assert mock_chroot.call_count == 2
        usermod_call = mock_chroot.call_args_list[1]
        assert "usermod" in usermod_call[0][0]


# ---------------------------------------------------------------------------
# Tests: deploy_ssh_key
# ---------------------------------------------------------------------------


class TestDeploySshKey:
    """Tests for deploy_ssh_key."""

    def test_noop_when_no_pubkey(self, tmp_path: Path):
        from arches_installer.core.install import deploy_ssh_key

        nonexistent = tmp_path / "no-key.pub"

        with patch(f"{MODULE}.ISO_BUILD_HOST_PUBKEY", nonexistent):
            # Should not raise or do anything
            deploy_ssh_key("testuser")

    def test_deploys_key(self, mnt: Path, iso_build_host_pubkey: Path):
        from arches_installer.core.install import deploy_ssh_key

        # Create the user's home and /etc/passwd
        home = mnt / "home" / "testuser"
        home.mkdir(parents=True)
        passwd = mnt / "etc" / "passwd"
        passwd.write_text("testuser:x:1000:1000::/home/testuser:/bin/zsh\n")

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}.ISO_BUILD_HOST_PUBKEY", iso_build_host_pubkey),
            patch("os.chown") as mock_chown,
        ):
            deploy_ssh_key("testuser")

        auth_keys = home / ".ssh" / "authorized_keys"
        assert auth_keys.exists()
        assert "ssh-ed25519" in auth_keys.read_text()
        # chown called for .ssh dir and authorized_keys
        assert mock_chown.call_count == 2

    def test_warns_when_user_not_in_passwd(
        self, mnt: Path, iso_build_host_pubkey: Path
    ):
        from arches_installer.core.install import deploy_ssh_key

        home = mnt / "home" / "testuser"
        home.mkdir(parents=True)
        passwd = mnt / "etc" / "passwd"
        passwd.write_text("otheruser:x:1001:1001::/home/other:/bin/bash\n")

        log_messages = []

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}.ISO_BUILD_HOST_PUBKEY", iso_build_host_pubkey),
        ):
            deploy_ssh_key("testuser", log=log_messages.append)

        # Should have logged a warning
        assert any("WARNING" in m for m in log_messages)


# ---------------------------------------------------------------------------
# Tests: enable_services
# ---------------------------------------------------------------------------


class TestEnableServices:
    """Tests for enable_services."""

    def test_enables_all_services(self):
        from arches_installer.core.install import enable_services

        services = ["NetworkManager", "sshd", "sddm"]

        with patch(f"{MODULE}.chroot_run") as mock_chroot:
            enable_services(services)

        assert mock_chroot.call_count == 3
        for i, svc in enumerate(services):
            cmd = mock_chroot.call_args_list[i][0][0]
            assert cmd == ["systemctl", "enable", svc]

    def test_empty_services_list(self):
        from arches_installer.core.install import enable_services

        with patch(f"{MODULE}.chroot_run") as mock_chroot:
            enable_services([])

        mock_chroot.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: stage_ansible
# ---------------------------------------------------------------------------


class TestStageAnsible:
    """Tests for stage_ansible."""

    def test_copies_ansible_dir(
        self,
        mnt: Path,
        iso_ansible_dir: Path,
        dev_workstation_template: InstallTemplate,
    ):
        from arches_installer.core.install import stage_ansible

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}.ISO_ANSIBLE_DIR", iso_ansible_dir),
        ):
            stage_ansible(dev_workstation_template)

        target = mnt / "opt" / "arches" / "ansible"
        assert target.exists()
        assert (target / "site.yml").exists()

    def test_noop_when_no_firstboot_roles(self, mnt: Path, iso_ansible_dir: Path):
        from arches_installer.core.install import stage_ansible
        from arches_installer.core.template import (
            AnsibleConfig,
            InstallPhases,
            InstallTemplate,
            SystemConfig,
        )

        template = InstallTemplate(
            name="Minimal",
            description="No ansible",
            system=SystemConfig(),
            install=InstallPhases(),
            ansible=AnsibleConfig(firstboot_roles=[]),
        )

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}.ISO_ANSIBLE_DIR", iso_ansible_dir),
        ):
            stage_ansible(template)

        target = mnt / "opt" / "arches" / "ansible"
        assert not target.exists()

    def test_noop_when_iso_ansible_missing(
        self, mnt: Path, tmp_path: Path, dev_workstation_template: InstallTemplate
    ):
        from arches_installer.core.install import stage_ansible

        nonexistent = tmp_path / "no-ansible"

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}.ISO_ANSIBLE_DIR", nonexistent),
        ):
            stage_ansible(dev_workstation_template)

        target = mnt / "opt" / "arches" / "ansible"
        assert not target.exists()


# ---------------------------------------------------------------------------
# Tests: run_mkinitcpio
# ---------------------------------------------------------------------------


class TestRunMkinitcpio:
    """Tests for run_mkinitcpio."""

    def test_calls_mkinitcpio(self, x86_64_platform: PlatformConfig):
        from arches_installer.core.install import run_mkinitcpio

        with patch(f"{MODULE}.chroot_run") as mock_chroot:
            run_mkinitcpio(x86_64_platform)

        mock_chroot.assert_called_once_with(["mkinitcpio", "-P"], log=None)


# ---------------------------------------------------------------------------
# Tests: run_hardware_detection
# ---------------------------------------------------------------------------


class TestRunHardwareDetection:
    """Tests for run_hardware_detection."""

    def test_runs_detection_tool(self, x86_64_platform: PlatformConfig):
        from arches_installer.core.install import run_hardware_detection

        with patch(f"{MODULE}.chroot_run") as mock_chroot:
            run_hardware_detection(x86_64_platform)

        mock_chroot.assert_called_once_with(["chwd", "-a"], log=None)

    def test_skips_when_disabled(self, aarch64_platform: PlatformConfig):
        from arches_installer.core.install import run_hardware_detection

        with patch(f"{MODULE}.chroot_run") as mock_chroot:
            run_hardware_detection(aarch64_platform)

        mock_chroot.assert_not_called()

    def test_optional_failure_does_not_raise(self, x86_64_platform: PlatformConfig):
        """When hw detection is optional, CalledProcessError is swallowed."""
        from arches_installer.core.install import run_hardware_detection

        with patch(
            f"{MODULE}.chroot_run",
            side_effect=subprocess.CalledProcessError(1, "chwd"),
        ):
            # Should not raise because optional=True
            run_hardware_detection(x86_64_platform)

    def test_mandatory_failure_raises(self):
        """When hw detection is NOT optional, CalledProcessError propagates."""
        from arches_installer.core.install import run_hardware_detection
        from arches_installer.core.platform import (
            BootloaderPlatformConfig,
            HardwareDetectionConfig,
            KernelConfig,
            KernelVariant,
            PlatformConfig,
        )

        mandatory_platform = PlatformConfig(
            name="test",
            description="test",
            arch="x86_64",
            kernel=KernelConfig(
                variants=[KernelVariant(package="linux", headers="linux-headers")]
            ),
            bootloader=BootloaderPlatformConfig(),
            hardware_detection=HardwareDetectionConfig(
                enabled=True,
                tool="some-tool",
                args=["--detect"],
                optional=False,
            ),
            base_packages=["base"],
        )

        with patch(
            f"{MODULE}.chroot_run",
            side_effect=subprocess.CalledProcessError(1, "some-tool"),
        ):
            with pytest.raises(subprocess.CalledProcessError):
                run_hardware_detection(mandatory_platform)


# ---------------------------------------------------------------------------
# Tests: copy_apple_firmware
# ---------------------------------------------------------------------------


class TestCopyAppleFirmware:
    """Tests for copy_apple_firmware."""

    def test_noop_on_non_apple_platform(
        self, x86_64_platform: PlatformConfig, mnt: Path
    ):
        from arches_installer.core.install import copy_apple_firmware

        with patch(f"{MODULE}.MOUNT_ROOT", mnt):
            copy_apple_firmware(x86_64_platform)

        # No firmware dir should be created
        assert not (mnt / "lib" / "firmware" / "vendor").exists()

    def test_noop_on_generic_aarch64(self, aarch64_platform: PlatformConfig, mnt: Path):
        from arches_installer.core.install import copy_apple_firmware

        with patch(f"{MODULE}.MOUNT_ROOT", mnt):
            copy_apple_firmware(aarch64_platform)

        assert not (mnt / "lib" / "firmware" / "vendor").exists()

    def test_copies_firmware_on_apple(
        self, aarch64_apple_platform: PlatformConfig, mnt: Path, tmp_path: Path
    ):
        from arches_installer.core.install import copy_apple_firmware

        # Create fake host firmware
        host_fw = tmp_path / "host-firmware"
        host_fw.mkdir()
        (host_fw / "wifi.bin").write_text("firmware-data")

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(
                f"{MODULE}.Path",
                side_effect=lambda p: host_fw if p == "/host-firmware" else Path(p),
            ),
        ):
            copy_apple_firmware(aarch64_apple_platform)

        target_fw = mnt / "lib" / "firmware" / "vendor"
        assert target_fw.exists()
        assert (target_fw / "wifi.bin").exists()

    def test_warns_when_no_firmware(
        self, aarch64_apple_platform: PlatformConfig, mnt: Path, tmp_path: Path
    ):
        from arches_installer.core.install import copy_apple_firmware

        log_messages = []

        # All firmware paths nonexistent
        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(
                f"{MODULE}.Path",
                side_effect=lambda p: (
                    tmp_path / "nonexistent"
                    if p
                    in (
                        "/host-firmware",
                        "/lib/firmware/vendor",
                        "/usr/lib/firmware/vendor",
                    )
                    else Path(p)
                ),
            ),
        ):
            copy_apple_firmware(aarch64_apple_platform, log=log_messages.append)

        assert any("WARNING" in m for m in log_messages)

    def test_skips_if_firmware_already_present(
        self, aarch64_apple_platform: PlatformConfig, mnt: Path, tmp_path: Path
    ):
        from arches_installer.core.install import copy_apple_firmware

        # Create existing firmware in target
        target_fw = mnt / "lib" / "firmware" / "vendor"
        target_fw.mkdir(parents=True)
        (target_fw / "existing.bin").write_text("already-here")

        # Create host firmware too
        host_fw = tmp_path / "host-firmware"
        host_fw.mkdir()
        (host_fw / "new.bin").write_text("new-firmware")

        log_messages = []

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(
                f"{MODULE}.Path",
                side_effect=lambda p: host_fw if p == "/host-firmware" else Path(p),
            ),
        ):
            copy_apple_firmware(aarch64_apple_platform, log=log_messages.append)

        # Should skip — "already present"
        assert any("already present" in m for m in log_messages)
        # Should NOT have copied new firmware
        assert not (target_fw / "new.bin").exists()


# ---------------------------------------------------------------------------
# Tests: configure_apple_input
# ---------------------------------------------------------------------------


class TestConfigureAppleInput:
    """Tests for configure_apple_input."""

    def test_noop_on_non_apple(self, x86_64_platform: PlatformConfig, mnt: Path):
        from arches_installer.core.install import configure_apple_input

        with patch(f"{MODULE}.MOUNT_ROOT", mnt):
            configure_apple_input(x86_64_platform)

        assert not (mnt / "etc" / "modprobe.d" / "hid_apple.conf").exists()

    def test_configures_hid_apple(
        self, aarch64_apple_platform: PlatformConfig, mnt: Path
    ):
        from arches_installer.core.install import configure_apple_input

        with patch(f"{MODULE}.MOUNT_ROOT", mnt):
            configure_apple_input(aarch64_apple_platform)

        conf = mnt / "etc" / "modprobe.d" / "hid_apple.conf"
        assert conf.exists()
        text = conf.read_text()
        assert "swap_fn_leftctrl=1" in text
        assert "swap_opt_cmd=1" in text
        assert "fnmode=2" in text


# ---------------------------------------------------------------------------
# Tests: preseed_network_deps
# ---------------------------------------------------------------------------


class TestPreseedNetworkDeps:
    """Tests for preseed_network_deps."""

    def test_clones_omz_and_inits_rustup(self, mnt: Path):
        from arches_installer.core.install import preseed_network_deps

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}.chroot_run") as mock_chroot,
        ):
            preseed_network_deps("testuser")

        # Should be called 3 times: omz user, omz root, rustup
        assert mock_chroot.call_count == 3

        # Verify omz user clone
        user_call = mock_chroot.call_args_list[0][0][0]
        assert "su" in user_call
        assert "testuser" in user_call

        # Verify omz root clone
        root_call = mock_chroot.call_args_list[1][0][0]
        assert "git" in root_call
        assert "/root/.oh-my-zsh" in root_call

        # Verify rustup
        rustup_call = mock_chroot.call_args_list[2][0][0]
        assert "rustup" in " ".join(rustup_call)

    def test_skips_existing_omz(self, mnt: Path):
        from arches_installer.core.install import preseed_network_deps

        # Create existing omz dirs
        (mnt / "home" / "testuser" / ".oh-my-zsh").mkdir(parents=True)
        (mnt / "root" / ".oh-my-zsh").mkdir(parents=True)

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}.chroot_run") as mock_chroot,
        ):
            preseed_network_deps("testuser")

        # Only rustup should be called
        assert mock_chroot.call_count == 1
        rustup_call = mock_chroot.call_args_list[0][0][0]
        assert "rustup" in " ".join(rustup_call)

    def test_handles_clone_failure_gracefully(self, mnt: Path):
        from arches_installer.core.install import preseed_network_deps

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(
                f"{MODULE}.chroot_run",
                side_effect=subprocess.CalledProcessError(1, "git"),
            ),
        ):
            # Should not raise — failures are logged and swallowed
            preseed_network_deps("testuser")


# ---------------------------------------------------------------------------
# Tests: install_system (full pipeline)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("legacy_environment")
class TestInstallSystem:
    """Tests for the full install_system pipeline."""

    @pytest.mark.parametrize(
        "failure", [None, "pacstrap", "configure_locale", "stage_ansible"]
    )
    def test_calls_all_steps_in_order(
        self,
        mnt: Path,
        x86_64_platform: PlatformConfig,
        dev_workstation_template: InstallTemplate,
        failure,
    ):
        from arches_installer.core.install import install_system

        call_order = []

        def _track(name):
            def _fn(*args, **kwargs):
                call_order.append(name)
                if name == failure:
                    raise RuntimeError(name)

            return _fn

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(
                f"{MODULE}._pre_pacstrap_setup",
                side_effect=_track("_pre_pacstrap_setup"),
            ),
            patch(f"{MODULE}.pacstrap", side_effect=_track("pacstrap")),
            patch(f"{MODULE}.generate_fstab", side_effect=_track("generate_fstab")),
            patch(
                f"{MODULE}.configure_var_bind_mounts",
                side_effect=_track("configure_var_bind_mounts"),
            ),
            patch(
                f"{MODULE}.install_pacman_conf",
                side_effect=_track("install_pacman_conf"),
            ),
            patch(
                f"{MODULE}.sync_chroot_databases",
                side_effect=_track("sync_chroot_databases"),
            ),
            patch(
                f"{MODULE}.install_override_packages",
                side_effect=_track("install_override_packages"),
            ),
            patch(f"{MODULE}.configure_locale", side_effect=_track("configure_locale")),
            patch(
                f"{MODULE}.configure_timezone", side_effect=_track("configure_timezone")
            ),
            patch(
                f"{MODULE}.configure_hostname", side_effect=_track("configure_hostname")
            ),
            patch(f"{MODULE}.create_user", side_effect=_track("create_user")),
            patch(f"{MODULE}.deploy_ssh_key", side_effect=_track("deploy_ssh_key")),
            patch(
                f"{MODULE}.copy_apple_firmware",
                side_effect=_track("copy_apple_firmware"),
            ),
            patch(
                f"{MODULE}.configure_apple_input",
                side_effect=_track("configure_apple_input"),
            ),
            patch(
                f"{MODULE}.preseed_network_deps",
                side_effect=_track("preseed_network_deps"),
            ),
            patch(
                f"{MODULE}.run_hardware_detection",
                side_effect=_track("run_hardware_detection"),
            ),
            patch(
                f"{MODULE}._unmount_iso_cache_from_target",
                side_effect=_track("_unmount_iso_cache_from_target"),
            ),
            patch(f"{MODULE}.enable_services", side_effect=_track("enable_services")),
            patch(f"{MODULE}.stage_ansible", side_effect=_track("stage_ansible")),
        ):
            from contextlib import nullcontext

            with (
                pytest.raises(RuntimeError, match=failure) if failure else nullcontext()
            ):
                install_system(
                    x86_64_platform,
                    dev_workstation_template,
                    "myhost",
                    "testuser",
                    "testpass",
                )

        expected = [
            "_pre_pacstrap_setup",
            "pacstrap",
            "generate_fstab",
            "configure_var_bind_mounts",
            "install_pacman_conf",
            "sync_chroot_databases",
            "install_override_packages",
            "configure_locale",
            "configure_timezone",
            "configure_hostname",
            "create_user",
            "deploy_ssh_key",
            "copy_apple_firmware",
            "configure_apple_input",
            "preseed_network_deps",
            "run_hardware_detection",
            "enable_services",
            "stage_ansible",
            "_unmount_iso_cache_from_target",
        ]
        if failure:
            expected = expected[: expected.index(failure) + 1] + [
                "_unmount_iso_cache_from_target"
            ]
        assert call_order == expected

    def test_passes_correct_args_to_steps(
        self,
        mnt: Path,
        x86_64_platform: PlatformConfig,
        dev_workstation_template: InstallTemplate,
    ):
        from arches_installer.core.install import install_system

        with (
            patch(f"{MODULE}.MOUNT_ROOT", mnt),
            patch(f"{MODULE}._pre_pacstrap_setup"),
            patch(f"{MODULE}.pacstrap") as mock_pacstrap,
            patch(f"{MODULE}.generate_fstab"),
            patch(f"{MODULE}.configure_var_bind_mounts"),
            patch(f"{MODULE}.install_pacman_conf"),
            patch(f"{MODULE}.sync_chroot_databases"),
            patch(f"{MODULE}.install_override_packages") as mock_override,
            patch(f"{MODULE}.configure_locale") as mock_locale,
            patch(f"{MODULE}.configure_timezone") as mock_tz,
            patch(f"{MODULE}.configure_hostname") as mock_host,
            patch(f"{MODULE}.create_user") as mock_user,
            patch(f"{MODULE}.deploy_ssh_key") as mock_ssh,
            patch(f"{MODULE}.copy_apple_firmware") as mock_fw,
            patch(f"{MODULE}.configure_apple_input") as mock_input,
            patch(f"{MODULE}.preseed_network_deps") as mock_preseed,
            patch(f"{MODULE}.run_hardware_detection") as mock_hw,
            patch(f"{MODULE}._unmount_iso_cache_from_target"),
            patch(f"{MODULE}.enable_services") as mock_svc,
            patch(f"{MODULE}.stage_ansible") as mock_ansible,
        ):
            log_fn = MagicMock()
            install_system(
                x86_64_platform,
                dev_workstation_template,
                "myhost",
                "testuser",
                "testpass",
                log=log_fn,
            )

        mock_pacstrap.assert_called_once_with(
            x86_64_platform,
            dev_workstation_template,
            log_fn,
            hardware=None,
            provider=ANY,
            transaction=ANY,
        )
        mock_locale.assert_called_once_with(
            dev_workstation_template.system.locale, log_fn
        )
        mock_tz.assert_called_once_with(
            dev_workstation_template.system.timezone, log_fn
        )
        mock_host.assert_called_once_with("myhost", log_fn)
        mock_user.assert_called_once_with("testuser", "testpass", log_fn)
        mock_ssh.assert_called_once_with("testuser", log_fn)
        mock_svc.assert_called_once_with(dev_workstation_template.services, log_fn)
        mock_ansible.assert_called_once_with(dev_workstation_template, log_fn)
        mock_fw.assert_called_once_with(x86_64_platform, log_fn)
        mock_input.assert_called_once_with(x86_64_platform, log_fn)
        mock_preseed.assert_called_once_with(
            "testuser", log_fn, template=dev_workstation_template
        )
        mock_hw.assert_called_once_with(x86_64_platform, log_fn)
        mock_override.assert_called_once_with(
            dev_workstation_template, log_fn, transaction=ANY
        )
