"""Test default selection without reading or modifying a host's networking."""
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from discover import choose_ip


def link(name, *addresses):
    return {"ifname": name, "addr_info": [
        {"family": "inet", "scope": "global", "local": ip} for ip in addresses]}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.c = json.loads((ROOT / "config/cluster.json").read_text())
        self.links = [link("enp3s0", "192.168.2.153"), link("wlan0", "192.168.10.20"),
                      link("docker0", "172.17.0.1"), link("wg0", "10.20.0.2")]
        self.routes = [{"dst": "default", "dev": "enp3s0", "metric": 100},
                       {"dst": "default", "dev": "wlan0", "metric": 600}]

    def test_default_route_and_interface_override(self):
        self.assertEqual(choose_ip(self.links, self.routes, self.c), "192.168.2.153")
        self.assertEqual(choose_ip(self.links, self.routes, self.c, interface="wlan0"), "192.168.10.20")

    def test_explicit_saved_and_configured_precedence(self):
        self.c["api_address"] = "192.168.2.153"
        self.assertEqual(choose_ip(self.links, self.routes, self.c, saved="192.168.10.20"), "192.168.10.20")
        self.assertEqual(choose_ip(self.links, self.routes, self.c,
                                   saved="192.168.10.20", explicit="192.168.2.153"), "192.168.2.153")
        self.assertEqual(choose_ip(self.links, [], self.c), "192.168.2.153")

    def test_saved_address_must_still_be_present(self):
        with self.assertRaisesRegex(ValueError, "restore the address"):
            choose_ip(self.links, self.routes, self.c, saved="192.168.2.99")
        with self.assertRaises(ValueError):
            choose_ip(self.links, self.routes, self.c, explicit="192.168.2.153", interface="wlan0")

    def test_no_default_route_uses_only_suitable_single_address(self):
        links = [self.links[0], self.links[2], self.links[3], link("lo", "127.0.0.1"),
                 link("eth1", "169.254.2.3"), link("bridge0", "10.42.1.1")]
        self.assertEqual(choose_ip(links, [], self.c), "192.168.2.153")
        with self.assertRaisesRegex(ValueError, "--interface"):
            choose_ip(self.links, [], self.c)

    def test_equally_preferred_routes_are_ambiguous(self):
        self.routes[1]["metric"] = 100
        with self.assertRaisesRegex(ValueError, "Cannot select one"):
            choose_ip(self.links, self.routes, self.c)

    def test_multiple_addresses_need_a_preferred_source_or_override(self):
        links = [link("enp3s0", "192.168.2.153", "192.168.2.154")]
        with self.assertRaises(ValueError):
            choose_ip(links, self.routes, self.c)
        self.routes[0]["prefsrc"] = "192.168.2.154"
        self.assertEqual(choose_ip(links, self.routes, self.c), "192.168.2.154")

    def test_vpn_can_be_explicitly_selected_but_not_automatically(self):
        with self.assertRaises(ValueError):
            choose_ip([self.links[3]], [], self.c)
        self.assertEqual(choose_ip(self.links, [], self.c, interface="wg0"), "10.20.0.2")

    def test_invalid_explicit_values_and_unknown_interfaces_fail(self):
        for explicit in ("127.0.0.1", "10.42.1.1", "192.168.2.153;id"):
            with self.subTest(explicit=explicit), self.assertRaises(ValueError):
                choose_ip(self.links, self.routes, self.c, explicit=explicit)
        with self.assertRaises(ValueError):
            choose_ip(self.links, self.routes, self.c, interface="not-present")

    def test_sudo_user_and_current_user_fallback(self):
        command = ['bash', '-c', 'source "$1/lib/common.sh"; default_admin_user', 'bash', str(ROOT)]
        env = dict(os.environ, SUDO_USER="desktop-user")
        self.assertEqual(subprocess.check_output(command, env=env, text=True).strip(), "desktop-user")
        env.pop("SUDO_USER")
        self.assertEqual(subprocess.check_output(command, env=env, text=True).strip(), pwd.getpwuid(os.geteuid()).pw_name)
        env["SUDO_USER"] = "root"
        self.assertEqual(subprocess.check_output(command, env=env, text=True).strip(), "root")


if __name__ == "__main__":
    unittest.main()
