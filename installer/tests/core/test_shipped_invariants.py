"""Invariant checks on the actual shipped templates and modules.

These tests read the real files in ``templates/`` and ``modules/`` (not
synthetic test fixtures) and assert structural invariants that must
hold across every release of Arches. Catches regressions like:

  - A new template forgets to include the ``networking`` module
    (which would also mean no mDNS, no firewalld, no sshd).
  - A new template forgets ``base`` or some other required category.
  - Auto-install files reference templates or layouts that don't exist.

They are deliberately strict — failing this suite means the repo is
shipping a broken default, even if every unit test in isolation passes.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEMPLATES_DIR = _REPO_ROOT / "templates"
_MODULES_DIR = _REPO_ROOT / "modules"
_LAYOUTS_DIR = _REPO_ROOT / "disk-layouts"

# Internal/non-workload templates that live in templates/ but aren't
# actual install templates the user picks. Skipped from "every install
# template needs..." checks.
_NON_WORKLOAD = {
    "iso.toml",  # ISO-build config, not an installable template
    "host-install.toml",  # Apple Silicon host-install (different flow)
}


def _workload_templates() -> list[Path]:
    """Return paths to all shipped workload templates."""
    return sorted(
        p
        for p in _TEMPLATES_DIR.glob("*.toml")
        if p.name not in _NON_WORKLOAD
        and not p.name.startswith("auto-install")  # autoinstall files != templates
    )


def _autoinstall_files() -> list[Path]:
    """Return paths to all shipped auto-install configs."""
    return sorted(_TEMPLATES_DIR.glob("auto-install*.toml"))


def _module_category(slug: str) -> str | None:
    """Return the category of a module, or None if not found."""
    p = _MODULES_DIR / slug / "module.toml"
    if not p.is_file():
        return None
    with open(p, "rb") as f:
        data = tomllib.load(f)
    return data.get("meta", {}).get("category")


# ---------------------------------------------------------------------------
# Templates must include the required category modules
# ---------------------------------------------------------------------------


class TestTemplateCategoryCoverage:
    """Every workload template must include certain category modules."""

    @pytest.mark.parametrize("tmpl_path", _workload_templates(), ids=lambda p: p.name)
    def test_template_includes_base_module(self, tmpl_path: Path) -> None:
        """Every install must boot a working system — base is mandatory."""
        with open(tmpl_path, "rb") as f:
            data = tomllib.load(f)
        slugs = data.get("modules", {}).get("include", [])
        categories = {_module_category(s) for s in slugs} - {None}
        assert "base" in categories, (
            f"{tmpl_path.name} has no base-category module. "
            f"Modules: {slugs}, categories: {categories}"
        )

    @pytest.mark.parametrize("tmpl_path", _workload_templates(), ids=lambda p: p.name)
    def test_template_includes_networking_module(self, tmpl_path: Path) -> None:
        """Networking is the canonical home for mDNS (avahi + nss-mdns).

        Without a networking-category module, the box can't be reached
        as ``ssh user@hostname.local`` on a DHCP-assigned IP. This is
        the discoverability mechanism we promise for headless installs.

        The module-system itself enforces this for any template with a
        service/desktop module (via CATEGORY_DEPS), but a template that
        had ONLY base+dev-toolchain modules could legitimately skip
        networking. We force it explicitly here so a future minimalist
        template doesn't accidentally drop mDNS.
        """
        with open(tmpl_path, "rb") as f:
            data = tomllib.load(f)
        slugs = data.get("modules", {}).get("include", [])
        categories = {_module_category(s) for s in slugs} - {None}
        assert "networking" in categories, (
            f"{tmpl_path.name} has no networking-category module. "
            f"This breaks mDNS .local discovery — headless installs "
            f"would have no way for operators to find the box on "
            f"DHCP networks. Modules: {slugs}"
        )


# ---------------------------------------------------------------------------
# The networking module carries mDNS prerequisites
# ---------------------------------------------------------------------------


class TestNetworkingModuleCarriesMdns:
    """The single networking module must continue to carry mDNS bits.

    These tests would fail if someone deleted avahi/nss-mdns from the
    networking module — a regression we want to catch at PR time.
    """

    def test_avahi_package_in_pacstrap(self) -> None:
        with open(_MODULES_DIR / "networking/module.toml", "rb") as f:
            data = tomllib.load(f)
        pkgs = data["install"]["pacstrap"]["packages"]
        assert "avahi" in pkgs
        assert "nss-mdns" in pkgs

    def test_avahi_daemon_service_enabled(self) -> None:
        with open(_MODULES_DIR / "networking/module.toml", "rb") as f:
            data = tomllib.load(f)
        services = data["services"]["enable"]
        assert "avahi-daemon" in services

    def test_nsswitch_patch_task_present(self) -> None:
        """The Ansible role must patch /etc/nsswitch.conf to enable
        mDNS resolution. Without this, avahi-daemon runs but
        ``getent hosts foo.local`` returns NOTFOUND."""
        tasks = (_MODULES_DIR / "networking/ansible/tasks/main.yml").read_text()
        assert "nsswitch" in tasks.lower()
        assert "mdns_minimal" in tasks


# ---------------------------------------------------------------------------
# Auto-install references must resolve
# ---------------------------------------------------------------------------


class TestAutoInstallReferences:
    """Auto-install files must reference real templates and layouts."""

    @pytest.mark.parametrize(
        "auto_path", _autoinstall_files(), ids=lambda p: p.name
    )
    def test_template_exists(self, auto_path: Path) -> None:
        with open(auto_path, "rb") as f:
            data = tomllib.load(f)
        tmpl_name = data.get("install", {}).get("template", "")
        assert tmpl_name, f"{auto_path.name} sets no install.template"
        if not tmpl_name.endswith(".toml"):
            tmpl_name += ".toml"
        assert (_TEMPLATES_DIR / tmpl_name).is_file(), (
            f"{auto_path.name} references template {tmpl_name!r} "
            f"which does not exist in {_TEMPLATES_DIR}"
        )

    @pytest.mark.parametrize(
        "auto_path", _autoinstall_files(), ids=lambda p: p.name
    )
    def test_disk_layout_exists(self, auto_path: Path) -> None:
        with open(auto_path, "rb") as f:
            data = tomllib.load(f)
        layout = data.get("install", {}).get("disk_layout", "basic.toml")
        if not layout.endswith(".toml"):
            layout += ".toml"
        assert (_LAYOUTS_DIR / layout).is_file(), (
            f"{auto_path.name} references disk layout {layout!r} "
            f"which does not exist in {_LAYOUTS_DIR}"
        )


# ---------------------------------------------------------------------------
# Sanity check — module categories are well-formed
# ---------------------------------------------------------------------------


class TestModuleCategoriesValid:
    _VALID_CATEGORIES = {
        "base",
        "networking",
        "desktop",
        "dev-toolchain",
        "topic",
        "service",
    }

    @pytest.mark.parametrize(
        "module_dir",
        sorted(p for p in _MODULES_DIR.iterdir() if (p / "module.toml").is_file()),
        ids=lambda p: p.name,
    )
    def test_module_category_is_valid(self, module_dir: Path) -> None:
        with open(module_dir / "module.toml", "rb") as f:
            data = tomllib.load(f)
        cat = data.get("meta", {}).get("category", "")
        assert cat in self._VALID_CATEGORIES, (
            f"{module_dir.name}/module.toml has invalid category "
            f"{cat!r}. Valid: {sorted(self._VALID_CATEGORIES)}"
        )

    def test_exactly_one_networking_module(self) -> None:
        """The networking-category module is the chokepoint for things
        every install needs (sshd, NetworkManager, avahi, firewalld).

        Currently there is exactly one such module. If we ever add a
        second one (e.g. a minimal-networking variant), this test
        will fail and the author MUST audit whether avahi/mDNS still
        ends up on every install, or whether to make it a separate
        always-on module."""
        networking_modules = [
            d.name
            for d in _MODULES_DIR.iterdir()
            if (d / "module.toml").is_file()
            and _module_category(d.name) == "networking"
        ]
        assert len(networking_modules) == 1, (
            f"Expected exactly one networking-category module "
            f"(the canonical home for mDNS et al). Found: "
            f"{networking_modules}. If you're adding a new networking "
            f"variant, audit avahi/nss-mdns/nsswitch placement first."
        )
