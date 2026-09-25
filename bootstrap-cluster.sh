#!/usr/bin/env bash
set -Eeuo pipefail
# shellcheck source=lib/common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib/common.sh"
usage() { echo 'Usage: bootstrap-cluster.sh [--node-ip IP] [--interface IFACE] [--role hybrid|controller] [--node-name NAME] [--config FILE]'; }
node_ip=; interface=; role=hybrid; node_name=$(hostname -s); config="$REPO_ROOT/config/cluster.json"
while (($#)); do
  case "$1" in
    --node-ip) node_ip=${2:?}; shift 2 ;;
    --interface) interface=${2:?}; shift 2 ;;
    --role) role=${2:?}; shift 2 ;;
    --node-name) node_name=${2:?}; shift 2 ;;
    --config) config=${2:?}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "Unknown option: $1" ;;
  esac
done
[[ $role == hybrid || $role == controller ]] || die 'First node must be hybrid or controller.'
root_only; debian_only; lock_host
[[ -f /etc/systemd/system/elektrokube-no-swap.service ]] || die 'Run prepare-node.sh first, or use install.sh.'
node_ip=$(select_node_ip "$config" "$node_ip" "$interface") || exit 1
require_local_ip "$node_ip"
log "Using first-node IPv4 $node_ip."
tmp=$(mktemp -d); trap 'rm -rf -- "$tmp"' EXIT
python3 "$REPO_ROOT/lib/config.py" resolve --config "$config" --node-ip "$node_ip" > "$tmp/cluster.json"
load_config "$tmp/cluster.json"
[[ $API_ADDRESS == "$node_ip" ]] || die 'For initial bootstrap, api_address must equal the first node IP. Add HA API infrastructure separately.'
install -d -m 0700 /etc/elektrokube
if [[ -e /etc/elektrokube/cluster.json ]]; then
  cmp -s "$tmp/cluster.json" /etc/elektrokube/cluster.json || die 'Cluster configuration differs from the saved bootstrap settings.'
fi
install -m 0600 "$tmp/cluster.json" /etc/elektrokube/cluster.json
"$REPO_ROOT/lib/install-node.sh" /etc/elektrokube/cluster.json "$role" "$node_ip" "$node_name" true ''
wait_api
install_helm
install_cilium_cli
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
python3 "$REPO_ROOT/lib/config.py" cilium --config /etc/elektrokube/cluster.json > "$tmp/cilium-values.json"
install -m 0600 "$tmp/cilium-values.json" /etc/elektrokube/cilium-values.json
helm repo add cilium https://helm.cilium.io --force-update
helm repo update cilium
# No --atomic: rolling back the first CNI on a timeout would strand cluster networking.
helm upgrade --install cilium cilium/cilium --namespace kube-system --version "$CILIUM_VERSION" \
  --values /etc/elektrokube/cilium-values.json --wait --timeout 10m
kube wait --for=condition=Ready "node/$node_name" --timeout=300s
cilium status --wait --wait-duration 5m
log "Cluster bootstrapped at https://$API_ADDRESS:6443. Hubble is available via localhost port-forward."
