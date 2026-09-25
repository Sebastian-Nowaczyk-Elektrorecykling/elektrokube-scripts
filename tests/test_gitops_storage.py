"""Storage registration against a fake existing Flux cluster; no host changes."""
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
import gitops_storage as storage


def obj(kind, name, spec=None, namespace="flux-system", owner=None):
    value = {"apiVersion": "v1", "kind": kind, "metadata": {"name": name, "namespace": namespace, "generation": 1},
             "spec": spec or {}}
    if owner:
        value["metadata"]["labels"] = {"kustomize.toolkit.fluxcd.io/name": owner,
                                       "kustomize.toolkit.fluxcd.io/namespace": "flux-system"}
    return value


class Cluster:
    def __init__(self):
        self.commit = "a" * 40
        self.remote = self.commit
        self.revision = "main@sha1:" + self.commit
        self.calls = []
        self.applies = []
        self.objects = {}
        self.crds = {storage.flux.SOURCE, storage.flux.SYNC, storage.flux.RELEASE,
                     "helmrepositories.source.toolkit.fluxcd.io"}
        self.api = ["mutatingadmissionpolicies", "mutatingadmissionpolicybindings"]
        self.classes = []
        self.source = obj("GitRepository", storage.NAME, {"url": storage.GIT_URL, "ref": {"branch": "main"}})
        self.syncs = {name: obj("Kustomization", name, {
            "path": "./clusters/elektrokube" if name == storage.NAME else "./infrastructure/" +
                    {"storage-classes": "storage-classes"}.get(name, name.removeprefix("storage-")),
            "sourceRef": {"kind": "GitRepository", "name": storage.NAME}})
            for name in (storage.NAME, *storage.CHILDREN)}
        for name in ("source-controller", "kustomize-controller", "helm-controller"):
            self.objects[("deployment", name, "flux-system")] = obj("Deployment", name)
        cilium = obj("Kustomization", "cilium")
        cilium["status"] = {"observedGeneration": 1, "conditions": [{"type": "Ready", "status": "True"}]}
        self.objects[(storage.flux.SYNC, "cilium", "flux-system")] = cilium

    def get(self, resource, name, namespace="flux-system"):
        return copy.deepcopy(self.objects.get((resource, name, namespace)))

    def run(self, *args):
        self.calls.append(args)
        if args[:2] == ("git", "clone"):
            return ""
        if args[0] == "git" and "rev-parse" in args:
            return self.commit
        if args[:2] == ("git", "ls-remote"):
            return self.remote + "\trefs/heads/main"
        raise AssertionError(args)

    def kube(self, *args):
        self.calls.append(args)
        if args[:2] == ("get", "crds"):
            return "\n".join(self.crds)
        if args[0] == "get" and args[1].startswith("--raw="):
            return json.dumps({"resources": [{"name": name} for name in self.api]})
        if args[:2] == ("get", "storageclasses"):
            return json.dumps({"items": self.classes})
        if "rollout" in args:
            return ""
        if args[0] == "kustomize":
            manifests = [self.source, self.syncs[storage.NAME]] if args[-1].endswith("bootstrap") else [self.source, *self.syncs.values()]
            return yaml.safe_dump_all(manifests)
        if args[0] == "apply":
            manifests = list(yaml.safe_load_all(Path(args[-1]).read_text()))
            self.applies.append(manifests)
            for item in manifests:
                resource = storage.flux.SOURCE if item["kind"] == "GitRepository" else storage.flux.SYNC
                self.objects[(resource, item["metadata"]["name"], "flux-system")] = copy.deepcopy(item)
            return ""
        raise AssertionError(args)

    def reconcile(self, resource, name, namespace="flux-system", revision=None):
        self.calls.append(("reconcile", resource, name, revision))
        if resource == storage.flux.SOURCE:
            return {"status": {"artifact": {"revision": self.revision}}}
        if name == storage.NAME:
            for child in storage.CHILDREN:
                self.objects[(storage.flux.SYNC, child, "flux-system")] = copy.deepcopy(self.syncs[child])
        if revision != self.revision:
            raise ValueError("Unexpected revision")
        return {}


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.cluster = Cluster()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)

    def register(self):
        with patch.object(storage.flux, "get", self.cluster.get), \
                patch.object(storage.flux, "run", self.cluster.run), \
                patch.object(storage.flux, "kube", self.cluster.kube), \
                patch.object(storage.flux, "reconcile", self.cluster.reconcile), patch.object(storage.flux, "log"):
            storage.register(self.work)

    def test_seed_only_storage_then_wait_for_the_fetched_revision(self):
        self.register()
        self.assertEqual(len(self.cluster.applies), 1)
        self.assertEqual({(o["kind"], o["metadata"]["name"]) for o in self.cluster.applies[0]},
                         {("GitRepository", storage.NAME), ("Kustomization", storage.NAME)})
        reconciles = [c for c in self.cluster.calls if c[0] == "reconcile"]
        self.assertEqual([c[2] for c in reconciles], [storage.NAME, storage.NAME, *storage.CHILDREN])
        self.assertTrue(all(c[3] == self.cluster.revision for c in reconciles[1:]))
        self.assertFalse(any(c[0] in ("helm", "flux") or "infrastructure/flux" in str(c) for c in self.cluster.calls))

    def test_rerun_does_not_reapply_or_reset_existing_resources(self):
        self.register()
        self.cluster.applies.clear()
        self.cluster.objects[(storage.flux.SOURCE, storage.NAME, "flux-system")]["spec"]["interval"] = "2m"
        self.register()
        self.assertEqual(self.cluster.applies, [])
        self.assertEqual(self.cluster.get(storage.flux.SOURCE, storage.NAME)["spec"]["interval"], "2m")

    def test_partial_seed_only_creates_missing_root(self):
        self.cluster.objects[(storage.flux.SOURCE, storage.NAME, "flux-system")] = self.cluster.source
        self.register()
        self.assertEqual([d["kind"] for d in self.cluster.applies[0]], ["Kustomization"])

    def test_missing_flux_or_mutation_api_stops_before_writes(self):
        for field in ("crds", "api"):
            with self.subTest(field=field):
                self.cluster = Cluster()
                setattr(self.cluster, field, [])
                with self.assertRaisesRegex(ValueError, "Flux CRDs|MutatingAdmissionPolicy"):
                    self.register()
                self.assertEqual(self.cluster.applies, [])

    def test_other_default_or_unmanaged_longhorn_is_not_adopted(self):
        self.cluster.classes = [obj("StorageClass", "local-path")]
        self.cluster.classes[0]["metadata"]["annotations"] = {"storageclass.kubernetes.io/is-default-class": "true"}
        with self.assertRaisesRegex(ValueError, "Another default"):
            self.register()
        self.cluster.classes = [obj("StorageClass", "longhorn")]
        with self.assertRaisesRegex(ValueError, "not owned"):
            self.register()
        self.assertEqual(self.cluster.applies, [])

    def test_foreign_source_child_or_suspended_child_stops_before_writes(self):
        cases = ((storage.flux.SOURCE, storage.NAME, {"url": "https://example.org/other.git"}, "points elsewhere"),
                 (storage.flux.SYNC, "storage-longhorn", {"sourceRef": {"name": "storage-addons"}}, "different"),
                 (storage.flux.SYNC, "storage-cnpg", {"suspend": True}, "suspended"))
        for resource, name, changes, message in cases:
            with self.subTest(name=name):
                self.cluster = Cluster()
                item = copy.deepcopy(self.cluster.source if resource == storage.flux.SOURCE else self.cluster.syncs[name])
                item["spec"].update(changes)
                self.cluster.objects[(resource, name, "flux-system")] = item
                with self.assertRaisesRegex(ValueError, message):
                    self.register()
                self.assertEqual(self.cluster.applies, [])

    def test_existing_operator_without_managed_release_stops(self):
        self.cluster.objects[("statefulset", "garage", "garage")] = obj("StatefulSet", "garage", namespace="garage")
        with self.assertRaisesRegex(ValueError, "no managed HelmRelease"):
            self.register()
        self.assertEqual(self.cluster.applies, [])

    def test_remote_change_does_not_seed_or_claim_success(self):
        self.cluster.remote = "b" * 40
        with self.assertRaisesRegex(ValueError, "changed during preflight"):
            self.register()
        self.assertEqual(self.cluster.applies, [])
        self.cluster.remote = self.cluster.commit
        self.cluster.revision = "main@sha1:" + "b" * 40
        with self.assertRaisesRegex(ValueError, "changed during handoff"):
            self.register()


if __name__ == "__main__":
    unittest.main()
