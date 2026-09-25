#!/usr/bin/env bash
# Sourcing this file does not modify the host.
REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
log() { printf '\n[elektrokube] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }
root_only() { [[ $EUID == 0 ]] || die 'Run this command with sudo/root.'; }
debian_only() {
  # shellcheck source=/dev/null
  source /etc/os-release
  [[ $ID == debian && $VERSION_ID == 13 ]] || die 'A fresh Debian 13 host is required.'
  [[ -d /run/systemd/system ]] || die 'A booted systemd host is required.'
  case $(uname -m) in x86_64|aarch64) ;; *) die 'Only amd64 and arm64 are supported.' ;; esac
}
arch() { case $(uname -m) in x86_64) echo amd64 ;; aarch64) echo arm64 ;; *) die 'Unsupported CPU.' ;; esac; }
lock_host() {
  # Child scripts inherit this lock; independent concurrent provisioning fails.
  if [[ ${ELEKTROKUBE_LOCKED:-} != 1 ]]; then
    exec 9>/run/lock/elektrokube.lock
    flock -n 9 || die 'Another provisioning operation is running.'
    export ELEKTROKUBE_LOCKED=1
  fi
}
load_config() {
  local data
  data=$(python3 "$REPO_ROOT/lib/config.py" env --config "$1") || die 'Invalid cluster configuration.'
  # Values come exclusively from a validated JSON schema and shlex.quote.
  eval "$data"
}
download() { curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 --retry 3 --connect-timeout 15 "$1" -o "$2"; }
kube() { /usr/local/bin/k3s kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml "$@"; }
install_helm() (
  if command -v helm >/dev/null && [[ $(helm version --template '{{.Version}}') == "$HELM_VERSION" ]]; then exit; fi
  local_tmp=$(mktemp -d); trap 'rm -rf -- "$local_tmp"' EXIT
  archive="helm-${HELM_VERSION}-linux-$(arch).tar.gz"
  download "https://get.helm.sh/$archive" "$local_tmp/$archive"
  download "https://get.helm.sh/$archive.sha256sum" "$local_tmp/checksums"
  (cd "$local_tmp" && sha256sum --check checksums)
  tar --no-same-owner -xzf "$local_tmp/$archive" -C "$local_tmp"
  install -m 0755 "$local_tmp/linux-$(arch)/helm" /usr/local/bin/helm
)
install_cilium_cli() (
  if command -v cilium >/dev/null && cilium version --client 2>/dev/null | grep -Fq "$CILIUM_CLI_VERSION"; then exit; fi
  local_tmp=$(mktemp -d); trap 'rm -rf -- "$local_tmp"' EXIT
  archive="cilium-linux-$(arch).tar.gz"
  url="https://github.com/cilium/cilium-cli/releases/download/$CILIUM_CLI_VERSION/$archive"
  download "$url" "$local_tmp/$archive"
  download "$url.sha256sum" "$local_tmp/checksums"
  (cd "$local_tmp" && sha256sum --check checksums)
  tar --no-same-owner -xzf "$local_tmp/$archive" -C "$local_tmp" cilium
  install -m 0755 "$local_tmp/cilium" /usr/local/bin/cilium
)
require_local_ip() {
  python3 - "$1" "$REPO_ROOT/lib" <<'PY'
import json, subprocess, sys
sys.path.insert(0, sys.argv[2])
from config import ipv4
address = ipv4(sys.argv[1])
interfaces = json.loads(subprocess.check_output(['ip', '-j', '-4', 'address', 'show']))
if address not in {i['local'] for n in interfaces for i in n['addr_info']}:
    sys.exit('Requested node IP is not assigned to this host: ' + address)
PY
}
wait_api() {
  local i
  for ((i=0; i<120; i++)); do
    if kube get --raw=/readyz >/dev/null 2>&1; then return; fi
    sleep 2
  done
  die 'API did not become ready; inspect journalctl -u k3s. Rerun to resume.'
}
