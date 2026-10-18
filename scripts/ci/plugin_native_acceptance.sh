#!/usr/bin/env bash
# Reusable native acceptance for this exact checkout and its integration builds.
set -euo pipefail
native_python=${1:?Pass the host Python executable}
native_results=${2:?Pass a private JUnit result path}
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

"$native_python" -m pytest -q \
  tests/agent/test_plugin_session_runtime.py \
  tests/agent/test_plugin_llm_sessions.py \
  tests/agent/test_plugin_native_questions.py \
  tests/agent/test_plugin_session_children.py \
  tests/agent/test_plugin_session_credentials.py \
  tests/agent/test_plugin_session_image_scope.py \
  tests/agent/test_plugin_session_recovery.py \
  tests/agent/transports/test_codex_app_server_supervisor.py \
  tests/plugins/test_workspace_trees.py \
  tests/scripts/test_plugin_native_sandbox.py \
  --junitxml="$native_results"
"$native_python" -c 'import sys,xml.etree.ElementTree as E; suites=E.parse(sys.argv[1]).getroot().iter("testsuite"); assert sum(int(s.get("skipped", "0")) for s in suites)==0, "Native acceptance must not skip"' "$native_results"
