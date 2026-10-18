#!/usr/bin/env bash
# Enable bubblewrap's own sandbox on Ubuntu's disposable Actions runner.
set -euo pipefail

print_profile() {
  cat <<'PROFILE'
abi <abi/4.0>,
include <tunables/global>
profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
}
PROFILE
}

case "${1:-}" in
  --print-profile) print_profile; exit 0 ;;
  '') ;;
  *) echo 'Unsupported native CI setup argument' >&2; exit 2 ;;
esac

if [[ "${GITHUB_ACTIONS:-}" != true || "${RUNNER_OS:-}" != Linux ]]; then
  echo 'Native CI setup is restricted to disposable GitHub Linux runners' >&2
  exit 2
fi

if [[ ! -x /usr/bin/bwrap ]] || ! command -v apparmor_parser >/dev/null; then
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends bubblewrap apparmor
fi

probe() {
  /usr/bin/bwrap --unshare-user --unshare-net --ro-bind / / /bin/true
}

if probe; then
  echo 'Bubblewrap user namespaces are ready'
  exit 0
fi

# Ubuntu documents an application-scoped userns opt-in for sandbox builders.
# Keep the kernel-wide restriction and Codex filesystem isolation enabled.
profile_path=$(mktemp "$RUNNER_TEMP/hermes-native-bwrap.XXXXXX")
print_profile >"$profile_path"
sudo apparmor_parser --replace "$profile_path"
probe
echo 'Bubblewrap user namespaces are ready with the scoped CI profile'
