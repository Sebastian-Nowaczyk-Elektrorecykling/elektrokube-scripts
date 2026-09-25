"""Exercise the handoff against a fake cluster; no network or host changes."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import config
import gitops


def obj(kind, name, spec=None, namespace="flux-system"):
    return {"apiVersion": "v1", "kind": kind,
            "metadata": {"name": name, "namespace": namespace, "generation": 1},
            "spec": spec or {}}


class Cluster:
    def __init__(self, settings):
        self.config = settings
        self.commit = "a" * 40
        self.remote_commit = self.commit
        self.revision = "main@sha1:" + self.commit
        self.calls = []
        self.objects = {}
        self.crds = set()
        self.source = obj("GitRepository", "flux-system", {"url": gitops.GIT_URL, "ref": {"branch": "main"}})
        self.syncs = {name: obj("Kustomization", name, {
            "path": "./clusters/elektrokube" if name == "flux-system" else f"./infrastructure/{name}",
            "sourceRef": {"kind": "GitRepository", "name": "flux-system"}})
            for name in ("flux-system", "flux", "cilium")}
        self.flux = [obj("Deployment", name) for name in gitops.CONTROLLERS]
        self.flux += [obj("CustomResourceDefinition", name) for name in (gitops.SOURCE, gitops.SYNC, gitops.RELEASE)]
        self.release = obj("HelmRelease", "cilium", {
            "releaseName": "cilium", "targetNamespace": "kube-system", "storageNamespace": "kube-system",
            "chart": {"spec": {"version": settings["cilium_version"]}}}, "kube-system")
        self.values = config.cilium_values(settings)
        self.live_values = copy.deepcopy(self.values)
        self.chart = "cilium-" + settings["cilium_version"]
        self.helm_status = "deployed"
        self.fail_apply = False

    def put(self, resource, name, value, namespace="flux-system"):
        self.objects[(resource, namespace, name)] = copy.deepcopy(value)

    def run(self, *args):
        self.calls.append(args)
        if args[:2] == ("git", "clone"):
            return ""
        if args[0] == "git" and "rev-parse" in args:
            return self.commit
        if args[:2] == ("git", "ls-remote"):
            return self.remote_commit + "\trefs/heads/main"
        if args[:2] == ("helm", "list"):
            return json.dumps([{"chart": self.chart, "status": self.helm_status}])
        if args[:3] == ("helm", "get", "values"):
            return json.dumps(self.live_values)
        raise AssertionError(args)

    def kube(self, *args):
        self.calls.append(args)
        if args[0] == "kustomize":
            path = args[1]
            if path.endswith("infrastructure/flux"):
                return yaml.safe_dump_all(self.flux)
            if path.endswith("infrastructure/cilium"):
                cm = obj("ConfigMap", "cilium-values", namespace="kube-system")
                cm["data"] = {"values.yaml": yaml.safe_dump(self.values)}
                return yaml.safe_dump_all([self.release, cm])
            return yaml.safe_dump_all([self.source, self.syncs["flux-system"]])
        if args[:2] == ("get", "crds"):
            return "\n".join(self.crds)
        if args[0] == "wait" or "rollout" in args:
            return ""
        if args[0] == "apply":
            if self.fail_apply:
                raise subprocess.CalledProcessError(1, args)
            if "-f" in args:
                data = json.loads(Path(args[-1]).read_text())
                self.put("configmap", "cluster-settings", data)
            elif args[-1].endswith("infrastructure/flux"):
                for d in self.flux:
                    if d["kind"] == "Deployment":
                        self.put("deployment", d["metadata"]["name"], d)
                    else:
                        self.crds.add(d["metadata"]["name"])
            else:
                self.put(gitops.SOURCE, "flux-system", self.source)
                self.put(gitops.SYNC, "flux-system", self.syncs["flux-system"])
            return ""
        if args[0] == "-n":
            _, ns, verb, resource, name, *_ = args
            key = (resource, ns, name)
            if verb == "get":
                value = self.objects.get(key)
                return json.dumps(value) if value else ""
            if verb == "annotate":
                item = self.objects[key]
                item["status"] = {
                    "observedGeneration": item["metadata"]["generation"],
                    "lastHandledReconcileAt": args[-1].split("=", 1)[1],
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "artifact": {"revision": self.revision}, "lastAppliedRevision": self.revision}
                if resource == gitops.SYNC and name == "flux-system":
                    for child in ("flux", "cilium"):
                        self.objects.setdefault((gitops.SYNC, ns, child), copy.deepcopy(self.syncs[child]))
                if resource == gitops.SYNC and name == "flux":
                    for controller in gitops.CONTROLLERS:
                        self.objects[("deployment", ns, controller)]["metadata"]["labels"] = {
                            "kustomize.toolkit.fluxcd.io/name": "flux",
                            "kustomize.toolkit.fluxcd.io/namespace": "flux-system"}
                if resource == gitops.SYNC and name == "cilium":
                    self.release["metadata"]["labels"] = dict(gitops.OWNER_LABELS)
                    self.put(gitops.RELEASE, "cilium", self.release, "kube-system")
                return ""
        raise AssertionError(args)

    def applies(self):
        return [call for call in self.calls if call[0] == "apply"]


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.settings = json.loads((ROOT / "config/cluster.json").read_text())
        self.settings["api_address"] = "192.168.2.153"
        self.cluster = Cluster(self.settings)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)

    def handoff(self):
        with patch.object(gitops, "kube", self.cluster.kube), patch.object(gitops, "run", self.cluster.run), \
                patch.object(gitops, "log"):
            gitops.handoff(self.work, self.settings)

    def test_first_handoff_orders_raw_install_settings_seed_and_fresh_reconciliation(self):
        self.handoff()
        applies = self.cluster.applies()
        self.assertEqual(len(applies), 3)
        self.assertTrue(applies[0][-1].endswith("infrastructure/flux"))
        self.assertTrue(applies[1][-1].endswith("cluster-settings.json"))
        self.assertTrue(applies[2][-1].endswith("clusters/elektrokube/flux-system"))
        cm = self.cluster.objects[("configmap", "flux-system", "cluster-settings")]
        self.assertEqual(cm["data"], {"API_IP": "192.168.2.153", "CLUSTER_NAME": "elektrokube"})
        reconciles = [call[4] for call in self.cluster.calls if "annotate" in call]
        self.assertEqual(reconciles, ["flux-system", "flux-system", "flux", "cilium", "cilium"])
        self.assertTrue(all(c[1] in ("list", "get") for c in self.cluster.calls if c[0] == "helm"))

    def test_refuses_changed_live_values_before_cluster_writes(self):
        self.cluster.live_values["kubeProxyReplacement"] = False
        with self.assertRaisesRegex(ValueError, "Live Cilium"):
            self.handoff()
        self.assertEqual(self.cluster.applies(), [])

    def test_refuses_wrong_chart_or_failed_release_before_cluster_writes(self):
        for field, value in (("chart", "cilium-1.19.0"), ("helm_status", "failed")):
            with self.subTest(field=field):
                self.cluster = Cluster(self.settings)
                setattr(self.cluster, field, value)
                with self.assertRaisesRegex(ValueError, "already deployed matching"):
                    self.handoff()
                self.assertEqual(self.cluster.applies(), [])

    def test_refuses_foreign_source_and_foreign_helmrelease(self):
        self.cluster.crds.add(gitops.SOURCE)
        source = copy.deepcopy(self.cluster.source)
        source["spec"]["url"] = "https://example.org/another.git"
        self.cluster.put(gitops.SOURCE, "flux-system", source)
        with self.assertRaisesRegex(ValueError, "points elsewhere"):
            self.handoff()
        self.assertEqual(self.cluster.applies(), [])
        self.cluster = Cluster(self.settings)
        self.cluster.crds.add(gitops.RELEASE)
        self.cluster.put(gitops.RELEASE, "cilium", self.cluster.release, "kube-system")
        with self.assertRaisesRegex(ValueError, "not owned"):
            self.handoff()
        self.assertEqual(self.cluster.applies(), [])

    def test_refuses_helm_managed_flux(self):
        deployment = copy.deepcopy(self.cluster.flux[0])
        deployment["metadata"]["annotations"] = {"meta.helm.sh/release-name": "flux"}
        self.cluster.put("deployment", "source-controller", deployment)
        with self.assertRaisesRegex(ValueError, "Helm or Flux Operator"):
            self.handoff()
        self.assertEqual(self.cluster.applies(), [])

    def test_refuses_conflicting_settings_and_moving_git_head(self):
        self.cluster.put("configmap", "cluster-settings", {"data": {"API_IP": "192.168.2.99"}})
        with self.assertRaisesRegex(ValueError, "cluster-settings differs"):
            self.handoff()
        self.assertEqual(self.cluster.applies(), [])
        self.cluster = Cluster(self.settings)
        self.cluster.remote_commit = "b" * 40
        with self.assertRaisesRegex(ValueError, "main changed during preflight"):
            self.handoff()
        self.assertEqual(self.cluster.applies(), [])

    def test_completed_rerun_preserves_git_upgrades_and_does_not_reapply_installation(self):
        self.handoff()
        self.cluster.calls.clear()
        self.cluster.release["spec"]["chart"]["spec"]["version"] = "1.20.3"
        self.cluster.values["operator"]["replicas"] = 2
        self.cluster.live_values["operator"]["replicas"] = 2
        self.handoff()
        self.assertEqual(self.cluster.applies(), [])
        self.assertFalse(any(c[0] == "helm" for c in self.cluster.calls))

    def test_failed_apply_does_not_seed_reconciliation_or_run_a_rollback(self):
        self.cluster.fail_apply = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.handoff()
        self.assertEqual(len(self.cluster.applies()), 1)
        self.assertFalse(any("delete" in c or "uninstall" in c or "rollback" in c for c in self.cluster.calls))

    def test_suspended_existing_graph_is_not_resumed(self):
        self.handoff()
        self.cluster.calls.clear()
        self.cluster.objects[(gitops.SYNC, "flux-system", "cilium")]["spec"]["suspend"] = True
        with self.assertRaisesRegex(ValueError, "suspended"):
            self.handoff()
        self.assertEqual(self.cluster.applies(), [])


class ReadinessTests(unittest.TestCase):
    def test_old_ready_generation_or_request_cannot_report_success(self):
        current = {"metadata": {"generation": 2}, "status": {
            "observedGeneration": 2, "lastHandledReconcileAt": "now",
            "conditions": [{"type": "Ready", "status": "True"}]}}
        self.assertTrue(gitops.fresh_ready(current, "now"))
        self.assertFalse(gitops.fresh_ready(current, "old"))
        current["status"]["observedGeneration"] = 1
        self.assertFalse(gitops.fresh_ready(current, "now"))
        current["status"]["observedGeneration"] = 2
        current["status"]["conditions"].append({"type": "Reconciling", "status": "True"})
        self.assertFalse(gitops.fresh_ready(current, "now"))

    def test_wait_handles_child_not_created_yet_and_times_out(self):
        ready = {"metadata": {"name": "child"}}
        with patch.object(gitops, "get", side_effect=[None, ready]), patch.object(gitops.time, "sleep"):
            self.assertEqual(gitops.wait_for(gitops.SYNC, "child", "flux-system", bool), ready)
        with patch.object(gitops, "get", return_value=None), patch.object(gitops.time, "sleep"), \
                patch.object(gitops.time, "monotonic", side_effect=[0, 0, 2]):
            with self.assertRaisesRegex(RuntimeError, "not created"):
                gitops.wait_for(gitops.SYNC, "child", "flux-system", bool, timeout=1)


if __name__ == "__main__":
    unittest.main()
