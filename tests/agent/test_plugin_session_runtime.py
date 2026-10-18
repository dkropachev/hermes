"""Package-manager launchers expose only their actual native runtime files."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from agent.plugin_session_runtime import codex_runtime_files


def npm_launcher(root, native, *, optional=True):
    """Real Node launcher with npm's optional-package or bundled-vendor layout."""
    package = root / 'node_modules' / '@openai' / 'codex'
    entry = package / 'bin' / 'codex.js'
    entry.parent.mkdir(parents=True)
    (package / 'package.json').write_text(json.dumps({
        'name': '@openai/codex', 'type': 'module', 'bin': {'codex': 'bin/codex.js'},
    }))
    details = json.loads(subprocess.check_output([
        'node', '-p', 'JSON.stringify({platform:process.platform,arch:process.arch})',
    ], text=True))
    target = {
        ('linux', 'x64'): 'x86_64-unknown-linux-musl',
        ('linux', 'arm64'): 'aarch64-unknown-linux-musl',
        ('darwin', 'x64'): 'x86_64-apple-darwin',
        ('darwin', 'arm64'): 'aarch64-apple-darwin',
        ('win32', 'x64'): 'x86_64-pc-windows-msvc',
        ('win32', 'arm64'): 'aarch64-pc-windows-msvc',
    }[details['platform'], details['arch']]
    vendor = package / 'vendor'
    if optional:
        dependency = package.parent / ('codex-' + details['platform'] + '-' + details['arch'])
        dependency.mkdir()
        (dependency / 'package.json').write_text('{}')
        vendor = dependency / 'vendor'
    binary = vendor / target / 'bin' / ('codex.exe' if os.name == 'nt' else 'codex')
    binary.parent.mkdir(parents=True)
    binary.symlink_to(native)
    entry.write_text(
        '#!/usr/bin/env node\n'
        'import {spawn} from "node:child_process";\n'
        f'const child=spawn({json.dumps(str(binary))},process.argv.slice(2),{{stdio:"inherit"}});\n'
        'for(const sig of ["SIGINT","SIGTERM"])process.on(sig,()=>child.kill(sig));\n'
        'child.on("exit",(code,signal)=>signal?process.kill(process.pid,signal):process.exit(code??1));\n'
    )
    entry.chmod(0o755)
    shim = root / 'bin' / 'codex'
    shim.parent.mkdir()
    shim.symlink_to(entry)
    return shim


def test_standalone_limits_access_to_the_binary_and_companion(tmp_path):
    binary = tmp_path / 'codex'
    binary.write_bytes(b'native')
    companion = tmp_path / 'codex-code-mode-host'
    companion.write_bytes(b'companion')
    (tmp_path / 'auth.json').write_text('private')
    assert codex_runtime_files(str(binary)) == (binary, companion)


@pytest.mark.parametrize('optional', [False, True])
def test_npm_launchers_resolve_native_files_without_granting_package_access(tmp_path, optional):
    if shutil.which('node') is None:
        pytest.skip('Node is required to exercise its package resolution')
    native = tmp_path / 'native' / 'codex'
    native.parent.mkdir()
    native.write_bytes(b'native')
    native.chmod(0o755)
    companion = native.with_name('codex-code-mode-host')
    companion.write_bytes(b'companion')
    shim = npm_launcher(tmp_path / 'installation', native, optional=optional)
    assert codex_runtime_files(str(shim)) == (native, companion)
    native.unlink()
    with pytest.raises(ValueError, match='Cannot resolve the native executable'):
        codex_runtime_files(str(shim))
