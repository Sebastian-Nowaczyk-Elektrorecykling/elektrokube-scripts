#!/usr/bin/env bash
set -Eeuo pipefail
# Internal entrypoint. Configuration/identity is supplied by bootstrap or enrollment.
# shellcheck source=common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
[[ $# == 6 ]] || die 'Internal usage: install-node.sh CONFIG ROLE IP NAME FIRST TOKEN_FILE'
config=$1; role=$2; node_ip=$3; node_name=$4; first=$5; token_file=$6
root_only; debian_only; lock_host
case "$first" in true|false) ;; *) die 'Invalid first-node flag.' ;; esac
load_config "$config"
require_local_ip "$node_ip"
tmp=$(mktemp -d); trap 'rm -rf -- "$tmp"' EXIT
args=(node --config "$config" --role "$role" --node-ip "$node_ip" --node-name "$node_name")
[[ $first != true ]] || args+=(--first)
python3 "$REPO_ROOT/lib/config.py" "${args[@]}" > "$tmp/config.yaml"
identity="$role:$first:$node_name:$node_ip:$K3S_VERSION"
state=/etc/elektrokube/node-identity
if [[ -e /etc/rancher/k3s/config.yaml || -d /var/lib/rancher/k3s || -e /etc/systemd/system/k3s.service || -e /etc/systemd/system/k3s-agent.service ]]; then
  [[ -f $state && $(cat "$state") == "$identity" ]] || die 'Existing/unmanaged k3s or changed identity/version: refusing an in-place conversion.'
fi
if [[ -f /etc/rancher/k3s/config.yaml ]]; then
  cmp -s "$tmp/config.yaml" /etc/rancher/k3s/config.yaml || die 'Existing k3s configuration differs. Reconfiguration/upgrades require a maintenance procedure.'
fi
install -d -m 0700 /etc/rancher/k3s /etc/elektrokube
if [[ $first == false ]]; then
  [[ -s $token_file ]] || die 'Missing join token.'
  install -m 0600 "$token_file" /etc/rancher/k3s/join.token
fi
install -m 0600 "$tmp/config.yaml" /etc/rancher/k3s/config.yaml
printf '%s\n' "$identity" > "$state"
chmod 0600 "$state"
mode=server; service=k3s
if [[ $role == worker ]]; then mode=agent; service=k3s-agent; fi
install -d -m 0755 "/etc/systemd/system/$service.service.d"
cat > "/etc/systemd/system/$service.service.d/10-elektrokube.conf" <<'EOF'
[Unit]
Requires=elektrokube-no-swap.service
After=elektrokube-no-swap.service network-online.target
Wants=network-online.target
EOF
systemctl daemon-reload
if [[ -x /usr/local/bin/k3s && -f /etc/systemd/system/$service.service ]]; then
  [[ $(/usr/local/bin/k3s --version | head -n1) == *"$K3S_VERSION "* ]] || die 'Installed k3s version differs from the pinned version.'
  systemctl enable --now "$service"
else
  download "https://raw.githubusercontent.com/k3s-io/k3s/$K3S_VERSION/install.sh" "$tmp/install.sh"
  printf '%s  %s\n' "$K3S_INSTALLER_SHA256" "$tmp/install.sh" | sha256sum --check --status || die 'k3s installer checksum mismatch.'
  # Installer checks the downloaded k3s binary against the upstream release checksum.
  env -u K3S_URL -u K3S_TOKEN -u K3S_TOKEN_FILE -u K3S_CONFIG_FILE \
    INSTALL_K3S_VERSION="$K3S_VERSION" INSTALL_K3S_EXEC="$mode" sh "$tmp/install.sh"
fi
log "Installed $role node $node_name ($node_ip)."
