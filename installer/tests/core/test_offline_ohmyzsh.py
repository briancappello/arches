"""Local Git objects only: no packages, network, privilege, or host configuration."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest
from tests.core.test_provider_build import run, tree as tree

ROOT = Path(__file__).resolve().parents[3]
RUN = subprocess.run


@pytest.fixture
def asset_module(tmp_path, monkeypatch):
    assert importlib.util.find_spec("arches_installer.core.ohmyzsh"), (
        "Verified Oh My Zsh asset support is missing"
    )
    from arches_installer.core import ohmyzsh

    # No PATH fallback. Git subprocesses also disable all inherited Git config.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to("/usr/bin/git")
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    real_run = subprocess.run

    def local_only(args, **kwargs):
        assert args[0] == "git", args
        assert "fetch" not in args, "Network fetch is forbidden in this fixture"
        assert Path(kwargs["cwd"]).is_relative_to(tmp_path)
        assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
        if "HEAD^{commit}" in args:
            assert args[args.index("--git-dir") + 1] == str(
                Path(kwargs["cwd"]) / ".git"
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", local_only)
    return ohmyzsh


@pytest.fixture
def bundled(tmp_path, monkeypatch, asset_module):
    omz = asset_module
    source = tmp_path / "source"
    source.mkdir()
    omz.git("init", "--template=", "-b", "master", cwd=source)
    (source / "oh-my-zsh.sh").write_text("# fixture, never sourced\n")
    (source / "themes").mkdir()
    (source / "themes/fino.zsh-theme").write_text("# fixture\n")
    omz.git("add", ".", cwd=source)
    omz.git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@invalid",
        "commit",
        "-m",
        "fixture",
        cwd=source,
    )
    revision = omz.git("rev-parse", "HEAD", cwd=source).strip()
    monkeypatch.setattr(omz, "REVISION", revision)
    stage = tmp_path / "stage"
    (stage / "firstboot-assets").mkdir(parents=True)
    bundle = stage / "firstboot-assets/ohmyzsh.bundle"
    omz.git("bundle", "create", str(bundle), "refs/heads/master", cwd=source)
    manifest = {
        "version": 1,
        "ohmyzsh": {
            "revision": revision,
            "origin": omz.ORIGIN,
            "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        },
    }
    (stage / "firstboot-assets.json").write_text(json.dumps(manifest))
    return omz, stage, source


def test_clone_preserves_git_origin_updates_and_retry(bundled, tmp_path):
    omz, stage, _ = bundled
    for account in ("user", "root"):
        dest = tmp_path / account / ".oh-my-zsh"
        dest.parent.mkdir()
        assert omz.install(stage, dest)
        assert (dest / "oh-my-zsh.sh").is_file()
        assert omz.git("rev-parse", "HEAD", cwd=dest).strip() == omz.REVISION
        assert omz.git("branch", "--show-current", cwd=dest).strip() == "master"
        assert omz.git("remote", "get-url", "origin", cwd=dest).strip() == omz.ORIGIN
        assert (
            omz.git("config", "branch.master.merge", cwd=dest).strip()
            == "refs/heads/master"
        )
        (dest / "custom").mkdir()
        (dest / "custom/keep").write_text("runtime customization")
        omz.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@invalid",
            "commit",
            "--allow-empty",
            "-m",
            "runtime update",
            cwd=dest,
        )
        updated = omz.git("rev-parse", "HEAD", cwd=dest).strip()
        assert updated != omz.REVISION
        assert not omz.install(stage, dest)
        assert omz.git("rev-parse", "HEAD", cwd=dest).strip() == updated
        assert (dest / "custom/keep").read_text() == "runtime customization"


@pytest.mark.parametrize(
    "damage", ["missing", "corrupt", "revision", "origin", "symlink", "writable"]
)
def test_bad_asset_fails_before_checkout(bundled, tmp_path, damage):
    omz, stage, source = bundled
    bundle = stage / "firstboot-assets/ohmyzsh.bundle"
    manifest = stage / "firstboot-assets.json"
    if damage == "missing":
        bundle.unlink()
    elif damage == "corrupt":
        bundle.write_bytes(b"corrupt")
    elif damage in ("revision", "origin"):
        data = json.loads(manifest.read_text())
        data["ohmyzsh"][damage] = "wrong"
        manifest.write_text(json.dumps(data))
    elif damage == "symlink":
        bundle.unlink()
        bundle.symlink_to(source / "oh-my-zsh.sh")
    else:
        bundle.chmod(0o666)
    dest = tmp_path / "checkout"
    with pytest.raises((ValueError, OSError), match="(?i)oh.my.zsh|asset"):
        omz.install(stage, dest)
    assert not dest.exists()


def test_bundle_commit_must_match_even_with_matching_sha256(
    bundled, tmp_path, monkeypatch
):
    omz, stage, _ = bundled
    manifest = stage / "firstboot-assets.json"
    data = json.loads(manifest.read_text())
    monkeypatch.setattr(omz, "REVISION", "0" * 40)
    data["ohmyzsh"]["revision"] = omz.REVISION
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="(?i)Oh My Zsh"):
        omz.install(stage, tmp_path / "checkout")


def test_corrupt_cache_is_not_refetched(bundled, tmp_path):
    omz, stage, _ = bundled
    cache = tmp_path / "cache"
    cache.mkdir()
    cached = cache / f"ohmyzsh-{omz.REVISION}.bundle"
    shutil.copyfile(stage / "firstboot-assets/ohmyzsh.bundle", cached)
    assert omz.build(cache) == cached
    cached.write_bytes(b"corrupt")
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        omz.build(cache)


def test_installer_stages_contract_and_skips_network(bundled, tmp_path, monkeypatch):
    from arches_installer.core import install, provider
    from arches_installer.core.template import AnsibleConfig, InstallTemplate

    omz, stage, _ = bundled
    target = tmp_path / "target"
    monkeypatch.setattr(provider, "STAGED_ROOT", stage)
    monkeypatch.setattr(install, "MOUNT_ROOT", target)
    monkeypatch.setattr(install, "ISO_ANSIBLE_DIR", stage / "ansible")
    (stage / "ansible").mkdir()
    calls = []
    monkeypatch.setattr(install, "chroot_run", lambda args, **kw: calls.append(args))
    template = InstallTemplate.from_dict({"meta": {"name": "fixture"}})
    template.ansible = AnsibleConfig(firstboot_roles=["zsh"])
    install.stage_ansible(template)
    assert (target / "opt/arches/firstboot-assets.json").read_bytes() == (
        stage / "firstboot-assets.json"
    ).read_bytes()
    assert (target / "opt/arches/ohmyzsh.py").is_file()
    assert (target / "opt/arches/ansible/group_vars/all/ohmyzsh.yml").read_text() == (
        "zsh_offline_required: true\n"
    )
    install.preseed_network_deps("user", template=template)
    assert calls == []
    omz.validate(target / "opt/arches")


def test_new_media_missing_contract_fails_preflight(
    tmp_path, monkeypatch, x86_64_platform
):
    from arches_installer.core import pipeline, provider
    from arches_installer.core.template import AnsibleConfig, InstallTemplate

    stage = tmp_path / "stage"
    stage.mkdir()
    # Resolve policy separately so this tests the new asset gate, not metadata parsing.
    selected = provider.resolve_provider(x86_64_platform)
    monkeypatch.setattr(provider, "STAGED_ROOT", stage)
    monkeypatch.setattr(
        pipeline, "resolve_install_provider", lambda *a: (selected, None)
    )
    (stage / "target-provider.json").write_text('{"firstboot_assets": 1}')
    template = InstallTemplate.from_dict({"meta": {"name": "fixture"}})
    template.ansible = AnsibleConfig(firstboot_roles=["zsh"])
    params = pipeline.InstallParams(
        platform=x86_64_platform,
        template=template,
        device="/dev/never",
        hostname="fixture",
        username="user",
        password="fixture",
    )
    with pytest.raises(ValueError, match="(?i)asset"):
        pipeline.preflight_provider(params)


def test_build_selection_stages_only_target_zsh(bundled, tmp_path, monkeypatch):
    omz, stage, _ = bundled
    spec = importlib.util.spec_from_file_location(
        "iso_config_assets", ROOT / "scripts/iso-config.py"
    )
    iso = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(iso)
    from arches_installer.core import module, provider

    monkeypatch.setattr(module, "_MODULES_SEARCH", [ROOT / "modules"])
    monkeypatch.setattr(provider, "_PROVIDERS_SEARCH", [ROOT / "providers"])
    monkeypatch.setattr(provider, "_PLATFORMS_DIR", ROOT / "platforms")
    monkeypatch.setenv("ARCHES_TEMPLATE", "vm-server")
    monkeypatch.setenv("ARCHES_PROVIDER", "cachyos")
    cache = tmp_path / "cache"
    cache.mkdir()
    shutil.copyfile(
        stage / "firstboot-assets/ohmyzsh.bundle",
        cache / f"ohmyzsh-{omz.REVISION}.bundle",
    )
    output = tmp_path / "output"
    iso.stage_firstboot_assets(ROOT / "platforms/x86-64/platform.toml", output, cache)
    omz.validate(output)
    monkeypatch.setenv("ARCHES_PROVIDER", "omarchy")
    monkeypatch.setenv("ARCHES_TEMPLATE", "omarchy-workstation")
    iso.stage_firstboot_assets(ROOT / "platforms/x86-64/platform.toml", output, cache)
    assert not (output / "firstboot-assets").exists()
    assert json.loads((output / "firstboot-assets.json").read_text())["ohmyzsh"] is None
    # Reclaim a reused staging root, without a real chown in the fixture.
    chowns = []
    monkeypatch.setattr(iso.os, "geteuid", lambda: 0)
    monkeypatch.setattr(iso.os, "chown", lambda *args: chowns.append(args))
    iso.stage_firstboot_assets(ROOT / "platforms/x86-64/platform.toml", output, cache)
    assert chowns == [(output, 0, 0)]


def test_build_miss_requests_only_official_pin(bundled, tmp_path, monkeypatch):
    omz, stage, _ = bundled
    git = omz.git
    requested = []

    def fetch_fixture(*args, **kwargs):
        if args[0] == "fetch":
            assert args == ("fetch", "--no-tags", omz.ORIGIN, omz.REVISION)
            assert kwargs.pop("network") is True
            requested.append(args)
            return git(
                "bundle",
                "unbundle",
                str(stage / "firstboot-assets/ohmyzsh.bundle"),
                **kwargs,
            )
        return git(*args, **kwargs)

    monkeypatch.setattr(omz, "git", fetch_fixture)
    cache = tmp_path / "new-cache"
    result = omz.build(cache)
    assert result.name == f"ohmyzsh-{omz.REVISION}.bundle"
    assert omz.build(cache) == result
    assert len(requested) == 1


def test_make_asset_stage_propagates_failure_and_does_not_leak(tree):
    # Real Make recipe, with all package/build/network tools denied by tree.
    args = ["make", "-s", "_stage-firstboot-assets", "PLATFORM=x86-64"]
    result = run(tree, args + ["PROVIDER=cachyos", "TEMPLATE=vm-server"])
    assert result.returncode != 0
    assert "exit status 98" in result.stderr
    output = tree / "iso/airootfs/opt/arches"
    assert not (output / "firstboot-assets.json").exists()
    assert (
        json.loads((output / "target-provider.json").read_text())["firstboot_assets"]
        == 1
    )
    (tree / "templates/vm-server.toml").write_text(
        '[meta]\nname = "No Zsh"\n[modules]\ninclude = ["base"]\n'
    )
    result = run(tree, args + ["PROVIDER=cachyos", "TEMPLATE=vm-server"])
    assert result.returncode == 0, result.stderr
    assert not (output / "firstboot-assets").exists()
    assert json.loads((output / "firstboot-assets.json").read_text())["ohmyzsh"] is None
    result = run(tree, args + ["PROVIDER=omarchy", "TEMPLATE=omarchy-workstation"])
    assert result.returncode == 0, result.stderr
    assert not (output / "firstboot-assets").exists()
    assert json.loads((output / "firstboot-assets.json").read_text())["ohmyzsh"] is None


def test_git_ignores_inherited_hooks_and_config(bundled, tmp_path, monkeypatch):
    omz, stage, _ = bundled
    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / "config").write_text("[core]\nsshCommand = /must-not-execute\n")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(evil))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(evil / "config"))
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(evil))
    monkeypatch.setenv("GIT_DIR", str(evil))
    assert omz.install(stage, tmp_path / "checkout")
    assert not (tmp_path / "checkout/.git/config").read_text().count("must-not-execute")


def test_fetch_failure_has_no_master_fallback(bundled, tmp_path, monkeypatch):
    omz, _, _ = bundled
    git = omz.git
    calls = []

    def fail(*args, **kwargs):
        if args[0] == "fetch":
            calls.append(args)
            raise subprocess.CalledProcessError(1, args)
        return git(*args, **kwargs)

    monkeypatch.setattr(omz, "git", fail)
    cache = tmp_path / "failed-cache"
    with pytest.raises(subprocess.CalledProcessError):
        omz.build(cache)
    assert calls == [("fetch", "--no-tags", omz.ORIGIN, omz.REVISION)]
    assert list(cache.iterdir()) == []


def test_git_keeps_object_checks_with_narrow_legacy_mode_warning(
    asset_module, tmp_path, monkeypatch
):
    calls = []

    def record(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="")

    monkeypatch.setattr(subprocess, "run", record)
    asset_module.git("status", cwd=tmp_path)
    assert "fetch.fsckObjects=true" in calls[0]
    assert "fetch.fsck.zeroPaddedFilemode=warn" in calls[0]


@pytest.mark.parametrize("entrypoint", [False, True])
def test_partial_checkout_is_not_a_successful_retry(bundled, entrypoint):
    omz, stage, ancestor = bundled
    # A valid ancestor repository must not satisfy the destination check.
    dest = ancestor / "partial"
    (dest / ".git").mkdir(parents=True)
    if entrypoint:
        (dest / "oh-my-zsh.sh").write_text("# orphaned entrypoint\n")
    with pytest.raises(ValueError, match="working tree"):
        omz.install(stage, dest)


@pytest.mark.parametrize("body", ["[]", "null", "{}"])
def test_malformed_manifest_is_clear_failure(bundled, body):
    omz, stage, _ = bundled
    (stage / "firstboot-assets.json").write_text(body)
    with pytest.raises(ValueError, match="Oh My Zsh"):
        omz.validate(stage)


def test_role_rejects_incomplete_offline_contract():
    parsed = RUN(
        [
            "/usr/bin/python",
            "-c",
            """
import json, pathlib, sys, yaml
print(json.dumps(yaml.safe_load(pathlib.Path(sys.argv[1]).read_text())))
""",
            str(ROOT / "modules/zsh/ansible/tasks/main.yml"),
        ],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": "", "HOME": "/nonexistent"},
    )
    tasks = json.loads(parsed.stdout)
    guards = [t["assert"] for t in tasks if "assert" in t]
    assert any(
        "zsh_offline.stat.exists == zsh_asset_contract.stat.exists" in t["that"]
        for t in guards
    )


@pytest.mark.parametrize("contract", ["offline", "missing-pair", "legacy"])
def test_actual_role_tasks_offline_user_root_and_retry(bundled, tmp_path, contract):
    omz, stage, _ = bundled
    role = ROOT / "modules/zsh/ansible"
    helper = stage / "ohmyzsh.py"
    helper.write_text(
        Path(omz.__file__)
        .read_text()
        .replace(
            'REVISION = "6aa16696d508f368586c5dc215b0f73a41d9ff16"',
            f'REVISION = "{omz.REVISION}"',
        )
    )
    for account in ("user", "root"):
        (tmp_path / account).mkdir()
    if contract != "legacy":
        variables = stage / "ansible/group_vars/all/ohmyzsh.yml"
        variables.parent.mkdir(parents=True)
        variables.write_text("zsh_offline_required: true\n")
    if contract != "offline":
        helper.unlink()
        (stage / "firstboot-assets.json").unlink()
    # Execute YAML conditions and tasks, with only filesystem/account paths
    # mapped. This is deliberately not a replacement for native Ansible proof.
    driver = r"""
import json, os, pathlib, shutil, subprocess, sys
import jinja2, yaml
role, root, stage = map(pathlib.Path, sys.argv[1:])
env = jinja2.Environment(undefined=jinja2.StrictUndefined)
values = {"ansible_user": "fixture", "ansible_managed": "fixture"}
variables = stage / "ansible/group_vars/all/ohmyzsh.yml"
if variables.exists():
    values.update(yaml.safe_load(variables.read_text()))
def render(value):
    return env.from_string(value).render(values)
for key, value in yaml.safe_load((role / "defaults/main.yml").read_text()).items():
    values[key] = render(value) if isinstance(value, str) else value
def mapped(value):
    path = pathlib.Path(render(value))
    for prefix, dest in (("/opt/arches", stage), ("/home/fixture", root / "user"), ("/root", root / "root")):
        if path.is_relative_to(prefix):
            path = dest / path.relative_to(prefix)
            assert path.resolve().is_relative_to(root)
            return path
    raise AssertionError(f"Unmapped path: {path}")
events = []
for task in yaml.safe_load((role / "tasks/main.yml").read_text()):
    if "when" in task and not env.compile_expression(task["when"])(**values):
        continue
    account = render(task.get("become_user", "root"))
    assert account in ("fixture", "root")
    if "stat" in task:
        values[task["register"]] = {"stat": {"exists": mapped(task["stat"]["path"]).exists()}}
    elif "assert" in task:
        for condition in task["assert"]["that"]:
            assert env.compile_expression(condition)(**values), task["assert"]["fail_msg"]
    elif "command" in task:
        argv = [render(a) for a in task["command"]["argv"]]
        assert argv[:3] == ["python3", "/opt/arches/ohmyzsh.py", "/opt/arches"]
        assert argv[3] == ("/home/fixture/.oh-my-zsh" if account == "fixture" else "/root/.oh-my-zsh")
        result = subprocess.run([sys.executable, *map(str, map(mapped, argv[1:]))],
            cwd=root, env={"PATH": str(root / "bin"), "HOME": str(root)},
            capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        values[task["register"]] = {"stdout": result.stdout.strip()}
        events.append([account, bool(env.compile_expression(task["changed_when"])(**values))])
    elif "file" in task:
        spec = task["file"]
        assert spec["state"] == "directory"
        path = mapped(spec["path"])
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(int(spec["mode"], 8))
    elif "git" in task:
        # Record the legacy branch, never execute a network operation.
        spec = task["git"]
        assert render(spec["repo"]) == "https://github.com/ohmyzsh/ohmyzsh.git"
        assert spec["version"] == "master" and spec["update"] is False
        dest = mapped(spec["dest"])
        assert dest == root / ("user" if account == "fixture" else "root") / ".oh-my-zsh"
        (dest / ".git").mkdir(parents=True, exist_ok=True)
        events.append([account, "legacy"])
    elif "copy" in task or "template" in task:
        kind = "copy" if "copy" in task else "template"
        spec = task[kind]
        src = role / ("files" if kind == "copy" else "templates") / spec["src"]
        assert src.resolve().is_relative_to(role)
        dest = mapped(spec["dest"])
        assert dest.is_relative_to(root / ("user" if account == "fixture" else "root"))
        dest.write_text(render(src.read_text()) if kind == "template" else src.read_text())
        dest.chmod(int(spec["mode"], 8))
    else:
        raise AssertionError(f"Forbidden role action (including online git): {task}")
print(json.dumps(events))
"""
    if contract != "offline":
        result = RUN(
            ["/usr/bin/python", "-c", driver, str(role), str(tmp_path), str(stage)],
            env={"PATH": "", "HOME": str(tmp_path)},
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if contract == "missing-pair":
            assert result.returncode != 0, result.stdout
            assert "Incomplete Oh My Zsh offline assets" in result.stderr
            assert not (tmp_path / "user/.oh-my-zsh").exists()
            assert not (tmp_path / "root/.oh-my-zsh").exists()
        else:
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout) == [
                ["fixture", "legacy"],
                ["root", "legacy"],
            ]
        return
    for changed in (True, False):
        result = RUN(
            ["/usr/bin/python", "-c", driver, str(role), str(tmp_path), str(stage)],
            env={"PATH": "", "HOME": str(tmp_path)},
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == [["fixture", changed], ["root", changed]]
    for account, theme in (("user", "bullet-train"), ("root", "fino")):
        home = tmp_path / account
        assert (home / ".oh-my-zsh/.git").is_dir()
        assert f'ZSH_THEME="{theme}"' in (home / ".zshrc").read_text()
    assert (
        tmp_path / "user/.oh-my-zsh/custom/themes/bullet-train.zsh-theme"
    ).read_bytes() == (role / "files/bullet-train.zsh-theme").read_bytes()
    (stage / "firstboot-assets/ohmyzsh.bundle").unlink()
    result = RUN(
        ["/usr/bin/python", "-c", driver, str(role), str(tmp_path), str(stage)],
        env={"PATH": "", "HOME": str(tmp_path)},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "Missing Oh My Zsh asset" in result.stderr
