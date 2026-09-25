#!/usr/bin/env bash
set -Eeuo pipefail
# shellcheck source=lib/common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib/common.sh"
usage() {
  cat <<'EOF'
Usage: gitops-storage.sh

Run as root on the first node after gitops-cilium-and-flux.sh.
Connect elektrokube-storage/main to the cluster's existing Flux controllers.
Install Longhorn, CloudNativePG and Garage through Flux, including the native
CNPG StorageClass defaulting policy (requires Kubernetes 1.36 or newer).
Uses /etc/elektrokube/cluster.json and /etc/rancher/k3s/k3s.yaml.
No Flux installation, Flux CLI, GitHub credentials, or Git writes.
EOF
}
case ${1:-} in
  -h|--help) usage; exit 0 ;;
  '') ;;
  *) usage; die "Unknown argument: $1" ;;
esac
[[ $# == 0 ]] || die 'This script takes no arguments.'
root_only; debian_only; lock_host
[[ -s /etc/elektrokube/cluster.json && -s /etc/elektrokube/node-identity &&
   -s /etc/rancher/k3s/k3s.yaml && -x /usr/local/bin/k3s ]] || die 'Bootstrap the first node before adding storage GitOps.'
case $(cat /etc/elektrokube/node-identity) in
  hybrid:true:*|controller:true:*) ;;
  *) die 'Run this script on the original first node.' ;;
esac
command -v git >/dev/null || die 'Missing git; complete prepare-admin.sh first.'
wait_api
# Use Debian's interpreter: APT installs PyYAML for /usr/bin/python3.
if ! /usr/bin/python3 -c 'import yaml' >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3-yaml
fi
umask 077
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
/usr/bin/python3 "$REPO_ROOT/lib/gitops_storage.py" --workdir "$tmp"
