# elektrokube-scripts

Install **k3s + Cilium on Debian 13** and administer the cluster from its first
node. Download this repository only onto that first node. `add-node.sh` copies
the required scripts to new nodes and provisions them through SSH.

**First node** means the machine on which you clone this repository. It is the
administration workstation and the initial k3s server. It can be a dedicated
controller or a worker/controller hybrid. **New node** means a freshly installed
Debian 13 machine joining as a worker, controller, or hybrid.

This repository installs host prerequisites, k3s, Cilium, Hubble Relay/UI, and
local administration tools. Flux installation, Flux reconciliation, application
manifests, Longhorn, KubeVirt, GPU operators/device plugins, ingress/Gateway API,
LAN DNS, and LoadBalancer address pools belong to later work in other repositories.
There are no Flux manifests or GitHub-token prompts here.

## Scripts and roles

| Script | Purpose |
| --- | --- |
| `install.sh` | Prepare the first node, bootstrap a **hybrid** cluster node, and install tools for its GNOME user |
| `prepare-node.sh` | Host preparation: networking, swap, storage/virtualization/GPU prerequisites, power policy |
| `bootstrap-cluster.sh` | Bootstrap k3s and Cilium on a prepared first node; `hybrid` or `controller` |
| `add-node.sh` | Ask for SSH credentials, establish restricted key access, prepare a new node, and join it automatically |
| `prepare-admin.sh` | Install kubectl access, Helm, Cilium CLI, Headlamp Desktop, completions, and diagnostics |

| Role | k3s process | Embedded etcd | Ordinary application workloads |
| --- | --- | --- | --- |
| `worker` | agent | No | Yes |
| `controller` | server | Yes | Blocked by `CriticalAddonsOnly=true:NoSchedule` unless a workload explicitly tolerates it |
| `hybrid` | server | Yes | Yes |

A dedicated controller retains kubelet, containerd and Cilium for essential
cluster services; it is not an experimental agentless server. CoreDNS, metrics
server, Cilium and Hubble tolerate the controller taint. A cluster with only
dedicated controllers needs a worker/hybrid for ordinary applications.

## Before installing

- Use fresh, updated **Debian 13** on amd64 or arm64, booted with systemd. Install
  GNOME on the first node. For useful workloads, start with at least 4 CPU cores,
  8 GiB RAM and an SSD on the first node; allocate more for VMs, storage and GPUs.
- Give every node a unique lowercase hostname and a **static IPv4 or DHCP
  reservation**. The first node's address is detected automatically (or selected
  with `--node-ip` / `--interface`); the scripts do not configure
  interfaces, DHCP, DNS, routing or the router. Address changes require planned
  cluster and SSH reconfiguration. This implementation is IPv4-only.
- All node LAN addresses must be mutually reachable. Use direct LAN SSH without
  NAT, jump hosts or proxies. The first node's SSH source address is pinned on
  every new node. Keep that address stable, including on a multi-interface host.
- Enable time synchronization and Internet access to Debian mirrors, GitHub
  release assets, Helm/Cilium registries, container registries, Flathub, and the
  NVIDIA repository when relevant. The scripts install chrony.
- Review `config/cluster.json`. Pod `10.42.0.0/16` and service `10.43.0.0/16`
  networks must not overlap your LAN, VPNs, routes, or each other. `api_address`
  defaults to the first node's detected/selected address. Bootstrap requires them to match.
- On each **new node**, install/start `openssh-server`. You can enroll as **root
  without sudo**, or use a normal account with unrestricted sudo access. The
  initial SSH login must work first; a password working at the console is not
  sufficient. Debian disables root SSH password login by default; follow the
  root-console steps below. Passwordless sudo is supported for non-root accounts.
- Keep console access for initial provisioning, firmware/MOK enrollment, and
  recovery. These scripts change power settings, disable swap, and replace the
  SSH policy on new nodes. They never format disks or reboot automatically.

### Root enrollment without sudo

For a freshly installed node with a working local root password, log in at the
**new node's console as root**. These commands do not use or require sudo.
Replace `192.168.2.153` with the **first node's actual LAN source IPv4**:

```bash
apt-get update
apt-get install -y openssh-server
systemctl enable --now ssh

FIRST_NODE_IP=192.168.2.153
install -d -m 0755 /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/00-elektrokube-enrollment.conf <<EOF
# elektrokube: temporary root enrollment
PermitRootLogin yes
PasswordAuthentication yes
PubkeyAuthentication yes
AuthenticationMethods any
AllowUsers root@$FIRST_NODE_IP
EOF
/usr/sbin/sshd -t && systemctl reload ssh

# Check the effective policy for a connection from the first node:
/usr/sbin/sshd -T -C "user=root,host=first-node,addr=$FIRST_NODE_IP" \
  | grep -E '^(permitrootlogin|passwordauthentication|pubkeyauthentication|authenticationmethods|allowusers) '
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

The effective values must include `permitrootlogin yes`,
`passwordauthentication yes`, `pubkeyauthentication yes`,
`authenticationmethods any`, and `allowusers root@YOUR_FIRST_NODE_IP`. Debian's
stock configuration includes `.conf` files from this directory before its main
settings. If the output differs, inspect earlier drop-ins, Match/Allow/Deny rules,
or a previously managed configuration; do not assume the new file took effect.
This temporary policy limits successful SSH logins to root from the first node.
It also prevents other accounts from logging in during initial enrollment.

On a multi-interface first node, `ip -4 route get NEW_NODE_IP` shows the `src`
address to use above. Use the host fingerprint printed at the new node's console
when enrollment asks you to verify it. Then, **on the first node**, run:

```bash
git pull
sudo ./add-node.sh --host 192.168.2.154 --user root --role worker
# If already root on the first node, omit sudo.
```

Successful enrollment switches SSH to source-restricted key-only login and
removes the temporary file with the exact marker shown above. Password access
is needed only for that initial connection. If you abandon enrollment, remove
the temporary file from the new node's console and reload SSH:

```bash
rm /etc/ssh/sshd_config.d/00-elektrokube-enrollment.conf
/usr/sbin/sshd -t && systemctl reload ssh
```

If you have already enrolled the node, password rejection is expected: use its
saved first-node key and the original source IP. Do not reopen password login
on a successfully enrolled node just to test it. Root's local console password
is not changed by these scripts.

For **non-root enrollment**, the new node additionally needs `sudo` installed
and the selected user must have sudo privileges. `add-node.sh` never invokes
sudo remotely when `--user root` is selected.

### Connection troubleshooting

| Error | What to check on the new node's root console |
| --- | --- |
| Connection refused / no host key returned | Correct node IP and SSH port; `systemctl status ssh`; install/start `openssh-server` |
| Timeout / no route | Network connection, chosen source IP, routing and firewall; this is before password authentication |
| Permission denied | `sshd -T -C ...` for the actual user/source; root password policy, account restrictions and password |
| Changed host key | Compare the fingerprint at the console before updating the first node's saved trust entry |

For the actual reason behind an authentication rejection, run
`journalctl -u ssh -n 50 --no-pager` on the new node immediately after the attempt.
The enrollment script distinguishes a transport failure from a rejected login
and explains the root-password policy. A failed first key probe is normal on a
fresh node and is handled before the password prompt.

## First node: hybrid + GNOME administration workstation

On the first node, as your normal desktop user:

```bash
sudo apt-get update
sudo apt-get install -y git ca-certificates python3
git clone https://github.com/Sebastian-Nowaczyk-Elektrorecykling/elektrokube-scripts.git
cd elektrokube-scripts

# Edit config/cluster.json if your networks require different CIDRs.
sudo ./install.sh
```

No address or username argument is normally needed. The script prints the
selected address and admin account before preparing the host:

- **Address:** an explicit `--node-ip` takes precedence, followed by the saved
  cluster address on reruns, then `api_address` in the configuration. Otherwise
  it selects an address on the active default-route interface, preferring the
  lowest route metric and an explicitly recorded source address. Without a
  usable default route, a single suitable local IPv4 is sufficient. Loopback,
  link-local, pod/service networks, and commonly named VPN/container interfaces
  are excluded from automatic selection. An ambiguous result requires
  `--interface IFACE` or `--node-ip IP`; both can be supplied to validate the
  address against a particular interface. No Internet probe is required.
- **Account:** `--admin-user USER` overrides the default. Otherwise `SUDO_USER`
  selects the account that invoked sudo; direct root execution uses root. Root
  gets its own kubeconfig and CLI tools. Headlamp is installed system-wide, but
  should be launched from a normal GNOME account; give that account access later
  with `./prepare-admin.sh --user YOUR_GNOME_USER` from the root shell.

For example, `sudo ./install.sh --interface enp3s0` chooses a specific LAN
interface, and `sudo ./install.sh --node-ip 192.168.2.153 --admin-user root`
explicitly selects both settings. If already logged in directly as root, simply
run `./install.sh`. Detection does not make a DHCP lease permanent: reserve the
selected address before relying on it for the cluster.

Other options are `--node-name NAME`,
`--gpu auto|none|nvidia|amd|intel`, `--enable-iommu`, `--skip-headlamp`, and
`--config FILE`. Run any public script with `--help` for its arguments.

GPU discovery defaults to `auto` and handles multiple vendors. Use `--gpu none`
if you will manage drivers yourself or your GPU needs a different driver branch.
Host preparation can report a required reboot or firmware action while cluster
installation continues. A Ready Kubernetes node does not prove GPU/VM readiness.

For a **dedicated-controller first node**, run the three steps explicitly:

```bash
sudo ./prepare-node.sh --gpu none
sudo ./bootstrap-cluster.sh --role controller
sudo ./prepare-admin.sh
```

The first server initializes embedded etcd immediately so additional controllers
can join later. All servers disable Flannel, k3s network policy, kube-proxy,
Traefik, ServiceLB and local-path storage. Cilium handles the CNI, network policy,
service forwarding and eBPF masquerading, with VXLAN tunneling and Kubernetes
PodCIDR allocation. CoreDNS and metrics-server remain enabled. There is no
default storage class until you install a storage provisioner.

## Add a new node from the first node

```bash
# Worker (the default role)
sudo ./add-node.sh --host 192.168.2.154 --user debian --role worker

# Dedicated controller
sudo ./add-node.sh --host 192.168.2.155 --user debian --role controller

# Worker/controller hybrid
sudo ./add-node.sh --host 192.168.2.156 --user debian --role hybrid
```

Omit `--user` to be asked for it. `--port` supports a nondefault SSH port.
`--node-name` overrides the new node's short hostname. GPU and IOMMU options
apply to that new node, independently of the first node's settings. For example:

```bash
sudo ./add-node.sh --host 192.168.2.154 --user debian \
  --role worker --node-name gpu-worker --gpu nvidia --enable-iommu
```

To avoid the host-key confirmation prompt, supply a fingerprint **already
verified at the new node's console** using `--host-key-fingerprint 'SHA256:…'`.
The script never silently trusts a changed host key. SSH/sudo passwords are
entered with hidden prompts and kept in process memory, not command arguments,
environment variables, inventory files or Git. Press Enter at the sudo prompt
to reuse the login password, or when sudo is already passwordless.

Enrollment performs these steps:

1. Verify/pin the new node's Ed25519 SSH host key and check direct LAN addressing.
2. Generate a per-node Ed25519 key **on the first node**. Only the public key is
   sent to the new node; the private key never leaves the first node.
3. Install the public key and verify a separate key-only connection before
   disabling any existing authentication method.
4. Copy scripts plus the appropriate join token through SSH. A worker receives
   the agent token; a server receives the server token. By default k3s may use
   the same underlying token for both; these scripts do not rotate cluster tokens.
5. Back up and replace `/etc/ssh/sshd_config`. Permit only the supplied account,
   using only the managed key, from the first node's observed IPv4 address.
   Disable password/keyboard-interactive login, other keys/certificate sources,
   forwarding, and IPv6 SSH. Other accounts cannot authenticate, including root
   unless root was the selected account. This is SSH authentication enforcement;
   the scripts do not rewrite your firewall.
6. Arm a **120-second automatic rollback**, reload SSH, and verify a new TCP
   connection. Cancel rollback only after key authentication succeeds.
7. Prepare the host and install the same pinned k3s release with its selected
   role. Wait for Kubernetes Ready and the Cilium DaemonSet rollout.

The password used for sudo is discarded when provisioning ends. No permanent
passwordless-sudo rule is added. Node software lives under
`/opt/elektrokube-scripts` on new nodes. Enrollment is serialized on the first
node. If a step fails, fix its reported cause and rerun the **same command**;
existing keys and matching node configuration are reused. Once SSH is hardened,
subsequent runs need only the key and, for a normal account, its sudo password.

### Keys, reconnecting and recovery

The first node keeps keys in `/root/.ssh/elektrokube/`, mode 0600, and node records
in `/etc/elektrokube/nodes/`. Keys have no passphrase for automated provisioning;
protect and back up the first node accordingly. The script prints an exact SSH
command after enrollment. A typical reconnect command is:

```bash
sudo ssh -i /root/.ssh/elektrokube/192.168.2.154-22.ed25519 \
  -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=/root/.ssh/elektrokube/192.168.2.154-22.known_hosts \
  debian@192.168.2.154
```

On a multi-interface first node, use the `-b SOURCE_IP` argument printed by the
script. Existing SSH sessions are not forcibly terminated, but all **new**
connections use the restricted policy after a successful enrollment.

If verification fails after changing SSH, wait two minutes for rollback and
retry. Password access may return while the previous policy is restored; a
failed enrollment is not reported as hardened. If you lose the first node/key,
change its IP, reinstall a target, or reboot during the rollback window, use the
new node's console. To restore its original SSH configuration deliberately:

```bash
sudo cp /etc/elektrokube/ssh/sshd_config.original /etc/ssh/sshd_config
sudo /usr/sbin/sshd -t
sudo systemctl reload ssh
```

Do not erase a changed `known_hosts` entry without checking the new fingerprint
at the console. Reusing a name from a removed/reinstalled node also requires
deliberate Kubernetes node and k3s node-password cleanup; enrollment refuses
unmanaged installations and conflicting identities.

## Power, storage, virtualization and GPUs

### Power policy

All nodes get logind and systemd sleep policies, masked sleep/hibernate targets
and services, a polkit policy, and locked GNOME power preferences. Idle, lid
close (AC, battery or dock), suspend and hibernate keys do not sleep the machine.
A **short physical power-button press reboots** via logind. Desktop power
handling is disabled so it cannot replace that action.

An already-running GNOME session may hold a low-level key/lid inhibitor that
cannot be revoked by reloading policy. Log out all graphical sessions or reboot
after preparation, then verify the power button and lid from console during
maintenance. Firmware-enforced long-press power cuts, battery exhaustion and
thermal shutdown cannot be overridden by these scripts. A laptop used closed
must still have adequate ventilation.

### Longhorn

Preparation installs and activates iSCSI (`open-iscsi`, `iscsid`, `iscsi_tcp`),
NFS clients, cryptsetup, device mapper, and ext4/XFS utilities. It creates
`/var/lib/longhorn` without formatting or repartitioning anything. Before
deploying Longhorn V1, place data on a suitable ext4/XFS filesystem with enough
capacity, check mount propagation, and configure disk/node placement. An active
`multipathd` is reported; exclude Longhorn devices before deploying storage.
The known incompatible `open-iscsi` 2.1.12 version is rejected.

Longhorn V2/SPDK requires additional deliberate configuration: isolated raw
devices/IOMMU groups, hugepages and CPU reservations. This repository does not
bind disks to VFIO or reserve hugepages. Run the selected Longhorn release's
preflight checks before installation.

### KubeVirt / other virtualization

Preparation loads KVM, the CPU-specific KVM module when available, `tun` and
`vhost_net`, and saves `virt-host-validate qemu` output to
`/etc/elektrokube/virtualization-report.txt`. It does not run a competing libvirt
daemon or install a VM platform. Enable VT-x/AMD-V (and nested virtualization
when applicable) in firmware/the outer hypervisor; `/dev/kvm` must exist.

`--enable-iommu` adds Intel/AMD IOMMU arguments to a dedicated GRUB drop-in and
requires a reboot plus firmware VT-d/AMD-Vi support. It never binds a display
GPU or disk away from the host. Check device isolation before PCI passthrough.
Choose and validate a KubeVirt version against Debian 13's kernel, including
live migration if needed; host prerequisites alone do not establish that
compatibility. Cilium supports the primary pod network; secondary VM networks
may later need Multus and additional CNI configuration.

### GPUs

`--gpu auto` detects PCI display devices, including mixed vendors:

| Vendor | Host preparation | Remaining deployment work |
| --- | --- | --- |
| NVIDIA, amd64 | Running-kernel headers, Debian `nvidia-driver`, `nvidia-smi`, `libcuda1`, NVIDIA Container Toolkit | Check hardware/driver branch, DKMS and Secure Boot MOK; reboot if needed; deploy a compatible device plugin/operator |
| AMD | Debian AMD graphics firmware and `amdgpu` | Check `/dev/kfd` and supported ROCm hardware; provide workload userspace libraries and a device plugin |
| Intel | Debian Intel graphics firmware | Check `/dev/dri` and kernel driver support; provide workload userspace libraries and a device plugin |

GPU installation adds Debian's signed contrib/non-free/non-free-firmware sources;
NVIDIA also adds its signed container-toolkit APT repository. Legacy/new NVIDIA
boards may require a different driver branch; `--gpu none` lets you provision
that manually. Automatic NVIDIA driver installation on arm64 is intentionally
rejected because platform requirements differ.

K3s detects NVIDIA's runtime when it starts. If you add the toolkit to an
already-running node, restart its k3s/k3s-agent service during maintenance and
check `/var/lib/rancher/k3s/agent/etc/containerd/config.toml` for `nvidia`.
Use `runtimeClassName: nvidia` for NVIDIA workloads unless you explicitly choose
another runtime configuration. Do not edit k3s's generated containerd TOML or
install a second containerd. Prevent a future GPU operator from competing with
host-managed drivers/toolkit. GPU resources appear in Kubernetes only after
deploying the appropriate plugin/operator.

## Administration and diagnostics

`prepare-admin.sh` installs kubectl (the version bundled with k3s), pinned Helm
and Cilium CLI, and tools including `jq`, `git`, `sshpass`, `dig`, `ping`, `mtr`,
`traceroute`, `tcpdump`, `conntrack`, `ss`, `ethtool`, `lsof`, `htop`, `iotop-c`
and `sysstat`. Headlamp Desktop is installed from Flathub; launch it from GNOME
or with `flatpak run io.kinvolk.Headlamp`, **as your desktop user**.

The selected admin account (including root when chosen) gets a mode-0600
cluster-admin kubeconfig at `~/.kube/elektrokube.yaml`.
If no `~/.kube/config` exists, it points there. An existing config is preserved:
use `export KUBECONFIG=~/.kube/elektrokube.yaml` or import that file into Headlamp.
Kubeconfig access grants full control of the cluster. Keep it private and rerun
`prepare-admin.sh` after k3s rotates its embedded client certificate. The desktop
application gets read access to the selected non-root user's `.kube` directory;
root's kubeconfig is not exposed to desktop applications. `prepare-admin.sh`
uses the same sudo-user/current-user default, with an optional `--user` override.

```bash
kubectl get nodes -o wide
kubectl get pods -A
kubectl top nodes
cilium status --wait
kubectl -n kube-system exec ds/cilium -- cilium-dbg status --verbose

# Local-only Hubble UI; stop with Ctrl-C. Open http://127.0.0.1:12000.
kubectl -n kube-system port-forward --address 127.0.0.1 svc/hubble-ui 12000:80

# On a server / worker respectively:
sudo journalctl -u k3s -b --no-pager
sudo journalctl -u k3s-agent -b --no-pager
```

## Network and availability

Allow the following on your LAN/firewalls as appropriate. The scripts do not
flush firewall rules or open the cluster to the Internet.

| Traffic | Protocol/port | Scope |
| --- | --- | --- |
| Enrollment/administration | TCP 22, or chosen SSH port | First node → new nodes |
| Kubernetes API / k3s supervisor | TCP 6443 | Nodes/admin → servers |
| Embedded etcd | TCP 2379–2380 | Servers ↔ servers only |
| Kubelet | TCP 10250 | Cluster nodes, especially servers → nodes |
| Cilium VXLAN | UDP 8472 | All cluster nodes ↔ all cluster nodes |
| Cilium health | TCP 4240 and ICMP | All cluster nodes ↔ all cluster nodes |
| Hubble Relay | TCP 4244 | Relay/node traffic within the cluster |
| NodePort services, when created | TCP/UDP 30000–32767 | Only intended clients |

Use **three or five** server nodes for etcd failure tolerance. Two etcd members
still require both members for quorum. Add the third promptly when expanding
from one server. An operator replica count of one supports initial bootstrap;
raise it deliberately when implementing HA.

Adding servers does **not** make the first-node API endpoint highly available:
joins and Cilium point at that first IP. The first node remains the admin/key
holder and API entry point until you implement a stable HA VIP/load balancer
and deliberately migrate the relevant SANs/configuration. Back up SSH keys,
etcd snapshots, and `/var/lib/rancher/k3s/server/token` securely; the server token
is needed with snapshots for restoration. Keep backups off the cluster.

## Repeat runs, versions and verification

Settings used to bootstrap are recorded at `/etc/elektrokube/cluster.json`.
Every node records its role, first/join state, name, IP and k3s version in
`/etc/elektrokube/node-identity`. Matching reruns resume an interrupted install.
Different roles, addresses, versions, managed k3s configuration, or existing
unmanaged k3s data cause a refusal; these are provisioning scripts, not a rolling
upgrade or node-conversion tool. Do not rerun bootstrap after handing Cilium to
Flux without first planning ownership and value changes.

Defaults verified against upstream release/documentation references:

| Component | Pin / source |
| --- | --- |
| k3s | `v1.36.4+k3s1` |
| Cilium | `1.20.2` (its documented Kubernetes range includes 1.36) |
| Cilium CLI | `v0.20.1` |
| Helm | `v3.19.0` |
| NVIDIA Container Toolkit | `1.20.0-1` |
| Host packages / Headlamp | Debian APT / signed Flathub updates |

The k3s installer is downloaded from the pinned tag and checked against the
SHA256 in `config/cluster.json`; that installer verifies the k3s binary checksum.
Helm and Cilium CLI downloads are verified against upstream SHA256 files.
Those checks detect corruption and mismatched downloads; upstream distribution
endpoints remain part of the trust chain. Changing pins requires reviewing
compatibility and updating the installer checksum when its content changes.

Before scheduling important workloads, verify on the actual machines:

1. Reboot/log out as reported, verify swap stays off with `swapon --show`, time
   synchronization with `chronyc tracking`, and lid/power-button behavior.
2. Confirm every intended node is Ready, system pods are running, Cilium reports
   kube-proxy replacement enabled, and `kubectl top nodes` works.
3. Run `cilium connectivity test` once workers are available. It creates test
   resources; inspect failures and remove the test namespace afterward with
   `kubectl delete namespace cilium-test-1` if it was retained (check its actual
   namespace name in the test output).
4. On new nodes, confirm the printed key-only SSH command succeeds from the
   first node. Check `sudo sshd -T` at the console for the restrictive policy.
5. Check `systemctl is-active iscsid`, the virtualization report, `/dev/kvm`, GPU
   device files/`nvidia-smi`, and runtime detection. Install workload operators
   only after their own release-specific preflight checks pass.

Offline repository checks (no host provisioning) are:

```bash
# Requires python3 and shellcheck.
bash tests/check.sh
```

CI checks shell syntax, ShellCheck, configuration/role boundaries, credential
transport, SSH rollback/race handling, and OpenSSH parsing. These checks are
not a substitute for a physical Debian 13 installation and multi-node network,
GPU, storage, or VM acceptance tests.

## Upstream references

- [Debian 13 SSH server configuration and root-login defaults](https://manpages.debian.org/trixie/openssh-server/sshd_config.5.en.html)
- [Cilium on k3s](https://docs.cilium.io/en/stable/installation/k3s/)
- [Cilium kube-proxy replacement](https://docs.cilium.io/en/stable/network/kubernetes/kubeproxy-free/)
- [Cilium/Kubernetes compatibility](https://docs.cilium.io/en/stable/network/kubernetes/compatibility/)
- [k3s embedded etcd](https://docs.k3s.io/datastore/ha-embedded)
- [k3s containerd and NVIDIA runtime](https://docs.k3s.io/advanced)
- [Longhorn installation requirements](https://longhorn.io/docs/latest/deploy/install/)
- [KubeVirt prerequisites](https://kubevirt.io/user-guide/cluster_admin/installation/)
- [Headlamp Linux desktop installation](https://headlamp.dev/docs/latest/installation/desktop/linux-installation/)
- [NVIDIA Container Toolkit installation](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)

The design follows the host-preparation ideas in
[minimum-k8s-net-elektro](https://github.com/Sebastian-Nowaczyk-Elektrorecykling/minimum-k8s-net-elektro),
with separate SSH enrollment and a reboot-on-power-button policy. No Flux or
application configuration is copied from that repository.
