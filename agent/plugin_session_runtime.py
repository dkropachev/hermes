"""Exact executable files needed by a configured native session launcher."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess


# Resolve optional packages with the same Node instance and module search path
# as the npm launcher. Python and Node need not have the same architecture.
_NPM_EXECUTABLE = r"""
const {createRequire} = require('node:module');
const path = require('node:path');
const entry = process.argv[1];
const platform = process.platform === 'android' ? 'linux' : process.platform;
const triples = {
  'linux-x64': 'x86_64-unknown-linux-musl',
  'linux-arm64': 'aarch64-unknown-linux-musl',
  'darwin-x64': 'x86_64-apple-darwin',
  'darwin-arm64': 'aarch64-apple-darwin',
  'win32-x64': 'x86_64-pc-windows-msvc',
  'win32-arm64': 'aarch64-pc-windows-msvc',
};
const target = triples[`${platform}-${process.arch}`];
if (!target) throw new Error('Unsupported Codex package platform');
let vendor = path.resolve(path.dirname(entry), '..', 'vendor');
try {
  const metadata = createRequire(entry).resolve(`@openai/codex-${platform}-${process.arch}/package.json`);
  vendor = path.join(path.dirname(metadata), 'vendor');
} catch (error) {
  if (error.code !== 'MODULE_NOT_FOUND') throw error;
}
console.log(JSON.stringify(path.join(vendor, target, 'bin', platform === 'win32' ? 'codex.exe' : 'codex')));
"""


def codex_runtime_files(codex_bin: str) -> tuple[Path, ...]:
    """Find executable files without allowing an installation directory wholesale.

    A package-manager shim is not the executable Codex re-enters inside its
    sandbox. Recognize the official npm entrypoint and resolve its native
    optional dependency before constructing filesystem permissions.
    """
    launcher = Path(shutil.which(codex_bin) or codex_bin).resolve()
    executable = launcher
    if launcher.name == 'codex.js' and launcher.parent.name == 'bin':
        metadata = launcher.parent.parent / 'package.json'
        try:
            package = json.loads(metadata.read_text())
        except (OSError, ValueError):
            package = {}
        if (isinstance(package, dict) and package.get('name') == '@openai/codex'
                and isinstance(package.get('bin'), dict) and package['bin'].get('codex') == 'bin/codex.js'):
            try:
                result = subprocess.run(
                    ['node', '-e', _NPM_EXECUTABLE, str(launcher)],
                    check=True, capture_output=True, text=True, timeout=10,
                    stdin=subprocess.DEVNULL,
                )
                executable = Path(json.loads(result.stdout)).resolve(strict=True)
                if not executable.is_file() or not os.access(executable, os.X_OK):
                    raise ValueError('Native executable is unavailable')
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                raise ValueError('Cannot resolve the native executable for the Codex npm installation') from exc
    files = [executable]
    code_host = executable.with_name('codex-code-mode-host.exe' if executable.suffix == '.exe' else 'codex-code-mode-host')
    if code_host.is_file():
        files.append(code_host.resolve())
    return tuple(files)
