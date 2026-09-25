#!/usr/bin/env python3
"""Choose a first-node IPv4 from saved settings or the active LAN interfaces."""
import argparse
import ipaddress
import json
from pathlib import Path
import subprocess
import sys

from config import ipv4, load

# Avoid silently using a container, overlay or VPN address for the cluster API.
# An explicit --interface/--node-ip can select a deliberately managed virtual LAN.
EXCLUDED_PREFIXES = ("docker", "veth", "cni", "cilium", "lxc", "flannel", "virbr",
                     "tun", "tap", "wg", "tailscale", "zt")


def candidates(interfaces, cluster, interface=None, automatic=True):
    networks = [ipaddress.ip_network(cluster[k]) for k in ("pod_cidr", "service_cidr")]
    found = {}
    for link in interfaces:
        name = link["ifname"]
        if interface and name != interface:
            continue
        if automatic and not interface and name.startswith(EXCLUDED_PREFIXES):
            continue
        for address in link.get("addr_info", []):
            if address.get("family") != "inet" or address.get("scope") != "global":
                continue
            if address.get("tentative") or address.get("dadfailed") or address.get("deprecated"):
                continue
            try:
                value = ipv4(address["local"])
            except ValueError:
                continue
            if any(ipaddress.ip_address(value) in net for net in networks):
                continue
            found.setdefault(name, set()).add(value)
    return found


def choose_ip(interfaces, routes, cluster, explicit="", saved="", interface=None):
    fixed = explicit or saved or cluster["api_address"]
    available = candidates(interfaces, cluster, interface, automatic=not fixed)
    all_ips = set().union(*available.values()) if available else set()
    if fixed:
        fixed = ipv4(fixed)
        if fixed not in all_ips:
            raise ValueError(f"Selected/saved node IP {fixed} is not an active local address"
                             + (f" on {interface}" if interface else "")
                             + "; restore the address or deliberately reconfigure the node")
        return fixed
    defaults = [r for r in routes if r.get("dst") == "default" and r.get("dev") in available
                and r.get("type", "unicast") == "unicast"]
    if defaults:
        best_metric = min(int(r.get("metric", 0)) for r in defaults)
        best = [r for r in defaults if int(r.get("metric", 0)) == best_metric]
        choices = set()
        for route in best:
            addresses = available[route["dev"]]
            preferred = route.get("prefsrc")
            choices.update({preferred} if preferred in addresses else addresses)
    else:
        choices = all_ips
    if len(choices) == 1:
        return choices.pop()
    details = ", ".join(f"{dev}: {'/'.join(sorted(ips))}" for dev, ips in sorted(available.items())) or "none"
    raise ValueError("Cannot select one LAN IPv4 automatically (candidates: " + details
                     + "). Use --interface IFACE or --node-ip IP.")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True)
    p.add_argument("--node-ip", default="")
    p.add_argument("--interface")
    a = p.parse_args()
    cluster = load(a.config, require_api=False)
    state = Path("/etc/elektrokube/cluster.json")
    saved = load(state)["api_address"] if state.exists() else ""
    interfaces = json.loads(subprocess.check_output(["ip", "-j", "-4", "address", "show", "up", "scope", "global"]))
    routes = json.loads(subprocess.check_output(["ip", "-j", "-4", "route", "show", "default"]))
    print(choose_ip(interfaces, routes, cluster, a.node_ip, saved, a.interface))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        sys.exit(str(exc))
