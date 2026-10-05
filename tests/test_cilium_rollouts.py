"""Guard Cilium's config-triggered rollouts in bootstrap Helm values."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from config import cilium_values


class CiliumRolloutTests(unittest.TestCase):
    def setUp(self):
        self.settings = json.loads((ROOT / "config/cluster.json").read_text())
        self.settings["api_address"] = "192.168.2.153"
        self.values = cilium_values(self.settings)

    def test_agents_roll_when_cilium_config_changes(self):
        self.assertIs(self.values.get("rollOutCiliumPods"), True)

    def test_operator_rolls_when_cilium_config_changes(self):
        self.assertIs(self.values["operator"].get("rollOutPods"), True)

    def test_envoy_rolls_when_its_config_changes(self):
        self.assertIs(self.values["envoy"].get("rollOutPods"), True)

    def test_host_network_gateway_and_privileged_port_capabilities_are_preserved(self):
        self.assertIs(self.values["gatewayAPI"]["enabled"], True)
        self.assertIs(self.values["gatewayAPI"]["hostNetwork"]["enabled"], True)
        self.assertIs(self.values["envoy"]["enabled"], True)
        capabilities = self.values["envoy"]["securityContext"]["capabilities"]
        self.assertIs(capabilities["keepCapNetBindService"], True)
        self.assertIn("NET_BIND_SERVICE", capabilities["envoy"])

    def test_bootstrap_cli_emits_the_same_rollout_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "cluster.json"
            config.write_text(json.dumps(self.settings))
            result = subprocess.run(
                [sys.executable, str(ROOT / "lib/config.py"), "cilium", "--config", str(config)],
                check=True, capture_output=True, text=True)
        values = json.loads(result.stdout)
        self.assertEqual(values, self.values)
        self.assertIs(values["rollOutCiliumPods"], True)
        self.assertIs(values["operator"]["rollOutPods"], True)
        self.assertIs(values["envoy"]["rollOutPods"], True)

    def test_rendering_is_repeatable_and_does_not_mutate_saved_settings(self):
        before = self.settings.copy()
        self.assertEqual(cilium_values(self.settings), cilium_values(self.settings))
        self.assertEqual(self.settings, before)


if __name__ == "__main__":
    unittest.main()
