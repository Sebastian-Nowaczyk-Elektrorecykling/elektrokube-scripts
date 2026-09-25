"""Real SSH/PAM checks, ONLY in the disposable Debian 13 CI container.

This changes that container's root password and creates a test account. It must
never be run on a real node. Offline test discovery deliberately excludes it.
"""
import argparse
import os
from pathlib import Path
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import enroll
import ssh_access


def main():
    if (os.environ.get("ELEKTROKUBE_DISPOSABLE_TEST_CONTAINER") != "1"
            or os.environ.get("GITHUB_ACTIONS") != "true"
            or not Path("/.dockerenv").exists() or os.geteuid() != 0
            or 'VERSION_ID="13"' not in Path("/etc/os-release").read_text()):
        raise SystemExit("Run only in the dedicated disposable Debian 13 GitHub Actions container")
    assert shutil.which("sudo") is None, "The target must have no sudo installed"
    assert not Path("/etc/elektrokube").exists(), "Never run against a provisioned node"
    login_password = secrets.token_urlsafe(24) + "'$\\!"
    root_password = secrets.token_urlsafe(24) + "'$\\!"
    subprocess.run(["useradd", "--create-home", "--shell", "/bin/bash", "enroller"], check=True)
    subprocess.run(["chpasswd"], input=f"enroller:{login_password}\nroot:{root_password}\n".encode(), check=True)
    Path("/run/sshd").mkdir(exist_ok=True)
    # Keep the key directory beneath root-owned /run: OpenSSH StrictModes
    # correctly rejects authorized_keys paths with a world-writable /tmp parent.
    with tempfile.TemporaryDirectory(prefix="su-integration-", dir="/run") as directory:
        tmp = Path(directory)
        # sshd reads public keys as the authenticating user; private keys stay 0600.
        tmp.chmod(0o755)
        key, host_key, known = tmp / "client", tmp / "host", tmp / "known_hosts"
        for path in (key, host_key):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        known.write_text(f"[127.0.0.1]:{port} " + host_key.with_suffix(".pub").read_text())
        authorized = tmp / "authorized_keys"
        config_path, log_path = tmp / "sshd_config", tmp / "sshd.log"
        # Exercise the same final normal-user policy as enrollment, with only
        # file locations and listen address overridden for this test server.
        final_config = ssh_access.ssh_config("enroller", "127.0.0.1", port)
        final_config = final_config.replace("/etc/ssh/elektrokube/authorized_keys/%u", str(authorized))
        final_config += f"ListenAddress 127.0.0.1\nHostKey {host_key}\nPidFile {tmp / 'pid'}\n"
        initial_config = final_config.replace("AuthenticationMethods publickey", "AuthenticationMethods any")
        initial_config = initial_config.replace("PasswordAuthentication no", "PasswordAuthentication yes")
        config_path.write_text(initial_config)
        subprocess.run(["/usr/sbin/sshd", "-t", "-f", str(config_path)], check=True)
        with log_path.open("wb") as log:
            daemon = subprocess.Popen(["/usr/sbin/sshd", "-D", "-e", "-f", str(config_path)], stderr=log)
            try:
                for _ in range(100):
                    assert daemon.poll() is None, log_path.read_text()
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                            break
                    except OSError:
                        time.sleep(0.05)
                else:
                    raise AssertionError("Test SSH server did not start")
                args = argparse.Namespace(host="127.0.0.1", port=port, user="enroller", elevate="su")
                conn = enroll.Connection(args, "127.0.0.1", key, known)
                with patch.object(enroll.getpass, "getpass", return_value=login_password) as prompt:
                    key_works, observed = enroll.authenticate(conn)
                assert not key_works and observed.startswith(b"127.0.0.1 ")
                prompt.assert_called_once()

                # A bad root password must fail before installing a key.
                with patch.object(enroll.getpass, "getpass", return_value="wrong-root-password"):
                    try:
                        enroll.configure_elevation(conn, password=True)
                    except RuntimeError as error:
                        assert "Root access through su failed" in str(error)
                    else:
                        raise AssertionError("su accepted a wrong root password")
                assert not authorized.exists()
                with patch.object(enroll.getpass, "getpass", return_value=root_password) as prompt:
                    enroll.configure_elevation(conn, password=True)
                prompt.assert_called_once()

                checks = 'test "$HOME" = /root && test "$PWD" = /root && test ! -t 0 && ! read -r secret && id -u'
                result = conn.call("/bin/bash -c " + shlex.quote(checks), password=True, root=True, capture_error=True)
                assert result.stdout == b"0\n", result
                assert result.stderr == b"", result
                failed = conn.call("/bin/bash -c 'echo expected-error >&2; exit 37'", password=True,
                                   root=True, check=False, capture_error=True)
                assert failed.returncode == 37 and failed.stderr == b"expected-error\n", failed

                # Install the key through su, then prove elevation survives the
                # switch to key-only SSH while direct root login stays disabled.
                public = key.with_suffix(".pub").read_text().strip()
                line = 'from="127.0.0.1",no-agent-forwarding,no-port-forwarding,no-X11-forwarding ' + public
                script = f"umask 022; printf '%s\\n' {shlex.quote(line)} > {shlex.quote(str(authorized))}"
                conn.call("/bin/bash -c " + shlex.quote(script), password=True, root=True)
                conn.call("true")
                config_path.write_text(final_config)
                subprocess.run(["/usr/sbin/sshd", "-t", "-f", str(config_path)], check=True)
                daemon.send_signal(signal.SIGHUP)
                for _ in range(50):
                    rejected = conn.call("true", password=True, capture_error=True, check=False)
                    if rejected.returncode:
                        assert b"Permission denied" in rejected.stderr, rejected
                        break
                    time.sleep(0.05)
                else:
                    raise AssertionError("SSH password authentication remained enabled")
                conn.login_password = None
                with patch.object(enroll.getpass, "getpass") as prompt:
                    assert enroll.authenticate(conn)[0]
                prompt.assert_not_called()
                result = conn.call("/usr/bin/id -u", root=True, capture_error=True)
                assert result.stdout == b"0\n" and result.stderr == b"", result
                root_args = argparse.Namespace(host="127.0.0.1", port=port, user="root")
                root_conn = enroll.Connection(root_args, "127.0.0.1", key, known)
                denied = root_conn.call("true", capture_error=True, check=False)
                assert denied.returncode != 0 and b"Permission denied" in denied.stderr, denied
                for value in (result.stdout, result.stderr, failed.stdout, failed.stderr, log_path.read_bytes()):
                    assert login_password.encode() not in value and root_password.encode() not in value
                print("PASS: Debian 13 normal-user SSH + su, no sudo, password isolation, failure status, key-only SSH")
            except BaseException:
                print(log_path.read_text().replace(login_password, "[redacted]").replace(root_password, "[redacted]"),
                      file=sys.stderr)
                raise
            finally:
                daemon.terminate()
                daemon.wait(timeout=10)


if __name__ == "__main__":
    main()
