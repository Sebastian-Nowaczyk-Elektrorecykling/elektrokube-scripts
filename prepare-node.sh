#!/usr/bin/env bash
set -Eeuo pipefail
# shellcheck source=lib/common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib/common.sh"
usage() { echo 'Usage: prepare-node.sh [--gpu auto|none|nvidia|amd|intel] [--enable-iommu] [--config FILE]'; }
gpu=auto; iommu=false; config="$REPO_ROOT/config/cluster.json"
while (($#)); do
  case "$1" in
    --gpu) gpu=${2:?}; shift 2 ;;
    --enable-iommu) iommu=true; shift ;;
    --config) config=${2:?}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "Unknown option: $1" ;;
  esac
done
case "$gpu" in auto|none|nvidia|amd|intel) ;; *) die 'Invalid GPU selection.' ;; esac
root_only; debian_only; lock_host
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gnupg python3 jq sudo \
  openssh-client iproute2 iptables nftables conntrack socat ethtool pciutils kmod \
  util-linux apparmor apparmor-utils chrony open-iscsi nfs-common cryptsetup \
  dmsetup xfsprogs e2fsprogs libvirt-clients dconf-cli
load_config "$config"
install -d -m 0700 /etc/elektrokube

log 'Preparing networking, swap, Longhorn V1, and virtualization prerequisites.'
cat > /etc/modules-load.d/elektrokube.conf <<'EOF'
overlay
br_netfilter
vxlan
xt_TPROXY
xt_socket
xt_mark
xt_CT
iscsi_tcp
dm_crypt
nfs
tun
vhost_net
EOF
while read -r module; do modprobe "$module"; done < /etc/modules-load.d/elektrokube.conf
cat > /etc/sysctl.d/90-elektrokube.conf <<'EOF'
net.ipv4.ip_forward = 1
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.conf.all.rp_filter = 0
net.ipv4.conf.default.rp_filter = 0
fs.inotify.max_user_instances = 1024
fs.inotify.max_user_watches = 1048576
EOF
sysctl --load=/etc/sysctl.d/90-elektrokube.conf
if command -v nmcli >/dev/null; then
  install -d /etc/NetworkManager/conf.d
  cat > /etc/NetworkManager/conf.d/90-elektrokube.conf <<'EOF'
[keyfile]
unmanaged-devices=interface-name:cilium*;interface-name:lxc*
EOF
  nmcli general reload
fi
[[ -e /etc/fstab.pre-elektrokube ]] || cp -a /etc/fstab /etc/fstab.pre-elektrokube
sed -i -E '/^[[:space:]]*#/! { /[[:space:]]swap[[:space:]]/s/^/# elektrokube disabled swap: /; }' /etc/fstab
swapoff -a
# Also cover swap units from zram generators or custom systemd units on reboot.
while read -r unit _; do
  [[ $unit == *.swap ]] && systemctl mask "$unit"
done < <(systemctl list-units --all --type=swap --plain --no-legend)
systemctl mask swap.target
cat > /etc/systemd/system/elektrokube-no-swap.service <<'EOF'
[Unit]
Description=Disable remaining generated swap before k3s
After=swap.target
Before=k3s.service k3s-agent.service
[Service]
Type=oneshot
ExecStart=/sbin/swapoff -a
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now elektrokube-no-swap.service chrony.service iscsid.service
iscsi_version=$(dpkg-query -W -f='${Version}' open-iscsi)
[[ $iscsi_version != *2.1.12* ]] || die 'open-iscsi 2.1.12 is incompatible with Longhorn. Install >=2.1.13 or <=2.1.11.'
install -d -m 0755 /var/lib/longhorn
case $(findmnt -n -o FSTYPE -T /var/lib/longhorn) in
  ext4|xfs) ;; *) log 'Longhorn needs an ext4/XFS data mount; configure it before deploying storage.' ;;
esac
if systemctl is-active --quiet multipathd; then
  log 'multipathd is active: exclude Longhorn devices before deploying Longhorn (see README).'
fi

if modprobe kvm; then
  printf '%s\n' kvm >> /etc/modules-load.d/elektrokube.conf
  for cpu_module in kvm_intel kvm_amd; do
    if { [[ $cpu_module == kvm_intel ]] && grep -q GenuineIntel /proc/cpuinfo; } ||
       { [[ $cpu_module == kvm_amd ]] && grep -q AuthenticAMD /proc/cpuinfo; }; then
      if modprobe "$cpu_module"; then printf '%s\n' "$cpu_module" >> /etc/modules-load.d/elektrokube.conf; fi
    fi
  done
fi
virt-host-validate qemu > /etc/elektrokube/virtualization-report.txt 2>&1 || true
[[ -c /dev/kvm ]] || log 'No /dev/kvm: enable hardware virtualization/nested virtualization in firmware or hypervisor.'
if [[ $iommu == true ]]; then
  if [[ ! -d /etc/default/grub.d ]] || ! command -v update-grub >/dev/null; then
    die '--enable-iommu requires GRUB; configure other bootloaders manually.'
  fi
  iommu_args=iommu=pt
  if grep -q GenuineIntel /proc/cpuinfo; then iommu_args='intel_iommu=on iommu=pt';
  elif grep -q AuthenticAMD /proc/cpuinfo; then iommu_args='amd_iommu=on iommu=pt';
  else die 'Automatic IOMMU boot setup supports Intel/AMD only.'; fi
  # shellcheck disable=SC2016 # The variable expands when GRUB reads this file.
  printf 'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT %s"\n' "$iommu_args" > /etc/default/grub.d/90-elektrokube-iommu.cfg
  update-grub
  touch /etc/elektrokube/reboot-required
fi

log 'Disabling sleep, hibernation and lid actions; a short power-button press will reboot.'
install -d /etc/systemd/logind.conf.d /etc/systemd/sleep.conf.d /etc/polkit-1/rules.d
cat > /etc/systemd/logind.conf.d/99-elektrokube.conf <<'EOF'
[Login]
HandlePowerKey=reboot
HandlePowerKeyLongPress=reboot
PowerKeyIgnoreInhibited=yes
HandleSuspendKey=ignore
HandleSuspendKeyLongPress=ignore
HandleHibernateKey=ignore
HandleHibernateKeyLongPress=ignore
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
LidSwitchIgnoreInhibited=yes
IdleAction=ignore
EOF
cat > /etc/systemd/sleep.conf.d/99-elektrokube.conf <<'EOF'
[Sleep]
AllowSuspend=no
AllowHibernation=no
AllowHybridSleep=no
AllowSuspendThenHibernate=no
EOF
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target \
  systemd-suspend.service systemd-hibernate.service systemd-hybrid-sleep.service systemd-suspend-then-hibernate.service
cat > /etc/polkit-1/rules.d/00-elektrokube-power.rules <<'EOF'
// Let logind handle hardware keys. Deny desktop sleep and low-level inhibitors.
polkit.addRule(function(action, subject) {
    if (subject.user === "root") return;
    if (/^org\.freedesktop\.login1\.(suspend|hibernate|hybrid-sleep|suspend-then-hibernate|sleep)(-.*)?$/.test(action.id) ||
        /^org\.freedesktop\.login1\.inhibit-handle-(power-key|suspend-key|hibernate-key|lid-switch|reboot-key)$/.test(action.id)) {
        return polkit.Result.NO;
    }
});
EOF
install -d /etc/dconf/profile /etc/dconf/db/local.d/locks
if [[ ! -e /etc/dconf/profile/user ]]; then printf 'user-db:user\nsystem-db:local\n' > /etc/dconf/profile/user;
elif ! grep -qxF system-db:local /etc/dconf/profile/user; then printf '\nsystem-db:local\n' >> /etc/dconf/profile/user; fi
cat > /etc/dconf/db/local.d/90-elektrokube <<'EOF'
[org/gnome/settings-daemon/plugins/power]
sleep-inactive-ac-type='nothing'
sleep-inactive-battery-type='nothing'
sleep-inactive-ac-timeout=0
sleep-inactive-battery-timeout=0
power-button-action='nothing'
EOF
cat > /etc/dconf/db/local.d/locks/elektrokube <<'EOF'
/org/gnome/settings-daemon/plugins/power/sleep-inactive-ac-type
/org/gnome/settings-daemon/plugins/power/sleep-inactive-battery-type
/org/gnome/settings-daemon/plugins/power/sleep-inactive-ac-timeout
/org/gnome/settings-daemon/plugins/power/sleep-inactive-battery-timeout
/org/gnome/settings-daemon/plugins/power/power-button-action
EOF
dconf update
systemctl reload systemd-logind.service
inhibitors=$(systemd-inhibit --list --no-pager --no-legend)
if [[ $inhibitors == *handle-power-key* || $inhibitors == *handle-lid-switch* ]]; then
  touch /etc/elektrokube/reboot-required
  log 'An existing desktop session holds a key/lid inhibitor. Log out all GUI sessions or reboot to activate the power-button policy.'
fi

# Match PCI display-class devices, not CPU/chipset vendor names; handle mixed GPUs.
gpu_pci=$(lspci -Dn | awk '$2 ~ /^03[0-9a-f][0-9a-f]:$/ { print $3 }')
vendors=()
if [[ $gpu == auto ]]; then
  [[ $gpu_pci != *10de:* ]] || vendors+=(nvidia)
  [[ $gpu_pci != *1002:* ]] || vendors+=(amd)
  [[ $gpu_pci != *8086:* ]] || vendors+=(intel)
elif [[ $gpu != none ]]; then vendors+=("$gpu"); fi
if ((${#vendors[@]})); then
  cat > /etc/apt/sources.list.d/elektrokube-gpu.sources <<'EOF'
Types: deb
URIs: https://deb.debian.org/debian
Suites: trixie trixie-updates
Components: contrib non-free non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: https://security.debian.org/debian-security
Suites: trixie-security
Components: contrib non-free non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
  apt-get update
fi
for vendor in "${vendors[@]}"; do
  case "$vendor" in
    nvidia)
      [[ $(arch) == amd64 ]] || die 'Provision NVIDIA ARM drivers for your hardware manually, then use --gpu none.'
      apt-get install -y --no-install-recommends "linux-headers-$(uname -r)" nvidia-driver nvidia-smi libcuda1 firmware-misc-nonfree
      tmp=$(mktemp -d); trap 'rm -rf -- "$tmp"' EXIT
      download https://nvidia.github.io/libnvidia-container/gpgkey "$tmp/key"
      gpg --batch --yes --dearmor -o /usr/share/keyrings/nvidia-container-toolkit.gpg "$tmp/key"
      download https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list "$tmp/repo"
      sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit.gpg] https://#g' \
        "$tmp/repo" > /etc/apt/sources.list.d/nvidia-container-toolkit.list
      apt-get update
      apt-get install -y --no-install-recommends "nvidia-container-toolkit=$NVIDIA_TOOLKIT_VERSION" \
        "nvidia-container-toolkit-base=$NVIDIA_TOOLKIT_VERSION" "libnvidia-container-tools=$NVIDIA_TOOLKIT_VERSION" \
        "libnvidia-container1=$NVIDIA_TOOLKIT_VERSION"
      # K3s detects this runtime. Do not install a separate containerd or edit its generated TOML.
      if ! nvidia-smi; then
        touch /etc/elektrokube/reboot-required
        log 'NVIDIA needs attention/reboot. Check DKMS, Secure Boot/MOK enrollment and hardware support before scheduling GPU workloads.'
      fi
      ;;
    amd)
      apt-get install -y --no-install-recommends firmware-amd-graphics
      modprobe amdgpu || true
      [[ -e /dev/kfd ]] || { touch /etc/elektrokube/reboot-required; log 'No /dev/kfd: check ROCm hardware support after reboot.'; }
      ;;
    intel)
      apt-get install -y --no-install-recommends firmware-intel-graphics
      [[ -d /dev/dri ]] || { touch /etc/elektrokube/reboot-required; log 'No /dev/dri: check Intel graphics firmware/kernel after reboot.'; }
      ;;
  esac
done
log 'Host prerequisites installed. Virtualization diagnostics: /etc/elektrokube/virtualization-report.txt'
[[ ! -e /etc/elektrokube/reboot-required ]] || log 'Reboot/logout or hardware checks are still required; see README. This script never reboots automatically.'
