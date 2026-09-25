"""Check Gateway API installation ordering without contacting Kubernetes."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = b"# pinned test Gateway API bundle\n"


class GatewayAPIInstallTests(unittest.TestCase):
    def invoke(self, *, failure="", checksum=None):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "calls"
            script = r'''
set -Eeuo pipefail
source "$TEST_REPO/lib/common.sh"
download() {
  printf 'DOWNLOAD %s\n' "$1" >> "$TEST_GATEWAY_LOG"
  printf '# pinned test Gateway API bundle\n' > "$2"
}
kube() {
  printf 'KUBE %s\n' "$*" >> "$TEST_GATEWAY_LOG"
  if [[ $1 == apply ]]; then
    [[ $TEST_GATEWAY_FAILURE != apply ]] || return 7
    printf '%s\n' \
      customresourcedefinition.apiextensions.k8s.io/gateways.gateway.networking.k8s.io \
      customresourcedefinition.apiextensions.k8s.io/gatewayclasses.gateway.networking.k8s.io \
      validatingadmissionpolicy.admissionregistration.k8s.io/safe-upgrades.gateway.networking.k8s.io
  elif [[ $1 == wait ]]; then
    [[ $TEST_GATEWAY_FAILURE != wait ]] || return 8
  fi
}
install_gateway_api
printf 'CILIUM_INSTALL\n' >> "$TEST_GATEWAY_LOG"
'''
            env = {**os.environ, "TMPDIR": tmp, "TEST_REPO": str(ROOT), "TEST_GATEWAY_LOG": str(log),
                   "TEST_GATEWAY_FAILURE": failure, "GATEWAY_API_VERSION": "v1.6.1",
                   "GATEWAY_API_SHA256": checksum or hashlib.sha256(FIXTURE).hexdigest()}
            result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
            return result, log.read_text().splitlines() if log.exists() else []

    def test_all_crds_established_before_cilium_and_admission_policy_is_not_waited_on(self):
        result, calls = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[0], "DOWNLOAD https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.6.1/standard-install.yaml")
        self.assertIn("apply --server-side --field-manager=kustomize-controller", calls[1])
        waits = [line for line in calls if line.startswith("KUBE wait")]
        self.assertEqual(len(waits), 2)
        self.assertTrue(all("--for=condition=Established" in line for line in waits))
        self.assertFalse(any("validatingadmissionpolicy" in line for line in waits))
        self.assertEqual(calls[-1], "CILIUM_INSTALL")

    def test_bad_checksum_stops_before_cluster_changes(self):
        result, calls = self.invoke(checksum="0" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Gateway API bundle checksum mismatch", result.stderr)
        self.assertFalse(any(line.startswith("KUBE") or line == "CILIUM_INSTALL" for line in calls))

    def test_failed_apply_or_establishment_prevents_cilium_installation(self):
        for failure in ("apply", "wait"):
            with self.subTest(failure=failure):
                result, calls = self.invoke(failure=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(any(line.startswith("KUBE " + failure) for line in calls), result.stderr)
                self.assertNotIn("CILIUM_INSTALL", calls)


if __name__ == "__main__":
    unittest.main()
