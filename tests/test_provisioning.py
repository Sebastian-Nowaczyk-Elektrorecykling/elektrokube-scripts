"""Offline tests of provisioning boundaries; never modify a host or contact a cluster."""
import argparse
import base64
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import config
import enroll
import ssh_access


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.c = json.loads((ROOT / "config/cluster.json").read_text())
        self.c["api_address"] = "192.168.2.153"

    def test_defaults_valid_and_versions_pinned(self):
        self.assertEqual(config.validate(self.c), self.c)
        self.assertEqual(config.cilium_values(self.c)["k8sServiceHost"], "192.168.2.153")
        self.assertTrue(config.cilium_values(self.c)["kubeProxyReplacement"])
        self.assertEqual(config.cilium_values(self.c)["ipam"]["mode"], "kubernetes")

    def test_server_join_has_matching_critical_configuration(self):
        first = config.node_config(self.c, "hybrid", "192.168.2.153", "first", True)
        joined = config.node_config(self.c, "controller", "192.168.2.154", "next")
        for key in ("flannel-backend", "disable-network-policy", "disable-kube-proxy", "disable",
                    "cluster-cidr", "service-cidr", "cluster-dns", "secrets-encryption"):
            self.assertEqual(first[key], joined[key])
        self.assertTrue(first["cluster-init"])
        self.assertNotIn("cluster-init", joined)
        self.assertEqual(joined["node-taint"], [config.TAINT])
        self.assertNotIn("node-taint", first)
        self.assertEqual(joined["token-file"], "/etc/rancher/k3s/join.token")

    def test_worker_does_not_receive_server_only_flags(self):
        worker = config.node_config(self.c, "worker", "192.168.2.155", "worker")
        for key in ("cluster-init", "disable-kube-proxy", "flannel-backend", "secrets-encryption", "node-taint"):
            self.assertNotIn(key, worker)
        self.assertEqual(worker["server"], "https://192.168.2.153:6443")

    def test_controller_essential_components_tolerate_taint(self):
        values = config.cilium_values(self.c)
        for component in (values["operator"], values["hubble"]["relay"], values["hubble"]["ui"]):
            self.assertIn({"key": "CriticalAddonsOnly", "operator": "Exists"}, component["tolerations"])

    def test_reject_bad_configuration_before_host_work(self):
        mutations = [("pod_cidr", "10.43.0.0/16"), ("pod_cidr", "10.42.0.1/16"),
                     ("cluster_dns", "1.1.1.1"), ("cluster_dns", "10.43.0.1"),
                     ("api_address", "10.42.1.3"), ("api_address", "127.0.0.1"),
                     ("helm_version", "latest"), ("k3s_version", "v1.36.4+k3s1;id"),
                     ("k3s_installer_sha256", "bad"), ("cluster_name", "UPPER")]
        for key, value in mutations:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                bad = copy.deepcopy(self.c); bad[key] = value
                config.validate(bad)
        with self.assertRaises(ValueError):
            config.validate(dict(self.c, unknown="value"))

    def test_reject_invalid_node_identity(self):
        for address in ("127.0.0.1", "10.42.1.2", "10.43.1.2", "0.0.0.0"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                config.node_config(self.c, "worker", address, "node")
        with self.assertRaises(ValueError):
            config.node_config(self.c, "worker", "192.168.2.155", "node", True)
        for name in ("bad;id", "-start", "bad.name", "a" * 64):
            with self.assertRaises(ValueError):
                config.node_name(name)


class SSHTests(unittest.TestCase):
    def test_root_commands_do_not_require_sudo(self):
        a = argparse.Namespace(host="192.168.2.154", port=22, user="root")
        c = enroll.Connection(a, "192.168.2.153", Path("key"), Path("known"))
        c.login_password = "root-login-secret"
        def fake_run(argv, **kwargs):
            self.assertEqual(argv[-1], "/usr/bin/true")
            self.assertNotIn("sudo", " ".join(argv))
            self.assertIsNone(kwargs["input"])
            self.assertEqual(os.read(kwargs["pass_fds"][0], 4096), b"root-login-secret\n")
            return subprocess.CompletedProcess(argv, 0, b"")
        with patch.object(enroll.subprocess, "run", side_effect=fake_run):
            c.call("/usr/bin/true", password=True, root=True)

    def test_authentication_policy_is_exclusive(self):
        text = ssh_access.ssh_config("admin", "192.168.2.153", 2222)
        for required in ("AllowUsers admin@192.168.2.153", "PasswordAuthentication no",
                         "KbdInteractiveAuthentication no", "AuthenticationMethods publickey",
                         "TrustedUserCAKeys none", "AuthorizedKeysCommand none", "PermitRootLogin no"):
            self.assertIn(required, text)
        self.assertNotIn("\nInclude ", text)
        self.assertNotIn("\nMatch ", text)
        self.assertIn("PermitRootLogin prohibit-password", ssh_access.ssh_config("root", "192.168.2.153", 22))
        for bad in ("x\nPermitRootLogin yes", "*@*"):
            with self.assertRaises(ValueError):
                ssh_access.ssh_config(bad, "192.168.2.153", 22)

    def test_passwords_never_in_argv_or_environment(self):
        a = argparse.Namespace(host="192.168.2.154", port=22, user="admin", elevate="sudo")
        c = enroll.Connection(a, "192.168.2.153", Path("key"), Path("known"))
        c.login_password = "private-login-password"
        c.sudo_password = "private-sudo-password"
        seen = []
        def fake_run(argv, **kwargs):
            self.assertNotIn(c.login_password, " ".join(argv))
            self.assertNotIn(c.sudo_password, " ".join(argv))
            self.assertNotIn("env", kwargs)
            self.assertEqual(os.read(kwargs["pass_fds"][0], 4096), (c.login_password + "\n").encode())
            self.assertEqual(kwargs["input"], (c.sudo_password + "\n").encode())
            self.assertIn("</dev/null", argv[-1])
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, b"")
        with patch.object(enroll.subprocess, "run", side_effect=fake_run):
            c.call("/usr/bin/true", password=True, root=True)
        self.assertEqual(len(seen), 1)

    def test_su_password_uses_stdin_without_a_terminal_or_sudo(self):
        a = argparse.Namespace(host="192.168.2.154", port=22, user="admin")
        c = enroll.Connection(a, "192.168.2.153", Path("key"), Path("known"))
        c.login_password = "login-secret"
        c.root_password = "root-secret-'$\\!"
        def fake_run(argv, **kwargs):
            self.assertNotIn(c.root_password, " ".join(argv))
            self.assertNotIn(c.login_password, " ".join(argv))
            self.assertNotIn("env", kwargs)
            self.assertNotIn("sudo", argv[-1])
            self.assertIn("-T", argv)
            self.assertIn("su --login --shell /bin/bash --command", argv[-1])
            self.assertIn("</dev/null", argv[-1])
            self.assertEqual(os.read(kwargs["pass_fds"][0], 4096), b"login-secret\n")
            self.assertEqual(kwargs["input"], (c.root_password + "\n").encode())
            return subprocess.CompletedProcess(argv, 0, b"0\n", b"Password: diagnostic\n")
        with patch.object(enroll.subprocess, "run", side_effect=fake_run):
            result = c.call("/usr/bin/id -u", password=True, root=True, capture_error=True)
        self.assertEqual(result.stdout, b"0\n")
        self.assertEqual(result.stderr, b"diagnostic\n")
        with self.assertRaisesRegex(ValueError, "input payload"):
            c.call("/bin/cat", root=True, data=b"payload")

    def test_su_preflight_uses_root_password_and_rejects_bad_credentials_before_mutations(self):
        for status, output in ((0, b"0\n"), (1, b""), (0, b"1000\n")):
            with self.subTest(status=status, output=output):
                conn = Mock(a=argparse.Namespace(user="debian"), elevation="su", login_password="user-secret")
                conn.call.side_effect = [subprocess.CompletedProcess([], 0, b"/usr/bin/su\n", b""),
                                         subprocess.CompletedProcess([], status, output, b"")]
                with patch.object(enroll.getpass, "getpass", return_value="root-secret") as prompt:
                    if status == 0 and output == b"0\n":
                        enroll.configure_elevation(conn, password=True)
                    else:
                        with self.assertRaisesRegex(RuntimeError, "Root access through su failed"):
                            enroll.configure_elevation(conn, password=True)
                self.assertEqual(conn.root_password, "root-secret")
                prompt.assert_called_once()
                self.assertEqual([call.args[0] for call in conn.call.call_args_list],
                                 ["command -v su", "/usr/bin/id -u"])
                self.assertTrue(conn.call.call_args.kwargs["root"])

    def test_explicit_sudo_still_supports_reusing_login_password(self):
        conn = Mock(a=argparse.Namespace(user="debian"), elevation="sudo", login_password="user-secret")
        conn.call.side_effect = [subprocess.CompletedProcess([], 0, b"/usr/bin/sudo\n", b""),
                                 subprocess.CompletedProcess([], 0, b"0\n", b"")]
        with patch.object(enroll.getpass, "getpass", return_value=""):
            enroll.configure_elevation(conn, password=True)
        self.assertEqual(conn.sudo_password, "user-secret")

    def test_root_login_never_prompts_for_elevation(self):
        conn = Mock(a=argparse.Namespace(user="root"))
        with patch.object(enroll.getpass, "getpass") as prompt:
            enroll.configure_elevation(conn, password=True)
        prompt.assert_not_called()
        conn.call.assert_not_called()

    def test_missing_sudo_recommends_su_before_prompting(self):
        conn = Mock(a=argparse.Namespace(user="debian"), elevation="sudo")
        conn.call.return_value = subprocess.CompletedProcess([], 1, b"", b"")
        with patch.object(enroll.getpass, "getpass") as prompt:
            with self.assertRaisesRegex(RuntimeError, "--elevate su"):
                enroll.configure_elevation(conn, password=True)
        prompt.assert_not_called()

    def test_keys_are_source_restricted_and_key_checks_use_new_connections(self):
        line = enroll.key_line("192.168.2.153", "ssh-ed25519 AAAA test")
        self.assertTrue(line.startswith('from="192.168.2.153",'))
        self.assertIn("no-port-forwarding", line)
        a = argparse.Namespace(host="192.168.2.154", port=22, user="admin")
        c = enroll.Connection(a, "192.168.2.153", Path("key"), Path("known"))
        with patch.object(enroll.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"")) as mocked:
            c.call("true")
        argv = mocked.call_args.args[0]
        for option in ("BatchMode=yes", "ControlMaster=no", "StrictHostKeyChecking=yes", "PreferredAuthentications=publickey"):
            self.assertIn(option, argv)

    def test_fingerprint_uses_key_bytes(self):
        blob = base64.b64encode(b"test-host-key").decode()
        self.assertTrue(enroll.fingerprint("host ssh-ed25519 " + blob).startswith("SHA256:"))

    def test_fresh_root_falls_back_to_password_after_key_refusal(self):
        conn = Mock(a=argparse.Namespace(user="root"), source="192.168.2.153")
        conn.call.side_effect = [subprocess.CompletedProcess([], 255, b"", b"Permission denied (publickey,password)."),
                                 subprocess.CompletedProcess([], 0, b"192.168.2.153 1234 192.168.2.154 22\n", b"")]
        with patch.object(enroll.getpass, "getpass", return_value="secret") as prompt:
            key_works, observed = enroll.authenticate(conn)
        self.assertFalse(key_works)
        self.assertTrue(observed.startswith(b"192.168.2.153 "))
        self.assertEqual(conn.login_password, "secret")
        prompt.assert_called_once()
        self.assertTrue(conn.call.call_args_list[1].kwargs["password"])

    def test_transport_failure_does_not_ask_for_a_password(self):
        conn = Mock(a=argparse.Namespace(user="root"), source="192.168.2.153")
        conn.call.return_value = subprocess.CompletedProcess([], 255, b"", b"connect to host: Connection refused")
        with patch.object(enroll.getpass, "getpass") as prompt:
            with self.assertRaisesRegex(RuntimeError, "openssh-server"):
                enroll.authenticate(conn)
        prompt.assert_not_called()

    def test_root_rejection_explains_policy_without_claiming_bad_password(self):
        conn = Mock(a=argparse.Namespace(user="root"), source="192.168.2.153")
        conn.call.side_effect = [subprocess.CompletedProcess([], 255, b"", b"Permission denied (publickey)."),
                                 subprocess.CompletedProcess([], 5, b"", b"Permission denied, please try again.")]
        with patch.object(enroll.getpass, "getpass", return_value="do-not-log-this"):
            with self.assertRaises(RuntimeError) as error:
                enroll.authenticate(conn)
        self.assertIn("Root enrollment needs no sudo", str(error.exception))
        self.assertIn("192.168.2.153", str(error.exception))
        self.assertNotIn("do-not-log-this", str(error.exception))

    def test_existing_key_does_not_ask_for_password(self):
        conn = Mock(a=argparse.Namespace(user="root"), source="192.168.2.153")
        conn.call.return_value = subprocess.CompletedProcess([], 0, b"192.168.2.153 1234 192.168.2.154 22\n", b"")
        with patch.object(enroll.getpass, "getpass") as prompt:
            key_works, _ = enroll.authenticate(conn)
        self.assertTrue(key_works)
        prompt.assert_not_called()

    def test_bundle_excludes_git_and_private_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "bundle.tar.gz"
            enroll.make_bundle(target, {"role": "worker"}, {}, b"join-secret")
            with tarfile.open(target) as archive:
                names = archive.getnames()
                self.assertIn("payload/join.token", names)
                self.assertEqual(archive.getmember("payload/join.token").mode, 0o600)
                self.assertFalse(any(".git" in n or "ed25519" in n or n.startswith("/") or ".." in n for n in names))


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"; self.state.mkdir()
        self.conf = self.root / "sshd_config"; self.conf.write_text("old ssh config\n")
        self.descriptor = self.root / "node.json"
        self.descriptor.write_text(json.dumps({"user": "admin", "source_ip": "192.168.2.153", "port": 22,
                                              "public_key": "ssh-ed25519 AAAA test"}))
        self.patches = [patch.object(ssh_access, "STATE", self.state),
                        patch.object(ssh_access, "SSHD_CONFIG", self.conf),
                        patch.object(ssh_access, "AUTH_KEYS", self.root / "ssh" / "keys"),
                        patch.object(ssh_access, "ENROLLMENT_DROPIN", self.root / "enrollment.conf")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_timer_armed_before_replacement_and_commit_cancels_it(self):
        def mocked(*args):
            if args[0] == "systemd-run":
                self.assertEqual(self.conf.read_text(), "old ssh config\n")
            return ""
        with patch.object(ssh_access, "run", side_effect=mocked) as run:
            ssh_access.harden(self.descriptor)
            self.assertIn("PasswordAuthentication no", self.conf.read_text())
            ssh_access.commit()
            self.assertFalse((self.state / "pending").exists())
            self.assertTrue(any(c.args[:2] == ("systemctl", "stop") for c in run.call_args_list))

    def test_failed_timer_or_reload_restores_original(self):
        for command in ("systemd-run", "systemctl"):
            self.conf.write_text("old ssh config\n")
            failed = False
            def mocked(*args):
                nonlocal failed
                if args[0] == command and not failed:
                    failed = True
                    raise RuntimeError("simulated failure")
                return ""
            with self.subTest(command=command), patch.object(ssh_access, "run", side_effect=mocked):
                with self.assertRaises(RuntimeError):
                    ssh_access.harden(self.descriptor)
                self.assertEqual(self.conf.read_text(), "old ssh config\n")
                self.assertFalse((self.state / "pending").exists())

    def test_stale_timer_cannot_undo_a_new_transaction(self):
        with patch.object(ssh_access, "run", return_value=""):
            ssh_access.harden(self.descriptor)
            ssh_access.rollback("old-transaction-id")
            self.assertIn("PasswordAuthentication no", self.conf.read_text())
            ssh_access.rollback()
            self.assertEqual(self.conf.read_text(), "old ssh config\n")
            with self.assertRaises(ValueError):
                ssh_access.commit()

    def test_temporary_password_rule_removed_only_after_verified_commit(self):
        dropin = self.root / "enrollment.conf"
        dropin.write_text(ssh_access.ENROLLMENT_MARKER + "\nPermitRootLogin yes\n")
        with patch.object(ssh_access, "run", return_value=""):
            ssh_access.harden(self.descriptor)
            ssh_access.rollback()
            self.assertTrue(dropin.exists())
            ssh_access.harden(self.descriptor)
            ssh_access.commit()
            self.assertFalse(dropin.exists())

    def test_commit_preserves_unrelated_enrollment_file(self):
        dropin = self.root / "enrollment.conf"
        dropin.write_text("# User-managed settings\nPermitRootLogin no\n")
        with patch.object(ssh_access, "run", return_value=""):
            ssh_access.harden(self.descriptor)
            ssh_access.commit()
            self.assertTrue(dropin.exists())


if __name__ == "__main__":
    unittest.main()
