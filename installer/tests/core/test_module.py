"""Tests for the composable module system."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from arches_installer.core.module import (
    CATEGORY_ORDER,
    ModuleError,
    discover_modules,
    load_module,
    resolve_modules,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def modules_dir(tmp_path: Path) -> Path:
    """Create a temp modules directory with sample modules."""
    d = tmp_path / "modules"
    d.mkdir()

    # base module (with ansible role)
    base = d / "base"
    base.mkdir()
    (base / "module.toml").write_text("""\
[meta]
name = "Base System"
description = "Core system configuration"
category = "base"

[install.pacstrap]
packages = ["base-devel", "neovim", "git"]
""")
    ansible = base / "ansible" / "tasks"
    ansible.mkdir(parents=True)
    (ansible / "main.yml").write_text("---\n- name: test\n  debug: msg=hi\n")

    # zsh module (base category, depends on base, has ansible)
    zsh = d / "zsh"
    zsh.mkdir()
    (zsh / "module.toml").write_text("""\
[meta]
name = "Zsh"
description = "Zsh shell"
category = "base"

[install.pacstrap]
packages = ["zsh"]

[dependencies]
requires = ["base"]
""")
    zsh_ansible = zsh / "ansible" / "tasks"
    zsh_ansible.mkdir(parents=True)
    (zsh_ansible / "main.yml").write_text("---\n- name: zsh\n  debug: msg=zsh\n")

    # networking module
    net = d / "networking"
    net.mkdir()
    (net / "module.toml").write_text("""\
[meta]
name = "Networking"
description = "NetworkManager and firewalld"
category = "networking"

[install.pacstrap]
packages = ["networkmanager", "firewalld"]

[services]
enable = ["NetworkManager", "firewalld"]
""")

    # kde module (desktop, conflicts with cosmic)
    kde = d / "kde"
    kde.mkdir()
    (kde / "module.toml").write_text("""\
[meta]
name = "KDE Plasma"
description = "KDE Plasma desktop"
category = "desktop"

[install.pacstrap]
packages = ["plasma-meta", "sddm"]

[install.override]
packages = ["arches-taskmanager-patched"]

[services]
enable = ["sddm", "bluetooth"]

[dependencies]
conflicts = ["cosmic"]
""")
    kde_ansible = kde / "ansible" / "tasks"
    kde_ansible.mkdir(parents=True)
    (kde_ansible / "main.yml").write_text("---\n- name: kde\n  debug: msg=kde\n")

    # cosmic module (desktop, conflicts with kde)
    cosmic = d / "cosmic"
    cosmic.mkdir()
    (cosmic / "module.toml").write_text("""\
[meta]
name = "COSMIC"
description = "System76 COSMIC desktop"
category = "desktop"

[install.pacstrap]
packages = ["cosmic-session"]

[services]
enable = ["cosmic-greeter"]

[dependencies]
conflicts = ["kde"]
""")

    # rust module (dev-toolchain, no ansible)
    rust = d / "rust"
    rust.mkdir()
    (rust / "module.toml").write_text("""\
[meta]
name = "Rust"
description = "Rust toolchain"
category = "dev-toolchain"

[install.pacstrap]
packages = ["rustup"]
""")

    # gaming module (topic, no ansible)
    gaming = d / "gaming"
    gaming.mkdir()
    (gaming / "module.toml").write_text("""\
[meta]
name = "Gaming"
description = "Gaming packages"
category = "topic"

[install.pacstrap]
packages = ["steam"]
""")

    # postgresql module (service)
    pg = d / "postgresql"
    pg.mkdir()
    (pg / "module.toml").write_text("""\
[meta]
name = "PostgreSQL"
description = "PostgreSQL database"
category = "service"

[install.pacstrap]
packages = ["postgresql"]

[services]
enable = ["postgresql"]
""")
    pg_ansible = pg / "ansible" / "tasks"
    pg_ansible.mkdir(parents=True)
    (pg_ansible / "main.yml").write_text("---\n- name: pg\n  debug: msg=pg\n")

    return d


@pytest.fixture
def _patch_modules_dir(modules_dir: Path):
    """Patch the modules search path to use the temp directory."""
    with patch(
        "arches_installer.core.module._MODULES_SEARCH",
        [modules_dir],
    ):
        yield modules_dir


# ---------------------------------------------------------------------------
# Tests: load_module
# ---------------------------------------------------------------------------


class TestLoadModule:
    def test_load_base_module(self, modules_dir: Path) -> None:
        mod = load_module(modules_dir / "base")
        assert mod.slug == "base"
        assert mod.name == "Base System"
        assert mod.category == "base"
        assert "git" in mod.install.pacstrap
        assert mod.has_ansible_role is True

    def test_load_module_without_ansible(self, modules_dir: Path) -> None:
        mod = load_module(modules_dir / "rust")
        assert mod.slug == "rust"
        assert mod.has_ansible_role is False

    def test_load_module_with_services(self, modules_dir: Path) -> None:
        mod = load_module(modules_dir / "networking")
        assert "NetworkManager" in mod.services
        assert "firewalld" in mod.services

    def test_load_module_with_override_packages(self, modules_dir: Path) -> None:
        mod = load_module(modules_dir / "kde")
        assert "arches-taskmanager-patched" in mod.install.override

    def test_load_module_with_conflicts(self, modules_dir: Path) -> None:
        mod = load_module(modules_dir / "kde")
        assert "cosmic" in mod.conflicts

    def test_load_module_with_requires(self, modules_dir: Path) -> None:
        mod = load_module(modules_dir / "zsh")
        assert "base" in mod.requires

    def test_load_nonexistent_module(self, tmp_path: Path) -> None:
        with pytest.raises(ModuleError, match="No module.toml"):
            load_module(tmp_path / "nope")

    def test_load_module_invalid_category(self, tmp_path: Path) -> None:
        mod_dir = tmp_path / "badmod"
        mod_dir.mkdir()
        (mod_dir / "module.toml").write_text("""\
[meta]
name = "Bad"
category = "invalid_category"
""")
        with pytest.raises(ModuleError, match="invalid category"):
            load_module(mod_dir)

    def test_detect_ansible_via_iso_roles_path(self, tmp_path: Path) -> None:
        """Ansible role detection should fall back to the ISO staged path."""
        # Create a module with no colocated ansible/
        mod_dir = tmp_path / "mymod"
        mod_dir.mkdir()
        (mod_dir / "module.toml").write_text("""\
[meta]
name = "My Module"
category = "base"
""")

        # Without ISO path, no ansible detected
        mod = load_module(mod_dir)
        assert mod.has_ansible_role is False

        # Create the ISO-layout role path
        iso_role = Path("/opt/arches/ansible/roles/mymod/tasks")
        try:
            iso_role.mkdir(parents=True, exist_ok=True)
            (iso_role / "main.yml").write_text("---\n")
            mod = load_module(mod_dir)
            assert mod.has_ansible_role is True
        except PermissionError:
            pytest.skip("Cannot write to /opt/arches (not running as root)")
        finally:
            # Clean up
            try:
                import shutil

                shutil.rmtree("/opt/arches/ansible/roles/mymod")
            except (PermissionError, FileNotFoundError):
                pass


# ---------------------------------------------------------------------------
# Tests: discover_modules
# ---------------------------------------------------------------------------


class TestDiscoverModules:
    def test_discovers_all_modules(self, _patch_modules_dir: Path) -> None:
        modules = discover_modules()
        slugs = {m.slug for m in modules}
        assert "base" in slugs
        assert "kde" in slugs
        assert "rust" in slugs
        assert "postgresql" in slugs

    def test_sorted_alphabetically(self, _patch_modules_dir: Path) -> None:
        modules = discover_modules()
        slugs = [m.slug for m in modules]
        assert slugs == sorted(slugs)

    def test_skips_dirs_without_module_toml(
        self, _patch_modules_dir: Path, modules_dir: Path
    ) -> None:
        # Create a directory without module.toml
        (modules_dir / "empty_dir").mkdir()
        modules = discover_modules()
        slugs = {m.slug for m in modules}
        assert "empty_dir" not in slugs


# ---------------------------------------------------------------------------
# Tests: resolve_modules
# ---------------------------------------------------------------------------


class TestResolveModules:
    def test_valid_selection(self, _patch_modules_dir: Path) -> None:
        resolved = resolve_modules(["base", "networking", "kde"])
        assert len(resolved.modules) == 3

    def test_ordering_by_category(self, _patch_modules_dir: Path) -> None:
        resolved = resolve_modules(["kde", "base", "networking", "rust", "postgresql"])
        categories = [m.category for m in resolved.modules]
        # Should follow category order
        cat_indices = [CATEGORY_ORDER.index(c) for c in categories]
        assert cat_indices == sorted(cat_indices)

    def test_alphabetical_within_category(self, _patch_modules_dir: Path) -> None:
        resolved = resolve_modules(["base", "zsh", "networking", "kde"])
        # base and zsh are both category "base" — should be alphabetical
        base_mods = [m for m in resolved.modules if m.category == "base"]
        slugs = [m.slug for m in base_mods]
        assert slugs == sorted(slugs)

    def test_unknown_module_raises(self, _patch_modules_dir: Path) -> None:
        with pytest.raises(ModuleError, match="Unknown module 'nonexistent'"):
            resolve_modules(["base", "nonexistent"])

    def test_missing_required_module_raises(self, _patch_modules_dir: Path) -> None:
        # zsh requires base
        with pytest.raises(ModuleError, match="requires 'base'"):
            resolve_modules(["zsh", "networking", "kde"])

    def test_conflict_raises(self, _patch_modules_dir: Path) -> None:
        with pytest.raises(ModuleError, match="conflicts with"):
            resolve_modules(["base", "networking", "kde", "cosmic"])

    def test_category_dep_desktop_requires_networking(
        self, _patch_modules_dir: Path
    ) -> None:
        with pytest.raises(ModuleError, match="category 'networking'"):
            resolve_modules(["base", "kde"])

    def test_category_dep_topic_requires_desktop(
        self, _patch_modules_dir: Path
    ) -> None:
        with pytest.raises(ModuleError, match="category 'desktop'"):
            resolve_modules(["base", "networking", "gaming"])

    def test_category_dep_service_requires_networking(
        self, _patch_modules_dir: Path
    ) -> None:
        with pytest.raises(ModuleError, match="category 'networking'"):
            resolve_modules(["base", "postgresql"])

    def test_category_dep_dev_toolchain_requires_base(
        self, _patch_modules_dir: Path
    ) -> None:
        with pytest.raises(ModuleError, match="category 'base'"):
            resolve_modules(["rust"])

    def test_multiple_desktop_modules_raises(self, _patch_modules_dir: Path) -> None:
        # Even without explicit conflicts, only one desktop allowed
        # (kde and cosmic also have explicit conflicts, but this tests
        # the single-desktop constraint separately)
        with pytest.raises(ModuleError):
            resolve_modules(["base", "networking", "kde", "cosmic"])

    def test_graphical_property(self, _patch_modules_dir: Path) -> None:
        resolved = resolve_modules(["base", "networking", "kde"])
        assert resolved.graphical is True

    def test_not_graphical_without_desktop(self, _patch_modules_dir: Path) -> None:
        resolved = resolve_modules(["base", "networking", "postgresql"])
        assert resolved.graphical is False

    def test_with_explicit_available(self, modules_dir: Path) -> None:
        """Test passing available modules explicitly."""
        available = [load_module(modules_dir / "base")]
        resolved = resolve_modules(["base"], available=available)
        assert len(resolved.modules) == 1


# ---------------------------------------------------------------------------
# Tests: ResolvedModules merging
# ---------------------------------------------------------------------------


class TestResolvedModulesMerging:
    def test_merged_install_combines_packages(self, _patch_modules_dir: Path) -> None:
        resolved = resolve_modules(["base", "networking", "kde"])
        install = resolved.merged_install()
        # Packages from all modules are present
        assert "git" in install.pacstrap
        assert "networkmanager" in install.pacstrap
        assert "plasma-meta" in install.pacstrap

    def test_merged_install_deduplicates(self, _patch_modules_dir: Path) -> None:
        resolved = resolve_modules(["base", "networking", "kde"])
        install = resolved.merged_install()
        # No duplicates
        assert len(install.pacstrap) == len(set(install.pacstrap))

    def test_merged_install_includes_override(self, _patch_modules_dir: Path) -> None:
        resolved = resolve_modules(["base", "networking", "kde"])
        install = resolved.merged_install()
        assert "arches-taskmanager-patched" in install.override

    def test_merged_services(self, _patch_modules_dir: Path) -> None:
        resolved = resolve_modules(["base", "networking", "kde"])
        services = resolved.merged_services()
        assert "NetworkManager" in services
        assert "sddm" in services
        assert "bluetooth" in services

    def test_merged_services_deduplicates(self, _patch_modules_dir: Path) -> None:
        resolved = resolve_modules(["base", "networking", "kde"])
        services = resolved.merged_services()
        assert len(services) == len(set(services))

    def test_ansible_roles_only_for_modules_with_roles(
        self, _patch_modules_dir: Path
    ) -> None:
        resolved = resolve_modules(["base", "networking", "kde", "rust"])
        roles = resolved.ansible_roles
        assert "base" in roles
        assert "kde" in roles
        # networking and rust have no ansible role
        assert "networking" not in roles
        assert "rust" not in roles

    def test_ansible_roles_in_category_order(self, _patch_modules_dir: Path) -> None:
        resolved = resolve_modules(["base", "zsh", "networking", "kde", "postgresql"])
        roles = resolved.ansible_roles
        # base and zsh are both "base" category, kde is "desktop", postgresql is "service"
        # Order: base category (alphabetical: base, zsh), desktop: kde, service: postgresql
        assert roles.index("base") < roles.index("kde")
        assert roles.index("zsh") < roles.index("kde")
        assert roles.index("kde") < roles.index("postgresql")
