#!/usr/bin/env bash
# Delegates to the real CLI. Does not fetch origin. Does not switch branch.
# The repo is this script's parent, not the process cwd. set -e exits non-zero
# when the CLI fails, and this script does not claim success.
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
if [[ ! -e "$repo/.git" ]]; then
    echo "hermes-agent checkout not found at $repo" >&2
    exit 1
fi
export PYTHONPATH="$repo"
python -m hermes_cli.main carry merge --repo "$repo"
