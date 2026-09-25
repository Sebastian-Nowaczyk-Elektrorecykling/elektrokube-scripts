#!/usr/bin/env python3
"""Enroll a fresh Debian 13 node over SSH, invoked on the first node as root."""
import argparse
import base64
import getpass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
import tempfile
import time

from config import load, ipv4, node_name, node_config

ROOT = Path(__file__).resolve().parent.parent
STATE = Path("/etc/elektrokube")
KEYS = Path("/root/.ssh/elektrokube")


def run(argv, **kwargs):
    result = subprocess.run(argv, **kwargs)
    if result.returncode:
        raise RuntimeError(f"{Path(argv[0]).name} failed (exit {result.returncode}); see the preceding error")
    return result


def key_line(source, public_key):
    ipv4(source)
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?", public_key.strip()):
        raise ValueError("Invalid Ed25519 public key")
    return f'from="{source}",no-agent-forwarding,no-port-forwarding,no-X11-forwarding {public_key.strip()}'


def fingerprint(key):
    blob = base64.b64decode(key.split()[2], validate=True)
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def ssh_error(result, user, source):
    detail = (result.stderr or b"").decode(errors="replace").strip()
    lower = detail.lower()
    if "permission denied" in lower or result.returncode == 5:
        hint = "SSH authentication was rejected; this does not prove the password is wrong."
        if user == "root":
            hint += (" Debian 13 normally disables root SSH password login even when the console password works."
                     " Root enrollment needs no sudo. On the NEW NODE's root console, follow README.md's"
                     " 'Root enrollment without sudo' steps to allow temporary access from " + source + "."
                     " If this node was already enrolled, use its saved key and the original first-node IP instead.")
        else:
            hint += " Check the account's password and the server's SSH authentication policy at its console."
    elif "connection refused" in lower:
        hint = "The SSH port refused the connection. Check the address/port and enable openssh-server on the new node."
    elif any(s in lower for s in ("timed out", "no route to host", "network is unreachable")):
        hint = "SSH could not reach the node. Check its LAN address, routing and firewall before changing passwords."
    elif "host key verification failed" in lower or "host identification has changed" in lower:
        hint = "The SSH host key differs from the saved key. Verify its fingerprint at the new node's console."
    else:
        hint = f"SSH connection failed (exit {result.returncode}). Inspect the SSH error and the new node's ssh journal."
    return hint + ("\n" + detail if detail else "")


def authenticate(conn):
    """Try the saved key first; request a password only after an authentication refusal."""
    command = 'printf "%s\\n" "$SSH_CONNECTION"'
    result = conn.call(command, capture_error=True, check=False)
    if result.returncode == 0:
        return True, result.stdout
    if b"permission denied" not in (result.stderr or b"").lower():
        raise RuntimeError(ssh_error(result, conn.a.user, conn.source))
    print("No usable enrolled key yet; trying the initial SSH password login.", flush=True)
    if conn.a.user == "root":
        print("Root SSH must allow the initial password login; sudo is not needed on the new node.", flush=True)
    conn.login_password = getpass.getpass("New node SSH password (not saved): ")
    result = conn.call(command, password=True, capture_error=True, check=False)
    if result.returncode:
        raise RuntimeError(ssh_error(result, conn.a.user, conn.source))
    return False, result.stdout


class Connection:
    def __init__(self, a, source, key, known):
        self.a, self.source, self.key = a, source, key
        self.login_password = None
        self.sudo_password = None
        self.base = ["ssh", "-4", "-p", str(a.port), "-b", source,
                     "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known}",
                     "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10",
                     "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                     "-o", "ControlMaster=no", "-o", "ControlPath=none",
                     "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none"]

    def call(self, command, *, password=False, root=False, data=None, capture=True, capture_error=False, check=True):
        argv = list(self.base)
        if password:
            argv += ["-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=password,keyboard-interactive"]
        else:
            argv += ["-i", str(self.key), "-o", "BatchMode=yes", "-o", "PreferredAuthentications=publickey"]
        if root and self.a.user != "root":
            # Only sudo consumes this stream; the actual command sees /dev/null.
            command = "sudo -S -p '' -- /bin/bash -c " + shlex.quote("exec " + command + " </dev/null")
            data = ((self.sudo_password or "") + "\n").encode()
        argv += [f"{self.a.user}@{self.a.host}", command]
        fds = ()
        if password:
            read_fd, write_fd = os.pipe()
            secret = (self.login_password + "\n").encode()
            if len(secret) > 4096:
                os.close(read_fd); os.close(write_fd)
                raise ValueError("Password exceeds supported length")
            os.write(write_fd, secret)
            os.close(write_fd)
            argv = ["sshpass", "-d", str(read_fd)] + argv
            fds = (read_fd,)
        try:
            result = subprocess.run(argv, input=data, stdout=subprocess.PIPE if capture else None,
                                    stderr=subprocess.PIPE if capture_error else None, pass_fds=fds)
        finally:
            for fd in fds:
                os.close(fd)
        if check and result.returncode:
            raise RuntimeError(f"Remote SSH step failed (exit {result.returncode})")
        return result


def pin_host(a, known):
    if known.exists() and known.stat().st_size:
        if a.host_key_fingerprint and a.host_key_fingerprint not in {
                fingerprint(line) for line in known.read_text().splitlines() if line and not line.startswith("#")}:
            raise ValueError("Supplied fingerprint differs from the previously pinned host key")
        return
    scanned = subprocess.run(["ssh-keyscan", "-4", "-T", "5", "-t", "ed25519", "-p", str(a.port), a.host],
                             capture_output=True, text=True)
    if scanned.returncode:
        raise RuntimeError(f"Cannot read an SSH host key from {a.host}:{a.port}. Check its IP/port and that"
                           " openssh-server is running (systemctl enable --now ssh at the new node's root console)."
                           " This check does not use a password.\n" + scanned.stderr.strip())
    scan = scanned.stdout.strip()
    lines = [x for x in scan.splitlines() if x and not x.startswith("#")]
    if len(lines) != 1 or lines[0].split()[1] != "ssh-ed25519":
        raise RuntimeError("Expected exactly one Ed25519 host key")
    found = fingerprint(lines[0])
    print(f"SSH host {a.host}:{a.port}: {found}", flush=True)
    if a.host_key_fingerprint:
        if found != a.host_key_fingerprint:
            raise ValueError("Host-key fingerprint mismatch")
    else:
        print("Compare with the NEW NODE console: ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub")
        if input("Fingerprint matches? Type yes: ").strip() != "yes":
            raise ValueError("Host key was not approved")
    known.write_text(lines[0] + "\n")
    known.chmod(0o600)


def make_bundle(path, descriptor, config, token):
    with tarfile.open(path, "w:gz") as archive:
        files = list(ROOT.glob("*.sh")) + list((ROOT / "lib").glob("*.sh")) + list((ROOT / "lib").glob("*.py"))
        for file in sorted(files):
            archive.add(file, arcname=str(file.relative_to(ROOT)), recursive=False)
        for name, content in {"node.json": json.dumps(descriptor).encode(),
                              "cluster.json": (json.dumps(config, indent=2) + "\n").encode(),
                              "join.token": token}.items():
            member = tarfile.TarInfo("payload/" + name)
            member.mode = 0o600
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))


def kube(*args, check=True):
    return subprocess.run(["/usr/local/bin/k3s", "kubectl", "--kubeconfig", "/etc/rancher/k3s/k3s.yaml", *args],
                          capture_output=True, text=True, check=check)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", required=True, type=ipv4, help="New node's reserved/static LAN IPv4 (also its k3s node IP)")
    p.add_argument("--user", help="Existing SSH account with root or unrestricted sudo access; prompts if omitted")
    p.add_argument("--port", type=int, default=22)
    p.add_argument("--role", choices=["worker", "controller", "hybrid"], default="worker")
    p.add_argument("--node-name", help="Defaults to the new node's short hostname")
    p.add_argument("--gpu", choices=["auto", "none", "nvidia", "amd", "intel"], default="auto")
    p.add_argument("--enable-iommu", action="store_true")
    p.add_argument("--host-key-fingerprint", help="Preverified SHA256 fingerprint; otherwise compare interactively")
    a = p.parse_args()
    if os.geteuid() != 0:
        p.error("Run add-node.sh with sudo on the first node")
    os.umask(0o077)
    c = load(STATE / "cluster.json")
    identity = (STATE / "node-identity").read_text().strip().split(":")
    if identity[1] != "true":
        p.error("Enrollment must run on the first node")
    kube("get", "--raw=/readyz")
    local_ips = {i["local"] for n in json.loads(run(["ip", "-j", "-4", "addr"], capture_output=True, text=True).stdout)
                 for i in n["addr_info"]}
    if a.host in local_ips:
        p.error("The target is an address on the first node")
    if not 1 <= a.port <= 65535:
        p.error("Invalid SSH port")
    source = ipv4(json.loads(run(["ip", "-j", "-4", "route", "get", a.host], capture_output=True, text=True).stdout)[0]["prefsrc"])
    a.user = a.user or input("New node SSH username: ").strip()
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", a.user):
        p.error("Use a normal Debian account name")
    KEYS.mkdir(parents=True, exist_ok=True, mode=0o700)
    KEYS.chmod(0o700)
    prefix = KEYS / f"{a.host}-{a.port}"
    key, known = Path(str(prefix) + ".ed25519"), Path(str(prefix) + ".known_hosts")
    pin_host(a, known)
    if not key.exists():
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "elektrokube-first-node", "-f", str(key)])
    key.chmod(0o600)
    public = Path(str(key) + ".pub").read_text().strip()
    conn = Connection(a, source, key, known)
    key_works, connection_info = authenticate(conn)
    if a.user != "root":
        if conn.login_password is not None:
            conn.sudo_password = getpass.getpass("sudo password [Enter uses SSH password; NOPASSWD also works]: ") or conn.login_password
        else:
            conn.sudo_password = getpass.getpass("New node sudo password [Enter for NOPASSWD]: ")
    observed = connection_info.decode().split()
    if len(observed) != 4 or observed[0] != source or observed[2] != a.host:
        raise ValueError("Use direct LAN SSH without NAT/proxies; the observed source/destination addresses differ")
    a.node_name = node_name(a.node_name or conn.call("hostname -s", password=not key_works).stdout.decode().strip())
    node_config(c, a.role, a.host, a.node_name)  # Validate IP/CIDR conflicts before mutations.
    old_node = kube("get", "node", a.node_name, "--ignore-not-found", "-o", "name").stdout.strip()
    expected = f'{a.role}:false:{a.node_name}:{a.host}:{c["k3s_version"]}'
    identity_check = "/bin/bash -c " + shlex.quote("if [ -f /etc/elektrokube/node-identity ]; then cat /etc/elektrokube/node-identity; fi")
    remote_identity = conn.call(identity_check, root=True, password=not key_works).stdout.decode().strip()
    if (remote_identity and remote_identity != expected) or (old_node and remote_identity != expected):
        raise ValueError("Node name is already registered or the remote machine has a different managed identity")
    if not key_works:
        line = key_line(source, public)
        script = f"""set -eu
. /etc/os-release
[ "$ID:$VERSION_ID" = debian:13 ] || exit 42
user={shlex.quote(a.user)}
user_home=$(getent passwd "$user" | cut -d: -f6)
user_group=$(id -gn "$user")
install -d -m 0700 -o "$user" -g "$user_group" "$user_home/.ssh"
touch "$user_home/.ssh/authorized_keys"
line={shlex.quote(line)}
grep -qxF "$line" "$user_home/.ssh/authorized_keys" || printf '%s\\n' "$line" >> "$user_home/.ssh/authorized_keys"
chown "$user:$user_group" "$user_home/.ssh/authorized_keys"
chmod 0600 "$user_home/.ssh/authorized_keys"
"""
        conn.call("/bin/bash -c " + shlex.quote(script), password=True, root=True, capture=False)
        conn.call("true")  # Separate key-only connection BEFORE any authentication change.
    conn.login_password = None
    print("Verified the new key. Copying scripts and applying transactional SSH restrictions.", flush=True)
    descriptor = {"host": a.host, "user": a.user, "port": a.port, "source_ip": source,
                  "public_key": public, "node_name": a.node_name, "role": a.role,
                  "gpu": a.gpu, "iommu": a.enable_iommu}
    token_path = Path("/var/lib/rancher/k3s/server/agent-token" if a.role == "worker" else "/var/lib/rancher/k3s/server/token")
    token = token_path.read_bytes()
    if not token.strip():
        raise ValueError("Empty cluster join token")
    stage = conn.call("mktemp -d /tmp/elektrokube.XXXXXXXX").stdout.decode().strip()
    if not re.fullmatch(r"/tmp/elektrokube\.[A-Za-z0-9]+", stage):
        raise ValueError("Unexpected remote staging path")
    try:
        with tempfile.TemporaryDirectory(prefix="elektrokube-") as local:
            bundle = Path(local) / "bundle.tar.gz"
            make_bundle(bundle, descriptor, c, token)
            conn.call("umask 077; cat > " + shlex.quote(stage + "/bundle.tar.gz"), data=bundle.read_bytes())
        setup = f"""set -eu
. /etc/os-release
[ "$ID:$VERSION_ID" = debian:13 ] || exit 42
test -d /run/systemd/system
if [ -d /var/lib/rancher/k3s ] && [ ! -f /etc/elektrokube/node-identity ]; then
  echo 'Refusing to enroll an unmanaged k3s node' >&2; exit 1
fi
if ! command -v python3 >/dev/null; then apt-get update; DEBIAN_FRONTEND=noninteractive apt-get install -y python3; fi
if [ -e /opt/elektrokube-scripts ] && [ ! -f /opt/elektrokube-scripts/.managed ]; then
  echo 'Refusing to replace an unmanaged /opt/elektrokube-scripts' >&2; exit 1
fi
install -d -m 0700 /opt/elektrokube-scripts
tar --no-same-owner --no-same-permissions -xzf {shlex.quote(stage + '/bundle.tar.gz')} -C /opt/elektrokube-scripts
chown -R root:root /opt/elektrokube-scripts
chmod 0700 /opt/elektrokube-scripts/*.sh /opt/elektrokube-scripts/lib/*.sh
touch /opt/elektrokube-scripts/.managed
"""
        conn.call("/bin/bash -c " + shlex.quote(setup), root=True, capture=False)
    finally:
        conn.call("rm -rf -- " + shlex.quote(stage), check=False)
    try:
        conn.call("/usr/bin/python3 /opt/elektrokube-scripts/lib/ssh_access.py harden", root=True, capture=False)
        # ControlMaster is disabled: this must be a NEW authenticated TCP connection.
        conn.call("true")
        conn.call("/usr/bin/python3 /opt/elektrokube-scripts/lib/ssh_access.py commit", root=True, capture=False)
    except Exception:
        print("SSH hardening did not complete. Allow 120 seconds for automatic rollback, then rerun.", flush=True)
        raise
    nodes = STATE / "nodes"
    nodes.mkdir(mode=0o700, exist_ok=True)
    record = dict(descriptor, key_file=str(key), known_hosts_file=str(known))
    record.pop("public_key")
    (nodes / (a.node_name + ".json")).write_text(json.dumps(record, indent=2) + "\n")
    print("Key-only SSH restricted to the first node is verified. Preparing and joining the node.", flush=True)
    conn.call("/opt/elektrokube-scripts/lib/provision-remote.sh", root=True, capture=False)
    conn.sudo_password = None
    for _ in range(120):
        if kube("get", "node", a.node_name, check=False).returncode == 0:
            break
        time.sleep(2)
    run(["/usr/local/bin/k3s", "kubectl", "--kubeconfig", "/etc/rancher/k3s/k3s.yaml",
         "wait", "--for=condition=Ready", "node/" + a.node_name, "--timeout=300s"])
    run(["/usr/local/bin/k3s", "kubectl", "--kubeconfig", "/etc/rancher/k3s/k3s.yaml",
         "-n", "kube-system", "rollout", "status", "daemonset/cilium", "--timeout=300s"])
    print(f"Added {a.node_name} as {a.role}. SSH key: {key}")
    print("Manual SSH (run with sudo on this first node):\n" + shlex.join(conn.base + ["-i", str(key), f"{a.user}@{a.host}"]))
    print("No automatic reboot was performed. Check the GPU/virtualization report and README acceptance steps.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Enrollment stopped: {exc}\nCorrect the reported issue and rerun the same command to resume.") from exc
