#!/usr/bin/env bash
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
for script in ./*.sh lib/*.sh tests/*.sh; do bash -n "$script"; done
shellcheck -x -P SCRIPTDIR ./*.sh lib/*.sh tests/*.sh
python3 -m unittest discover -s tests -v
for script in prepare-node.sh bootstrap-cluster.sh prepare-admin.sh install.sh add-node.sh; do
  bash "$script" --help >/dev/null
done
python3 lib/config.py check --config config/cluster.json
