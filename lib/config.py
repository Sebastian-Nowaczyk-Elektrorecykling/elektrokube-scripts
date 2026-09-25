#!/usr/bin/env python3
"""Validated configuration and pure renderers; JSON is also valid YAML."""
import argparse
import ipaddress
import json
import re
import shlex
from pathlib import Path

ROLES = ("worker", "controller", "hybrid")
TAINT = "CriticalAddonsOnly=true:NoSchedule"


def ipv4(value):
    ip = ipaddress.IPv4Address(value)
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_link_local:
        raise ValueError(f"Use a reachable unicast LAN IPv4 address: {value}")
    return str(ip)


def node_name(value):
    if len(value) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", value):
        raise ValueError("Node/cluster names must be DNS labels, 1–63 lowercase characters")
    return value


def validate(c, require_api=True):
    required = {"cluster_name", "api_address", "pod_cidr", "service_cidr", "cluster_dns",
                "k3s_version", "k3s_installer_sha256", "cilium_version",
                "cilium_cli_version", "helm_version", "nvidia_toolkit_version"}
    if set(c) != required or not all(isinstance(v, str) for v in c.values()):
        raise ValueError(f"Configuration requires exactly these string keys: {sorted(required)}")
    node_name(c["cluster_name"])
    if len(c["cluster_name"]) > 32:
        raise ValueError("Cilium cluster_name must be at most 32 characters")
    pod = ipaddress.IPv4Network(c["pod_cidr"])
    svc = ipaddress.IPv4Network(c["service_cidr"])
    if pod.overlaps(svc) or not 8 <= pod.prefixlen <= 24 or not 12 <= svc.prefixlen <= 27:
        raise ValueError("Use disjoint pod (/8–/24) and service (/12–/27) networks")
    dns = ipaddress.IPv4Address(c["cluster_dns"])
    if dns not in svc or dns in (svc.network_address, svc.broadcast_address, svc.network_address + 1):
        raise ValueError("cluster_dns must be a usable service IP other than the API service IP")
    if require_api or c["api_address"]:
        api = ipaddress.IPv4Address(ipv4(c["api_address"]))
        if api in pod or api in svc:
            raise ValueError("API address overlaps the pod or service network")
    patterns = {
        "k3s_version": r"v1\.\d+\.\d+\+k3s\d+",
        "k3s_installer_sha256": r"[0-9a-f]{64}",
        "cilium_version": r"\d+\.\d+\.\d+",
        "cilium_cli_version": r"v\d+\.\d+\.\d+",
        "helm_version": r"v3\.\d+\.\d+",
        "nvidia_toolkit_version": r"\d+\.\d+\.\d+-\d+",
    }
    for key, pattern in patterns.items():
        if not re.fullmatch(pattern, c[key]):
            raise ValueError(f"Invalid pinned version/checksum: {key}")
    return c


def load(path, require_api=True):
    return validate(json.loads(Path(path).read_text()), require_api)


def node_config(c, role, address, name, first=False):
    validate(c)
    if role not in ROLES or (first and role == "worker"):
        raise ValueError("First node must be controller or hybrid; join role must be valid")
    ipv4(address)
    node_name(name)
    if any(ipaddress.ip_address(address) in ipaddress.ip_network(c[k]) for k in ("pod_cidr", "service_cidr")):
        raise ValueError("Node IP overlaps the pod or service network")
    result = {"node-name": name, "node-ip": address,
              "node-label": [f"elektrokube.io/role={role}"]}
    if role != "worker":
        result.update({"flannel-backend": "none", "disable-network-policy": True,
                       "disable-kube-proxy": True, "disable": ["traefik", "servicelb", "local-storage"],
                       "cluster-cidr": c["pod_cidr"], "service-cidr": c["service_cidr"],
                       "cluster-dns": c["cluster_dns"], "secrets-encryption": True,
                       "write-kubeconfig-mode": "0600", "tls-san": [c["api_address"]]})
        if first:
            result["cluster-init"] = True
        if role == "controller":
            result["node-taint"] = [TAINT]
    if not first:
        result.update({"server": f'https://{c["api_address"]}:6443',
                       "token-file": "/etc/rancher/k3s/join.token"})
    return result


def cilium_values(c):
    validate(c)
    tolerate = [{"key": "CriticalAddonsOnly", "operator": "Exists"}]
    return {"cluster": {"name": c["cluster_name"]},
            "kubeProxyReplacement": True, "k8sServiceHost": c["api_address"],
            "k8sServicePort": 6443, "ipam": {"mode": "kubernetes"},
            "routingMode": "tunnel", "tunnelProtocol": "vxlan",
            "bpf": {"masquerade": True},
            "ipv4": {"enabled": True}, "ipv6": {"enabled": False},
            "operator": {"replicas": 1, "tolerations": tolerate},
            "hubble": {"enabled": True, "relay": {"enabled": True, "tolerations": tolerate},
                       "ui": {"enabled": True, "tolerations": tolerate}}}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["check", "env", "resolve", "node", "cilium"])
    p.add_argument("--config", required=True)
    p.add_argument("--node-ip")
    p.add_argument("--node-name")
    p.add_argument("--role", choices=ROLES)
    p.add_argument("--first", action="store_true")
    a = p.parse_args()
    c = load(a.config, require_api=a.action in ("node", "cilium"))
    if a.action == "resolve":
        c["api_address"] = c["api_address"] or ipv4(a.node_ip)
        validate(c)
        output = c
    elif a.action == "node":
        output = node_config(c, a.role, a.node_ip, a.node_name, a.first)
    elif a.action == "cilium":
        output = cilium_values(c)
    elif a.action == "env":
        print("\n".join(f"{k.upper()}={shlex.quote(v)}" for k, v in c.items()))
        return
    else:
        print("Configuration valid")
        return
    print(json.dumps(output, indent=2) + "\n", end="")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, TypeError) as exc:
        raise SystemExit(str(exc)) from exc
