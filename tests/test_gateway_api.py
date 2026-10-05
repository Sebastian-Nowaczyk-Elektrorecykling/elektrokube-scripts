"""Check Gateway API installation ordering without contacting Kubernetes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = b"# pinned test Gateway API bundle\n"
# Independent pin from elektrokube-cilium-and-flux/infrastructure/gateway-api/kustomization.yaml
# (Git blob 6a8ca16a7a38db2f140959e9e18a185ae0200a1a). The digest is published at
# https://api.github.com/repos/kubernetes-sigs/gateway-api/releases/assets/479238872.
GITOPS_BUNDLE_URL = "https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.6.1/experimental-install.yaml"
EXPERIMENTAL_SHA256 = "d7fa77650e4ef28fca0411536fcb5e237deb4d50301cfded3be49d9a1b7bbd02"
CRDS = ("gateways", "gatewayclasses", "httproutes", "tcproutes", "udproutes", "tlsroutes")


class GatewayAPIInstallTests(unittest.TestCase):
    def invoke(self, *, failure="", checksum=None):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "calls"
            script = r'''
set -Eeuo pipefail
source "$TEST_REPO/lib/common.sh"
download() {
  printf 'DOWNLOAD %s\n' "$1" >> "$TEST_GATEWAY_LOG"
  [[ $TEST_GATEWAY_FAILURE != download ]] || return 6
  printf '# pinned test Gateway API bundle\n' > "$2"
}
kube() {
  printf 'KUBE %s\n' "$*" >> "$TEST_GATEWAY_LOG"
  if [[ $1 == apply ]]; then
    [[ $TEST_GATEWAY_FAILURE != apply ]] || return 7
    printf '%s\n' "$TEST_GATEWAY_CRDS" \
      validatingadmissionpolicy.admissionregistration.k8s.io/safe-upgrades.gateway.networking.k8s.io
  elif [[ $1 == wait ]]; then
    [[ $TEST_GATEWAY_FAILURE != wait ]] || return 8
    if [[ $TEST_GATEWAY_FAILURE == wait-last && $2 == */tlsroutes.gateway.networking.k8s.io ]]; then
      return 9
    fi
  fi
}
install_gateway_api
printf 'CILIUM_INSTALL\n' >> "$TEST_GATEWAY_LOG"
'''
            env = {**os.environ, "TMPDIR": tmp, "TEST_REPO": str(ROOT), "TEST_GATEWAY_LOG": str(log),
                   "TEST_GATEWAY_FAILURE": failure, "GATEWAY_API_VERSION": "v1.6.1",
                   "TEST_GATEWAY_CRDS": "\n".join(
                       f"customresourcedefinition.apiextensions.k8s.io/{name}.gateway.networking.k8s.io"
                       for name in CRDS),
                   "GATEWAY_API_SHA256": checksum or hashlib.sha256(FIXTURE).hexdigest()}
            result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
            return result, log.read_text().splitlines() if log.exists() else []

    def test_defaults_pin_gitops_experimental_bundle_and_published_digest(self):
        settings = json.loads((ROOT / "config/cluster.json").read_text())
        self.assertEqual(
            f"https://github.com/kubernetes-sigs/gateway-api/releases/download/{settings['gateway_api_version']}/experimental-install.yaml",
            GITOPS_BUNDLE_URL)
        self.assertEqual(settings["gateway_api_sha256"], EXPERIMENTAL_SHA256)

    def test_all_crds_established_before_cilium_and_admission_policy_is_not_waited_on(self):
        result, calls = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([line for line in calls if line.startswith("DOWNLOAD ")],
                         ["DOWNLOAD " + GITOPS_BUNDLE_URL])
        self.assertFalse(any("standard-install.yaml" in line for line in calls))
        applies = [line for line in calls if line.startswith("KUBE apply ")]
        self.assertEqual(len(applies), 1)
        self.assertIn("apply --server-side --field-manager=kustomize-controller", calls[1])
        waits = [line for line in calls if line.startswith("KUBE wait")]
        self.assertEqual(waits, [
            f"KUBE wait customresourcedefinition.apiextensions.k8s.io/{name}.gateway.networking.k8s.io "
            "--for=condition=Established --timeout=120s" for name in CRDS])
        self.assertFalse(any("validatingadmissionpolicy" in line for line in waits))
        self.assertEqual(calls[-1], "CILIUM_INSTALL")

    def test_bad_checksum_stops_before_cluster_changes(self):
        result, calls = self.invoke(checksum="0" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Gateway API bundle checksum mismatch", result.stderr)
        self.assertFalse(any(line.startswith("KUBE") or line == "CILIUM_INSTALL" for line in calls))

    def test_failed_download_stops_before_cluster_changes(self):
        result, calls = self.invoke(failure="download")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, ["DOWNLOAD " + GITOPS_BUNDLE_URL])

    def test_failed_apply_or_establishment_prevents_cilium_installation(self):
        for failure in ("apply", "wait", "wait-last"):
            with self.subTest(failure=failure):
                result, calls = self.invoke(failure=failure)
                self.assertNotEqual(result.returncode, 0)
                verb = "wait" if failure == "wait-last" else failure
                self.assertTrue(any(line.startswith("KUBE " + verb) for line in calls), result.stderr)
                self.assertNotIn("CILIUM_INSTALL", calls)
                if failure == "wait-last":
                    self.assertIn("/tlsroutes.gateway.networking.k8s.io", calls[-1])


if __name__ == "__main__":
    unittest.main()
