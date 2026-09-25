#!/usr/bin/env python3
"""Raw Flux installation and adoption of the existing elektrokube Cilium release."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

import yaml

from config import cilium_values, load

GIT_URL = "https://github.com/Sebastian-Nowaczyk-Elektrorecykling/elektrokube-cilium-and-flux.git"
BRANCH = "main"
KUBECONFIG = "/etc/rancher/k3s/k3s.yaml"
CONTROLLERS = ("source-controller", "kustomize-controller", "helm-controller", "notification-controller")
KUSTOMIZATIONS = ("flux-system", "flux", "gateway-api", "cilium")
SOURCE = "gitrepositories.source.toolkit.fluxcd.io"
SYNC = "kustomizations.kustomize.toolkit.fluxcd.io"
RELEASE = "helmreleases.helm.toolkit.fluxcd.io"
OWNER_LABELS = {"kustomize.toolkit.fluxcd.io/name": "cilium",
                "kustomize.toolkit.fluxcd.io/namespace": "flux-system"}


def log(message):
    print(f"[elektrokube] {message}", flush=True)


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def kube(*args):
    return run("/usr/local/bin/k3s", "kubectl", "--kubeconfig", KUBECONFIG,
               "--request-timeout=30s", *args)


def get(kind, name, namespace="flux-system"):
    text = kube("-n", namespace, "get", kind, name, "--ignore-not-found", "-o", "json")
    return json.loads(text) if text else None


def require(condition, message):
    if not condition:
        raise ValueError(message)


def documents(text):
    return [obj for obj in yaml.safe_load_all(text) if obj]


def one(objects, kind, name):
    matches = [obj for obj in objects if obj["kind"] == kind and obj["metadata"]["name"] == name]
    require(len(matches) == 1, f"Expected exactly one {kind}/{name} in the GitOps manifests.")
    return matches[0]


def check_source(obj):
    if obj:
        spec = obj["spec"]
        require(spec.get("url") == GIT_URL and spec.get("ref") == {"branch": BRANCH},
                "GitRepository/flux-system points elsewhere; refusing to replace another GitOps source.")
        require(not spec.get("suspend"), "GitRepository/flux-system is suspended; resume it deliberately first.")


def check_sync(obj, name):
    if obj:
        spec = obj["spec"]
        path = "./clusters/elektrokube" if name == "flux-system" else f"./infrastructure/{name}"
        source = spec.get("sourceRef", {})
        require(spec.get("path") == path and source.get("kind") == "GitRepository" and
                source.get("name") == "flux-system" and source.get("namespace", "flux-system") == "flux-system",
                f"Kustomization/{name} belongs to a different reconciliation graph.")
        require(not spec.get("suspend"), f"Kustomization/{name} is suspended; resume it deliberately first.")


def check_release(obj):
    spec = obj["spec"]
    require(obj["metadata"].get("namespace") == "kube-system" and
            spec.get("releaseName") == "cilium" and spec.get("targetNamespace") == "kube-system" and
            spec.get("storageNamespace") == "kube-system", "Cilium Helm release identity does not match bootstrap.")
    require(not spec.get("suspend"), "HelmRelease/cilium is suspended; resume it deliberately first.")


def check_controller(obj):
    if obj:
        meta = obj["metadata"]
        annotations = meta.get("annotations", {})
        labels = meta.get("labels", {})
        owners = meta.get("ownerReferences", [])
        require(labels.get("app.kubernetes.io/managed-by") != "Helm" and
                "meta.helm.sh/release-name" not in annotations and
                not any(ref.get("kind") == "FluxInstance" for ref in owners) and
                not any("flux-operator" in field.get("manager", "") for field in meta.get("managedFields", [])),
                "Flux is managed by Helm or Flux Operator; an explicit migration is required.")


def fresh_ready(obj, request):
    if not obj:
        return False
    status = obj.get("status", {})
    conditions = {c["type"]: c["status"] for c in status.get("conditions", [])}
    return (status.get("observedGeneration") == obj["metadata"].get("generation") and
            status.get("lastHandledReconcileAt") == request and conditions.get("Ready") == "True" and
            conditions.get("Reconciling") != "True" and conditions.get("Stalled") != "True")


def wait_for(kind, name, namespace, predicate, timeout=1500):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = get(kind, name, namespace)
        if predicate(last):
            return last
        time.sleep(3)
    conditions = last.get("status", {}).get("conditions", []) if last else "not created"
    raise RuntimeError(f"Timed out waiting for {namespace}/{name}: {conditions}")


def reconcile(kind, name, namespace="flux-system", revision=None):
    wait_for(kind, name, namespace, bool, timeout=300)
    request = str(time.time_ns())
    kube("-n", namespace, "annotate", kind, name, "--overwrite", "--field-manager=flux-client-side-apply",
         f"reconcile.fluxcd.io/requestedAt={request}")
    obj = wait_for(kind, name, namespace, lambda item: fresh_ready(item, request))
    if revision is not None:
        actual = obj.get("status", {}).get("lastAppliedRevision")
        require(actual == revision, f"{name} reconciled another revision; main changed during handoff. Rerun the script.")
    return obj


def handoff(workdir, config):
    checkout = workdir / "gitops"
    log("Fetching the GitOps repository (read-only).")
    run("git", "clone", "--quiet", "--depth=1", "--single-branch", "--branch", BRANCH, GIT_URL, str(checkout))
    commit = run("git", "-C", str(checkout), "rev-parse", "HEAD")
    log(f"Preparing GitOps revision {commit}.")
    flux_dir = checkout / "infrastructure/flux"
    cilium_dir = checkout / "infrastructure/cilium"
    seed_dir = checkout / "clusters/elektrokube/flux-system"
    flux_objects = documents(kube("kustomize", str(flux_dir)))
    cilium_objects = documents(kube("kustomize", str(cilium_dir)))
    seed_objects = documents(kube("kustomize", str(seed_dir)))
    check_source(one(seed_objects, "GitRepository", "flux-system"))
    check_sync(one(seed_objects, "Kustomization", "flux-system"), "flux-system")
    desired_release = one(cilium_objects, "HelmRelease", "cilium")
    check_release(desired_release)
    require({d["metadata"]["name"] for d in flux_objects if d["kind"] == "Deployment"} == set(CONTROLLERS),
            "The GitOps repository must contain the four standard Flux controllers.")

    crds = set(kube("get", "crds", "-o", "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}").splitlines())
    source = get(SOURCE, "flux-system") if SOURCE in crds else None
    syncs = {name: get(SYNC, name) if SYNC in crds else None for name in KUSTOMIZATIONS}
    check_source(source)
    for name, obj in syncs.items():
        check_sync(obj, name)
    if "fluxinstances.fluxcd.controlplane.io" in crds:
        instances = json.loads(kube("-n", "flux-system", "get", "fluxinstances.fluxcd.controlplane.io", "-o", "json"))
        require(not instances.get("items"), "Flux Operator already manages flux-system; refusing a competing install.")
    controllers = [get("deployment", name) for name in CONTROLLERS]
    for obj in controllers:
        check_controller(obj)
        labels = obj["metadata"].get("labels", {}) if obj else {}
        if "kustomize.toolkit.fluxcd.io/name" in labels:
            require(source and syncs["flux-system"] and
                    labels.get("kustomize.toolkit.fluxcd.io/namespace") == "flux-system" and
                    labels["kustomize.toolkit.fluxcd.io/name"] in ("flux", "flux-system"),
                    "Flux controllers belong to another Kustomization; refusing a competing install.")
    release = get(RELEASE, "cilium", "kube-system") if RELEASE in crds else None
    if release:
        check_release(release)
        require(source and all(syncs.values()) and
                all(release["metadata"].get("labels", {}).get(k) == v for k, v in OWNER_LABELS.items()),
                "Existing HelmRelease/cilium is not owned by this GitOps graph; refusing to take it over.")
        log("Cilium is already owned by this GitOps repository; preserving its Git-managed version and values.")
    else:
        desired_text = one(cilium_objects, "ConfigMap", "cilium-values")["data"]["values.yaml"]
        desired_text = desired_text.replace("${API_IP}", config["api_address"]).replace("${CLUSTER_NAME}", config["cluster_name"])
        require("${" not in desired_text, "The Cilium values contain unsupported substitutions.")
        desired_values = yaml.safe_load(desired_text)
        require(desired_values == cilium_values(config), "Git Cilium values differ from the saved bootstrap configuration; review before adoption.")
        version = desired_release["spec"]["chart"]["spec"]["version"]
        require(version == config["cilium_version"], "Git Cilium version differs from the saved bootstrap version.")
        releases = json.loads(run("helm", "list", "--kubeconfig", KUBECONFIG, "-n", "kube-system",
                                  "--filter", "^cilium$", "--all", "-o", "json"))
        require(len(releases) == 1 and releases[0].get("status") == "deployed" and
                releases[0].get("chart") == f"cilium-{version}", "Expected an already deployed matching Cilium Helm release in kube-system.")
        live_values = json.loads(run("helm", "get", "values", "cilium", "--kubeconfig", KUBECONFIG,
                                    "-n", "kube-system", "-o", "json"))
        require(live_values == desired_values, "Live Cilium Helm values differ from Git; refusing to reset them during adoption.")
        kube("-n", "kube-system", "rollout", "status", "daemonset/cilium", "--timeout=300s")

    settings = {"API_IP": config["api_address"], "CLUSTER_NAME": config["cluster_name"]}
    existing_settings = get("configmap", "cluster-settings")
    if existing_settings:
        require(all(existing_settings.get("data", {}).get(k) == v for k, v in settings.items()),
                "Existing cluster-settings differs from the saved cluster configuration; refusing to overwrite it.")
    require(not release or existing_settings, "Managed Cilium is missing cluster-settings; restore its last known settings before rerunning.")
    remote = run("git", "ls-remote", "--exit-code", GIT_URL, f"refs/heads/{BRANCH}").split()[0]
    require(remote == commit, "GitOps main changed during preflight; rerun before installing.")

    managed_flux = source and syncs["flux"] and all(
        obj and obj["metadata"].get("labels", {}).get("kustomize.toolkit.fluxcd.io/name") == "flux" and
        obj["metadata"].get("labels", {}).get("kustomize.toolkit.fluxcd.io/namespace") == "flux-system"
        for obj in controllers)
    if not managed_flux:
        log("Installing the raw Flux manifests from Git.")
        # The same SSA manager is used by the subsequent Flux reconciliation.
        # No force-conflicts: an unrelated resource manager must not be displaced.
        kube("apply", "--server-side", "--field-manager=kustomize-controller", "-k", str(flux_dir))
    else:
        log("Flux already manages its controllers; leaving upgrades to Git reconciliation.")
    for obj in flux_objects:
        if obj["kind"] == "CustomResourceDefinition":
            kube("wait", "--for=condition=Established", f"crd/{obj['metadata']['name']}", "--timeout=120s")
    for name in CONTROLLERS:
        kube("-n", "flux-system", "rollout", "status", f"deployment/{name}", "--timeout=300s")
    if not existing_settings:
        manifest = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {
            "name": "cluster-settings", "namespace": "flux-system",
            "labels": {"reconcile.fluxcd.io/watch": "Enabled"}}, "data": settings}
        settings_file = workdir / "cluster-settings.json"
        settings_file.write_text(json.dumps(manifest))
        kube("apply", "--server-side", "--field-manager=elektrokube-gitops", "-f", str(settings_file))
    if not source or not syncs["flux-system"]:
        log("Adding the Flux GitRepository and root Kustomization.")
        kube("apply", "--server-side", "--field-manager=kustomize-controller", "-k", str(seed_dir))

    log("Waiting for a fresh Git fetch, Flux self-management, and Cilium adoption.")
    source = reconcile(SOURCE, "flux-system")
    revision = source["status"]["artifact"]["revision"]
    require(revision.rsplit(":", 1)[-1] == commit, "GitOps main changed during handoff; rerun to verify the new revision.")
    for name in KUSTOMIZATIONS:
        reconcile(SYNC, name, revision=revision)
    reconcile(RELEASE, "cilium", "kube-system")
    kube("-n", "kube-system", "rollout", "status", "daemonset/cilium", "--timeout=300s")
    kube("wait", "gatewayclass/cilium", "--for=condition=Accepted", "--timeout=300s")
    log(f"Flux now owns Gateway API CRDs, Cilium and its own controllers at {revision}.")
    log("Use Git for future Cilium/Flux changes. Do not rerun bootstrap-cluster.sh to change Cilium.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    try:
        handoff(args.workdir, load(args.config))
    except (ValueError, RuntimeError, OSError, KeyError, yaml.YAMLError, subprocess.CalledProcessError) as exc:
        print(f"[elektrokube] ERROR: {exc}", file=sys.stderr)
        print("No uninstall or rollback was attempted. Fix the error and rerun gitops-cilium-and-flux.sh.\n"
              "Inspect: kubectl -n flux-system get gitrepositories,kustomizations,pods\n"
              "         kubectl -n kube-system get helmrelease cilium", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
