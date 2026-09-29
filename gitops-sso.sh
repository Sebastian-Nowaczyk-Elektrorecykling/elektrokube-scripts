#!/usr/bin/env bash
set -Eeuo pipefail
# shellcheck source=lib/common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib/common.sh"
usage() {
  cat <<'EOF'
Usage: gitops-sso.sh

Run as root on the original first node after gitops-cilium-and-flux.sh and
gitops-storage.sh. Register elektrokube-sso/main with the existing Flux
controllers. All SSO workloads, DNS, secrets and certificates come from Flux.
Uses /etc/elektrokube/cluster.json and /etc/rancher/k3s/k3s.yaml.
No Flux installation, Flux CLI, GitHub credentials, or Git writes.
See elektrokube-sso/README.md for DNS forwarding, CA trust and first login.
EOF
}
case ${1:-} in
  -h|--help) usage; exit 0 ;;
  '') ;;
  *) usage; die "Unknown argument: $1" ;;
esac
[[ $# == 0 ]] || die 'This script takes no arguments.'
root_only; debian_only; lock_host
[[ -s /etc/elektrokube/cluster.json && -s /etc/elektrokube/node-identity &&
   -s /etc/rancher/k3s/k3s.yaml && -x /usr/local/bin/k3s ]] || die 'Bootstrap the first node before adding SSO GitOps.'
case $(cat /etc/elektrokube/node-identity) in
  hybrid:true:*|controller:true:*) ;;
  *) die 'Run this script on the original first node.' ;;
esac
command -v git >/dev/null || die 'Missing git; complete prepare-admin.sh first.'
wait_api
# Reuse the same interpreter and Flux helpers as gitops-storage.sh.
if ! /usr/bin/python3 -c 'import yaml' >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3-yaml
fi
umask 077
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
# Keep this registration script as the only SSO addition to the scripts repo.
/usr/bin/python3 - "$REPO_ROOT/lib" "$tmp" <<'PY'
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import sys
import yaml

sys.path.insert(0, sys.argv[1])
import gitops as flux

NAME = "elektrokube-sso"
GIT_URL = "https://github.com/Sebastian-Nowaczyk-Elektrorecykling/elektrokube-sso.git"
CHILDREN = tuple("sso-" + name for name in (
    "foundation", "controllers", "credentials", "certificates", "databases",
    "authentik", "openfga", "authorization", "gateway", "oauth2-proxy",
    "heimdall", "routes", "dns"))


def check_source(obj):
    if obj:
        spec = obj["spec"]
        flux.require(spec.get("url") == GIT_URL and spec.get("ref") == {"branch": "main"},
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


def ready(obj):
    return (bool(obj) and obj.get("status", {}).get("observedGeneration") ==
            obj["metadata"].get("generation") and any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in obj.get("status", {}).get("conditions", [])))


def route_ready(obj):
    if not obj:
        return False
    for parent in obj.get("status", {}).get("parents", []):
        if parent.get("parentRef", {}).get("name") != "sso":
            continue
        conditions = {c["type"]: c for c in parent.get("conditions", [])}
        if all(conditions.get(t, {}).get("status") == "True" and
               conditions[t].get("observedGeneration") == obj["metadata"].get("generation")
               for t in ("Accepted", "ResolvedRefs")):
            return True
    return False


def preflight():
    for controller in ("source-controller", "kustomize-controller", "helm-controller"):
        flux.require(flux.get("deployment", controller), "Install Flux first.")
        flux.kube("-n", "flux-system", "rollout", "status", f"deployment/{controller}", "--timeout=300s")
    for name in ("cilium", "gateway-api", "storage-cnpg", "storage-classes"):
        dependency = flux.get(flux.SYNC, name)
        flux.require(dependency and not dependency["spec"].get("suspend"),
                     f"Required Flux Kustomization/{name} is missing or suspended.")
        flux.wait_for(flux.SYNC, name, "flux-system", ready, timeout=300)
    release = flux.get(flux.RELEASE, "cilium", "kube-system")
    version = release["spec"]["chart"]["spec"]["version"] if release else ""
    parsed = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    flux.require(parsed and tuple(map(int, parsed.groups())) >= (1, 20, 2),
                 "Cilium >= 1.20.2 is required for the native ExternalAuth integration.")
    crd = json.loads(flux.kube("get", "crd", "httproutes.gateway.networking.k8s.io", "-o", "json"))
    v1 = next((v for v in crd["spec"]["versions"] if v["name"] == "v1" and v.get("served")), {})
    properties = v1.get("schema", {}).get("openAPIV3Schema", {}).get("properties", {})
    filters = properties.get("spec", {}).get("properties", {}).get("rules", {}).get("items", {}).get(
        "properties", {}).get("filters", {}).get("items", {}).get("properties", {})
    flux.require("externalAuth" in filters,
                 "Experimental Gateway API HTTPRoute ExternalAuth schema is missing. Reconcile gateway-api first.")
    settings = flux.get("configmap", "cluster-settings")
    api_ip = (settings or {}).get("data", {}).get("API_IP", "")
    ipaddress.IPv4Address(api_ip)
    flux.require(flux.get("storageclass", "longhorn-cnpg", ""), "StorageClass/longhorn-cnpg is missing.")
    # Never silently adopt an independently installed identity stack or cert-manager.
    for kind, name, namespace, owner in (
        ("namespace", "sso", "", "sso-foundation"),
        (flux.RELEASE, "sso-cert-manager", "cert-manager", "sso-controllers"),
        (flux.RELEASE, "sso-secret-generator", "sso", "sso-controllers"),
        (flux.RELEASE, "authentik", "sso", "sso-authentik"),
    ):
        existing = flux.get(kind, name, namespace)
        if existing:
            labels = existing["metadata"].get("labels", {})
            flux.require(labels.get("kustomize.toolkit.fluxcd.io/name") == owner and
                         labels.get("kustomize.toolkit.fluxcd.io/namespace") == "flux-system",
                         f"Existing {kind}/{name} is not owned by this SSO graph; migrate it explicitly.")
    if not flux.get(flux.RELEASE, "sso-cert-manager", "cert-manager"):
        controllers = json.loads(flux.kube("get", "deployments", "-A", "-l",
                                          "app.kubernetes.io/name=cert-manager", "-o", "json"))["items"]
        flux.require(not controllers, "cert-manager is already installed; adapt the SSO graph to reuse it first.")
    flux.log(f"SSO DNS will resolve .internal names to the existing Gateway node address {api_ip}.")


def register(workdir):
    preflight()
    checkout = workdir / "sso"
    flux.run("git", "clone", "--quiet", "--depth=1", "--single-branch", "--branch", "main", GIT_URL, str(checkout))
    commit = flux.run("git", "-C", str(checkout), "rev-parse", "HEAD")
    seed = flux.documents(flux.kube("kustomize", str(checkout / "bootstrap")))
    graph = flux.documents(flux.kube("kustomize", str(checkout / "clusters/elektrokube")))
    desired_source = flux.one(seed, "GitRepository", NAME)
    desired_root = flux.one(seed, "Kustomization", NAME)
    check_source(desired_source)
    flux.require(len(seed) == 2 and all(o["metadata"].get("namespace") == "flux-system" for o in seed),
                 "SSO bootstrap must contain only its GitRepository and root Kustomization.")
    flux.require(desired_root["spec"]["path"] == "./clusters/elektrokube" and
                 desired_root["spec"]["sourceRef"] == {"kind": "GitRepository", "name": NAME},
                 "Unexpected SSO root path or source.")
    syncs = {o["metadata"]["name"]: o for o in graph if o["kind"] == "Kustomization"}
    flux.require(set(syncs) == {NAME, *CHILDREN}, "Unexpected SSO reconciliation graph.")
    source = flux.get(flux.SOURCE, NAME)
    check_source(source)
    existing = {name: flux.get(flux.SYNC, name) for name in syncs}
    for name, item in existing.items():
        check_sync(item, syncs[name])
    remote = flux.run("git", "ls-remote", "--exit-code", GIT_URL, "refs/heads/main").split()[0]
    flux.require(remote == commit, "SSO main changed during preflight; rerun before registration.")
    missing = ([desired_source] if not source else []) + ([desired_root] if not existing[NAME] else [])
    if missing:
        seed_file = workdir / "sso-seed.yaml"
        seed_file.write_text(yaml.safe_dump_all(missing, sort_keys=False))
        flux.kube("apply", "--server-side", "--field-manager=kustomize-controller", "-f", str(seed_file))
    source = flux.reconcile(flux.SOURCE, NAME)
    revision = source["status"]["artifact"]["revision"]
    flux.require(revision.rsplit(":", 1)[-1] == commit,
                 "SSO main changed during handoff; rerun to verify the new revision.")
    for name in (NAME, *CHILDREN):
        flux.log(f"Waiting for {name} at {revision}.")
        flux.reconcile(flux.SYNC, name, revision=revision)
    flux.kube("-n", "sso", "wait", "gateway/sso", "--for=condition=Programmed", "--timeout=300s")
    for route in ("sso-http-redirect", "sso-login", "sso-auth", "sso-authentik-admin", "sso-openfga-admin"):
        flux.wait_for("httproute", route, "sso", route_ready, timeout=300)
    flux.log(f"SSO and internal DNS reconciled at {revision}.")
    flux.log("Follow elektrokube-sso/README.md to trust the CA, configure LAN DNS, and obtain the generated akadmin password.")


try:
    register(Path(sys.argv[2]))
except (ValueError, RuntimeError, OSError, KeyError, yaml.YAMLError, subprocess.CalledProcessError) as exc:
    print(f"[elektrokube] ERROR: {exc}", file=sys.stderr)
    print("Fix the error and rerun gitops-sso.sh. No uninstall or rollback was attempted.\n"
          "Inspect: kubectl -n flux-system get gitrepositories,kustomizations\n"
          "         kubectl -n sso get pods,jobs,httproutes", file=sys.stderr)
    sys.exit(1)
PY
