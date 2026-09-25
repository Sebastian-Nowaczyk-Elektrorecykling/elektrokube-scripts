#!/usr/bin/env python3
"""Root-only transactional SSH configuration on an enrolled Debian node."""
import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

STATE = Path("/etc/elektrokube/ssh")
SSHD_CONFIG = Path("/etc/ssh/sshd_config")
UNIT = "elektrokube-ssh-rollback"
AUTH_KEYS = Path("/etc/ssh/elektrokube/authorized_keys")


def ssh_config(user, source, port):
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        raise ValueError("Invalid SSH user")
    ipaddress.IPv4Address(source)
    if not 1 <= port <= 65535:
        raise ValueError("Invalid SSH port")
    root_login = "prohibit-password" if user == "root" else "no"
    # A complete config avoids Include/Match precedence weakening a late drop-in.
    return f"""# Managed by elektrokube. Original: /etc/elektrokube/ssh/sshd_config.original
Port {port}
AddressFamily inet
UsePAM yes
PubkeyAuthentication yes
AuthenticationMethods publickey
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PermitRootLogin {root_login}
AllowUsers {user}@{source}
AuthorizedKeysFile /etc/ssh/elektrokube/authorized_keys/%u
AuthorizedKeysCommand none
TrustedUserCAKeys none
HostbasedAuthentication no
GSSAPIAuthentication no
PermitUserEnvironment no
DisableForwarding yes
X11Forwarding no
PermitTunnel no
PermitTTY yes
UseDNS no
MaxAuthTries 3
LoginGraceTime 30
Subsystem sftp /usr/lib/openssh/sftp-server
"""


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout


def reload_ssh():
    run("/usr/sbin/sshd", "-t")
    run("systemctl", "reload", "ssh.service")


def rollback(transaction=None):
    if not (STATE / "pending").exists():
        return
    pending = json.loads((STATE / "pending").read_text())
    if transaction and transaction != pending["transaction"]:
        return
    shutil.copy2(STATE / "sshd_config.before", SSHD_CONFIG)
    reload_ssh()
    (STATE / "pending").unlink()


def harden(descriptor):
    if (STATE / "pending").exists():
        raise ValueError("An SSH transaction is pending; allow its rollback to finish before retrying")
    d = json.loads(Path(descriptor).read_text())
    content = ssh_config(d["user"], d["source_ip"], d["port"])
    key = d["public_key"].strip()
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?", key):
        raise ValueError("Expected one Ed25519 public key")
    keydir = AUTH_KEYS
    keydir.mkdir(parents=True, exist_ok=True, mode=0o755)
    keydir.parent.chmod(0o755)
    keydir.chmod(0o755)
    keypath = keydir / d["user"]
    keypath.write_text(f'from="{d["source_ip"]}",no-agent-forwarding,no-port-forwarding,no-X11-forwarding {key}\n')
    keypath.chmod(0o644)  # Public key only; sshd reads it as the authenticating user.
    candidate = STATE / "sshd_config.candidate"
    candidate.write_text(content)
    candidate.chmod(0o600)
    run("/usr/sbin/sshd", "-t", "-f", str(candidate))
    if not (STATE / "sshd_config.original").exists():
        shutil.copy2(SSHD_CONFIG, STATE / "sshd_config.original")
    shutil.copy2(SSHD_CONFIG, STATE / "sshd_config.before")
    transaction = uuid.uuid4().hex
    unit = UNIT + "-" + transaction
    (STATE / "pending").write_text(json.dumps({"digest": hashlib.sha256(content.encode()).hexdigest(),
                                               "unit": unit, "transaction": transaction}))
    try:
        # Arm BEFORE replacing/reloading. A stale timer cannot undo a later transaction.
        run("systemd-run", "--quiet", "--collect", "--unit=" + unit, "--on-active=120s",
            "/usr/bin/python3", str(Path(__file__).resolve()), "rollback", "--transaction", transaction)
        shutil.copyfile(candidate, SSHD_CONFIG)
        SSHD_CONFIG.chmod(0o644)
        reload_ssh()
    except Exception:
        rollback()
        raise


def commit():
    pending = STATE / "pending"
    if not pending.exists():
        raise ValueError("SSH rollback has already run; refusing to report hardening as successful")
    state = json.loads(pending.read_text())
    if hashlib.sha256(SSHD_CONFIG.read_bytes()).hexdigest() != state["digest"]:
        raise ValueError("SSH config changed during verification")
    pending.unlink()
    run("systemctl", "stop", state["unit"] + ".timer")
    (STATE / "committed").write_text("Key-only SSH verified from the first node.\n")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["harden", "commit", "rollback"])
    p.add_argument("--descriptor", default="/opt/elektrokube-scripts/payload/node.json")
    p.add_argument("--transaction")
    a = p.parse_args()
    if os.geteuid() != 0:
        p.error("Must run as root")
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open("/run/lock/elektrokube-ssh.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if a.action == "harden":
            harden(a.descriptor)
        elif a.action == "commit":
            commit()
        else:
            rollback(a.transaction)


if __name__ == "__main__":
    main()
