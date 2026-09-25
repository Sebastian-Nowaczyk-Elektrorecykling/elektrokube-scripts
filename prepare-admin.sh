#!/usr/bin/env bash
set -Eeuo pipefail
# shellcheck source=lib/common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib/common.sh"
usage() { echo 'Usage: prepare-admin.sh [--user USER] [--skip-headlamp] [--config FILE]'; }
admin_user=$(default_admin_user); headlamp=true; config="$REPO_ROOT/config/cluster.json"
while (($#)); do
  case "$1" in
    --user) admin_user=${2:?}; shift 2 ;;
    --skip-headlamp) headlamp=false; shift ;;
    --config) config=${2:?}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "Unknown option: $1" ;;
  esac
done
root_only; debian_only; lock_host
getent passwd "$admin_user" >/dev/null || die 'Admin account does not exist.'
admin_home=$(getent passwd "$admin_user" | cut -d: -f6)
admin_group=$(id -gn "$admin_user")
[[ -d $admin_home ]] || die 'Admin home directory does not exist.'
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl python3 git jq bash-completion \
  openssh-client sshpass dnsutils iputils-ping traceroute mtr-tiny tcpdump \
  iproute2 ethtool conntrack netcat-openbsd lsof htop iotop-c sysstat
[[ ! -f /etc/elektrokube/cluster.json ]] || config=/etc/elektrokube/cluster.json
load_config "$config"
install_helm; install_cilium_cli
[[ -x /usr/local/bin/k3s && -s /etc/rancher/k3s/k3s.yaml ]] || die 'Bootstrap the first node before installing admin access.'
if [[ -e /usr/local/bin/kubectl && ! -L /usr/local/bin/kubectl ]]; then
  log 'Preserving an existing kubectl binary; check its version matches the API.'
else ln -sfn /usr/local/bin/k3s /usr/local/bin/kubectl; fi
install -d -m 0755 /etc/bash_completion.d
/usr/local/bin/k3s kubectl completion bash > /etc/bash_completion.d/kubectl
helm completion bash > /etc/bash_completion.d/helm
cilium completion bash > /etc/bash_completion.d/cilium
install -d -m 0700 -o "$admin_user" -g "$admin_group" "$admin_home/.kube"
# Stage as root; never follow a pre-existing user-controlled kubeconfig symlink.
[[ ! -L $admin_home/.kube/elektrokube.yaml ]] || die 'Refusing a symlink at the managed kubeconfig path.'
install -m 0600 -o "$admin_user" -g "$admin_group" /etc/rancher/k3s/k3s.yaml "$admin_home/.kube/elektrokube.yaml"
if [[ ! -e $admin_home/.kube/config && ! -L $admin_home/.kube/config ]]; then
  runuser -u "$admin_user" -- ln -s elektrokube.yaml "$admin_home/.kube/config"
else
  log "Existing kubeconfig preserved. Use KUBECONFIG=$admin_home/.kube/elektrokube.yaml or import it into Headlamp."
fi
if [[ $headlamp == true ]]; then
  apt-get install -y --no-install-recommends flatpak xdg-desktop-portal-gnome
  flatpak remote-add --system --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
  flatpak install --system --noninteractive -y flathub io.kinvolk.Headlamp
  # Only kubeconfig access is needed; credentials use certificates, no host exec plugin.
  if [[ $admin_user != root ]]; then
    flatpak override --system --filesystem="$admin_home/.kube:ro" io.kinvolk.Headlamp
  fi
fi
log "Admin access installed for $admin_user; kubectl get nodes -o wide tests access."
if [[ $admin_user == root ]]; then
  log 'Root CLI administration is ready. For Headlamp, later run prepare-admin.sh --user YOUR_DESKTOP_USER and launch it from that GNOME account.'
elif [[ $headlamp == true ]]; then
  log 'Launch Headlamp from your GNOME session.'
fi
log 'The kubeconfig grants full cluster-admin access. Rerun prepare-admin.sh after k3s rotates its client certificate.'
