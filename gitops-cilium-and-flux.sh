#!/usr/bin/env bash
set -Eeuo pipefail
# shellcheck source=lib/common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib/common.sh"
usage() {
  cat <<'EOF'
Usage: gitops-cilium-and-flux.sh

Run as root on the first node after bootstrap-cluster.sh/install.sh.
Install the raw Flux manifests from elektrokube-cilium-and-flux/main, then
connect that repository to the existing Cilium release and Flux controllers.
Git is authoritative for Cilium values and chart version, including first adoption.
Bootstrap/live Cilium values and versions do not have to match Git. Flux applies
Git's desired state; changes may upgrade/downgrade Cilium and restart networking.
Release identity, ownership, and readiness checks remain in place.
Uses /etc/elektrokube/cluster.json for cluster substitutions and
/etc/rancher/k3s/k3s.yaml for Kubernetes access.
No flux bootstrap, GitHub credentials, Git writes, or Cilium reinstall.
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
   -s /etc/rancher/k3s/k3s.yaml && -x /usr/local/bin/k3s ]] || die 'Bootstrap the first node before adding GitOps.'
case $(cat /etc/elektrokube/node-identity) in
  hybrid:true:*|controller:true:*) ;;
  *) die 'Run this script on the original first node.' ;;
esac
for tool in git helm; do
  command -v "$tool" >/dev/null || die "Missing $tool; complete bootstrap-cluster.sh/prepare-admin.sh first."
done
wait_api
# Use Debian's interpreter: APT installs PyYAML for /usr/bin/python3.
if ! /usr/bin/python3 -c 'import yaml' >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3-yaml
fi
umask 077
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
/usr/bin/python3 "$REPO_ROOT/lib/gitops.py" --workdir "$tmp" --config /etc/elektrokube/cluster.json
