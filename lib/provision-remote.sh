#!/usr/bin/env bash
set -Eeuo pipefail
# shellcheck source=common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
root_only; debian_only; lock_host
payload="$REPO_ROOT/payload"
role=; host=; node_name=; gpu=; iommu=false
trap 'rm -f -- "$payload/join.token"' EXIT
settings=$(python3 - "$payload/node.json" <<'PY'
import json, shlex, sys
d = json.load(open(sys.argv[1]))
for key in ('role', 'host', 'node_name', 'gpu'):
    print(key + '=' + shlex.quote(d[key]))
print('iommu=' + ('true' if d['iommu'] else 'false'))
PY
)
eval "$settings"
prepare_args=()
[[ $iommu != true ]] || prepare_args+=(--enable-iommu)
"$REPO_ROOT/prepare-node.sh" --gpu "$gpu" --config "$payload/cluster.json" "${prepare_args[@]}"
"$REPO_ROOT/lib/install-node.sh" "$payload/cluster.json" "$role" "$host" "$node_name" false "$payload/join.token"
