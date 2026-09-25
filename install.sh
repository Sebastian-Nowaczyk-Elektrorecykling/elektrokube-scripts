#!/usr/bin/env bash
set -Eeuo pipefail
# shellcheck source=lib/common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib/common.sh"
usage() { echo 'Usage: install.sh --node-ip IP [--admin-user USER] [--node-name NAME] [--gpu auto|none|nvidia|amd|intel] [--enable-iommu] [--skip-headlamp] [--config FILE]'; }
node_ip=; node_name=$(hostname -s); admin_user=${SUDO_USER:-}; gpu=auto
config="$REPO_ROOT/config/cluster.json"; prepare_args=(); admin_args=()
while (($#)); do
  case "$1" in
    --node-ip) node_ip=${2:?}; shift 2 ;;
    --node-name) node_name=${2:?}; shift 2 ;;
    --admin-user) admin_user=${2:?}; shift 2 ;;
    --gpu) gpu=${2:?}; shift 2 ;;
    --config) config=${2:?}; shift 2 ;;
    --enable-iommu) prepare_args+=(--enable-iommu); shift ;;
    --skip-headlamp) admin_args+=(--skip-headlamp); shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "Unknown option: $1" ;;
  esac
done
[[ -n $node_ip && -n $admin_user && $admin_user != root ]] || { usage; die 'Specify --node-ip and a non-root --admin-user (sudo supplies its user by default).'; }
getent passwd "$admin_user" >/dev/null || die 'Admin account does not exist.'
root_only; debian_only; lock_host
require_local_ip "$node_ip"
"$REPO_ROOT/prepare-node.sh" --gpu "$gpu" --config "$config" "${prepare_args[@]}"
"$REPO_ROOT/bootstrap-cluster.sh" --node-ip "$node_ip" --node-name "$node_name" --role hybrid --config "$config"
"$REPO_ROOT/prepare-admin.sh" --user "$admin_user" --config "$config" "${admin_args[@]}"
log 'First hybrid node and admin workstation installed. Follow the README acceptance checks before adding workloads.'
