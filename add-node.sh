#!/usr/bin/env bash
set -Eeuo pipefail
# shellcheck source=lib/common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib/common.sh"
if [[ ${1:-} == --help || ${1:-} == -h ]]; then exec python3 "$REPO_ROOT/lib/enroll.py" --help; fi
root_only; debian_only; lock_host
for tool in python3 ssh ssh-keygen ssh-keyscan sshpass; do
  command -v "$tool" >/dev/null || die "Missing $tool; run prepare-admin.sh first."
done
exec python3 "$REPO_ROOT/lib/enroll.py" "$@"
