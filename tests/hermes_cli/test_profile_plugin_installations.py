"""Managed profiles share installed code while retaining local policy and context."""
from pathlib import Path

import yaml

from hermes_cli.plugins import PluginManager


def config(home, *, enabled=True, marker='profile'):
    home.mkdir(parents=True, exist_ok=True)
    (home/'config.yaml').write_text(yaml.safe_dump({'plugins': {'enabled': ['shared-code'] if enabled else [],
        'disabled': [], 'entries': {'shared-code': {'settings': {'marker': marker}}}}}))


def install(home, value):
    package = home/'plugins/shared-code'
    package.mkdir(parents=True)
    (package/'plugin.yaml').write_text('name: shared-code\nversion: 0.1.0\n')
    (package/'__init__.py').write_text("BOUND_HOME=None\nMARKER=None\nCODE="+repr(value)+"\ndef register(ctx):\n global BOUND_HOME,MARKER\n BOUND_HOME=str(ctx.workspaces._home_path)\n MARKER=ctx.get_config('marker',default='missing')\n")
    return package


def isolated(tmp_path, monkeypatch):
    root = tmp_path/'hermes'
    config(root, marker='root')
    monkeypatch.setenv('HERMES_HOME', str(root))
    monkeypatch.setenv('HERMES_BUNDLED_PLUGINS', str(tmp_path/'empty-bundled'))
    monkeypatch.setenv('HERMES_ENABLE_PROJECT_PLUGINS', '0')
    return root


def test_named_profile_loads_shared_installation_with_its_own_config_and_context(tmp_path, monkeypatch):
    root = isolated(tmp_path, monkeypatch)
    package = install(root, 'root-code')
    one, two = root/'profiles/one', root/'profiles/two'
    config(one, marker='one')
    config(two, marker='two')
    managers = [PluginManager(scope_key=str(home)) for home in (one, two)]
    try:
        for manager in managers:
            manager.discover_and_load()
        modules = [manager._plugins['shared-code'].module for manager in managers]
        assert modules[0] is not modules[1]
        for home, module in zip((one, two), modules):
            assert Path(module.__file__).parent == package
            assert module.BOUND_HOME == str(home)
            assert module.MARKER == home.name
    finally:
        for manager in managers:
            manager.unload()


def test_profile_installation_wins_and_disabled_profile_does_not_inherit_root_enablement(tmp_path, monkeypatch):
    root = isolated(tmp_path, monkeypatch)
    install(root, 'root-code')
    profile = root/'profiles/selected'
    config(profile)
    install(profile, 'local-code')
    manager = PluginManager(scope_key=str(profile))
    try:
        manager.discover_and_load()
        assert manager._plugins['shared-code'].module.CODE == 'local-code'
        config(profile, enabled=False)
        manager.discover_and_load(force=True)
        loaded = manager._plugins.get('shared-code')
        assert loaded is None or not loaded.enabled or loaded.module is None
    finally:
        manager.unload()


def test_unrelated_home_does_not_inherit_an_ambient_root_installation(tmp_path, monkeypatch):
    root = isolated(tmp_path, monkeypatch)
    install(root, 'root-code')
    unrelated = tmp_path/'other-home'
    config(unrelated)
    manager = PluginManager(scope_key=str(unrelated))
    try:
        manager.discover_and_load()
        assert 'shared-code' not in manager._plugins
    finally:
        manager.unload()
