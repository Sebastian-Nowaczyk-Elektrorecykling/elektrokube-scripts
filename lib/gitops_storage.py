#!/usr/bin/env python3
"""Register storage with existing Flux; never install or upgrade Flux itself."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

import yaml

# Shared read/apply/reconcile helpers only; gitops.handoff() is never called.
import gitops as flux

GIT_URL = "https://github.com/Sebastian-Nowaczyk-Elektrorecykling/elektrokube-storage.git"
BRANCH = "main"
NAME = "elektrokube-storage"
CHILDREN = ("storage-namespaces", "storage-sources", "storage-longhorn",
            "storage-classes", "storage-cnpg", "storage-cnpg-policy", "storage-garage")
RELEASES = (("longhorn", "longhorn-system", "storage-longhorn", "daemonset", "longhorn-manager"),
            ("cloudnative-pg", "cnpg-system", "storage-cnpg", "deployment", "cnpg-controller-manager"),
            ("garage", "garage", "storage-garage", "statefulset", "garage"))
CLASSES = ("longhorn", "longhorn-cnpg", "longhorn-garage", "longhorn-replicated")


def check_source(obj):
    if obj:
        spec = obj["spec"]
        flux.require(spec.get("url") == GIT_URL and spec.get("ref") == {"branch": BRANCH},
                     f"GitRepository/{NAME} points elsewhere; refusing to replace it.")
        flux.require(not spec.get("suspend"), f"GitRepository/{NAME} is suspended.")


def check_sync(obj, desired):
    if obj:
        spec, expected = obj["spec"], desired["spec"]
        source = spec.get("sourceRef", {})
        name = desired["metadata"]["name"]
        flux.require(spec.get("path") == expected["path"] and
                     source.get("kind") == "GitRepository" and source.get("name") == NAME and
                     source.get("namespace", "flux-system") == "flux-system",
                     f"Kustomization/{name} belongs to a different reconciliation graph.")
        flux.require(not spec.get("suspend"), f"Kustomization/{name} is suspended.")


def check_owner(obj, owner):
    if obj:
        labels = obj["metadata"].get("labels", {})
        flux.require(labels.get("kustomize.toolkit.fluxcd.io/name") == owner and
                     labels.get("kustomize.toolkit.fluxcd.io/namespace") == "flux-system",
                     f"{obj['kind']}/{obj['metadata']['name']} is not owned by this storage graph; migrate it explicitly.")
        flux.require(not obj.get("spec", {}).get("suspend"),
                     f"{obj['kind']}/{obj['metadata']['name']} is suspended.")


def preflight():
    crds = set(flux.kube("get", "crds", "-o", "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}").splitlines())
    required = {flux.SOURCE, flux.SYNC, flux.RELEASE, "helmrepositories.source.toolkit.fluxcd.io"}
    flux.require(required <= crds, "Flux CRDs are missing. Run gitops-cilium-and-flux.sh first.")
    for name in ("source-controller", "kustomize-controller", "helm-controller"):
        flux.require(flux.get("deployment", name), f"Flux {name} is missing; install Flux first.")
        flux.kube("-n", "flux-system", "rollout", "status", f"deployment/{name}", "--timeout=300s")
    cilium = flux.get(flux.SYNC, "cilium")
    flux.require(cilium and not cilium["spec"].get("suspend"),
                 "The base cilium Kustomization is missing or suspended; complete the Cilium/Flux handoff first.")
    flux.wait_for(flux.SYNC, "cilium", "flux-system", lambda obj: bool(obj) and
                  obj.get("status", {}).get("observedGeneration") == obj["metadata"].get("generation") and
                  any(c["type"] == "Ready" and c["status"] == "True"
                      for c in obj.get("status", {}).get("conditions", [])), timeout=300)
    api = json.loads(flux.kube("get", "--raw=/apis/admissionregistration.k8s.io/v1"))
    flux.require({"mutatingadmissionpolicies", "mutatingadmissionpolicybindings"} <=
                 {r["name"] for r in api["resources"]},
                 "The stable MutatingAdmissionPolicy API is unavailable; Kubernetes >= 1.36 is required.")
    classes = json.loads(flux.kube("get", "storageclasses", "-o", "json"))["items"]
    for sc in classes:
        meta = sc["metadata"]
        annotations = meta.get("annotations", {})
        is_default = any(annotations.get(key, "").lower() == "true" for key in (
            "storageclass.kubernetes.io/is-default-class", "storageclass.beta.kubernetes.io/is-default-class"))
        flux.require(not is_default or meta["name"] == "longhorn",
                     f"Another default StorageClass exists: {meta['name']}. Remove its default annotation deliberately first.")
        if meta["name"] in CLASSES:
            check_owner(sc, "storage-classes")
    for name, namespace, owner, kind, workload in RELEASES:
        release = flux.get(flux.RELEASE, name, namespace)
        check_owner(release, owner)
        if not release:
            flux.require(not flux.get(kind, workload, namespace),
                         f"Existing {namespace}/{workload} has no managed HelmRelease; migrate it explicitly.")
    for resource, name in (("helmrepositories.source.toolkit.fluxcd.io", "storage-longhorn"),
                           ("helmrepositories.source.toolkit.fluxcd.io", "storage-cnpg"),
                           (flux.SOURCE, "storage-garage-chart")):
        check_owner(flux.get(resource, name), "storage-sources")
    for resource in ("mutatingadmissionpolicies", "mutatingadmissionpolicybindings"):
        check_owner(flux.get(f"{resource}.admissionregistration.k8s.io", "cnpg-default-storage-class", ""),
                    "storage-cnpg-policy")


def register(workdir):
    preflight()
    checkout = workdir / "storage"
    flux.log("Fetching elektrokube-storage/main (read-only).")
    flux.run("git", "clone", "--quiet", "--depth=1", "--single-branch", "--branch", BRANCH, GIT_URL, str(checkout))
    commit = flux.run("git", "-C", str(checkout), "rev-parse", "HEAD")
    seed = flux.documents(flux.kube("kustomize", str(checkout / "bootstrap")))
    graph = flux.documents(flux.kube("kustomize", str(checkout / "clusters/elektrokube")))
    desired_source = flux.one(seed, "GitRepository", NAME)
    desired_root = flux.one(seed, "Kustomization", NAME)
    check_source(desired_source)
    flux.require(desired_root["spec"]["path"] == "./clusters/elektrokube" and
                 desired_root["spec"]["sourceRef"] == {"kind": "GitRepository", "name": NAME},
                 "Unexpected storage root path or source.")
    flux.require(len(seed) == 2 and all(obj["metadata"].get("namespace") == "flux-system" for obj in seed),
                 "Storage bootstrap must contain only its source and root Kustomization in flux-system.")
    syncs = {obj["metadata"]["name"]: obj for obj in graph if obj["kind"] == "Kustomization"}
    flux.require(set(syncs) == {NAME, *CHILDREN}, "Unexpected storage reconciliation graph.")
    source = flux.get(flux.SOURCE, NAME)
    check_source(source)
    existing = {name: flux.get(flux.SYNC, name) for name in syncs}
    for name, obj in existing.items():
        check_sync(obj, syncs[name])
    remote = flux.run("git", "ls-remote", "--exit-code", GIT_URL, f"refs/heads/{BRANCH}").split()[0]
    flux.require(remote == commit, "Storage main changed during preflight; rerun before registering it.")
    missing = ([desired_source] if not source else []) + ([desired_root] if not existing[NAME] else [])
    if missing:
        seed_file = workdir / "storage-seed.yaml"
        seed_file.write_text(yaml.safe_dump_all(missing, sort_keys=False))
        flux.log("Adding the storage source and root Kustomization to existing Flux.")
        flux.kube("apply", "--server-side", "--field-manager=kustomize-controller", "-f", str(seed_file))
    source = flux.reconcile(flux.SOURCE, NAME)
    revision = source["status"]["artifact"]["revision"]
    flux.require(revision.rsplit(":", 1)[-1] == commit,
                 "Storage main changed during handoff; rerun to verify the new revision.")
    for name in (NAME, *CHILDREN):
        flux.log(f"Waiting for {name} at {revision}.")
        flux.reconcile(flux.SYNC, name, revision=revision)
    flux.log(flux.kube("get", "storageclasses"))
    flux.log(f"Longhorn, CloudNativePG, its defaulting policy and Garage reconciled at {revision}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", required=True, type=Path)
    args = parser.parse_args()
    try:
        register(args.workdir)
    except (ValueError, RuntimeError, OSError, KeyError, yaml.YAMLError, subprocess.CalledProcessError) as exc:
        print(f"[elektrokube] ERROR: {exc}", file=sys.stderr)
        print("Fix the error and rerun gitops-storage.sh. No uninstall or rollback was attempted.\n"
              "Inspect: kubectl -n flux-system get gitrepositories,kustomizations\n"
              "         kubectl get helmreleases -A", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
