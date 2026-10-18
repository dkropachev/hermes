"""The CI prerequisite is opt-in, scoped, and never runs on a developer host."""
import os
from pathlib import Path
import re
import shutil
import subprocess


SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/ci/plugin_native_sandbox.sh'


def test_native_ci_setup_refuses_non_ci_hosts(tmp_path):
    environment = dict(os.environ)
    environment.pop('GITHUB_ACTIONS', None)
    before = set(tmp_path.iterdir())
    result = subprocess.run(['bash', str(SCRIPT)], env=environment, cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert 'restricted to disposable GitHub Linux runners' in result.stderr
    assert set(tmp_path.iterdir()) == before


def test_native_profile_is_scoped_to_bubblewrap_and_parses(tmp_path):
    result = subprocess.run(['bash', str(SCRIPT), '--print-profile'], check=True,
                            capture_output=True, text=True)
    declarations = re.findall(r'^profile\s+(\S+)\s+(\S+)\s+flags=\(([^)]+)\)\s*\{', result.stdout, re.M)
    assert len(declarations) == 1
    assert declarations[0][1] == '/usr/bin/bwrap'
    assert set(declarations[0][2].split(',')) == {'unconfined'}
    assert re.findall(r'^\s*(\w+),$', result.stdout, re.M) == ['userns']
    parser = shutil.which('apparmor_parser')
    if parser:
        profile = tmp_path / 'bubblewrap.apparmor'
        profile.write_text(result.stdout)
        subprocess.run([parser, '--skip-kernel-load', '--skip-cache', '--quiet', str(profile)],
                       check=True, capture_output=True)
