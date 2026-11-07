#!/usr/bin/env bash
# carry-merge-upstream.sh
# Unix/macOS counterpart to the .ps1.
# Run from inside the hermes-agent checkout on force/carry.

set -euo pipefail

echo "=== Force carry merge + rebuild ==="
echo "Current branch: $(git branch --show-current)"
echo "Fetching origin..."
git fetch origin

target="force/carry"
upstream="origin/main"

current=$(git branch --show-current)
if [[ "$current" != "$target" ]]; then
    echo "Switching to $target..."
    git checkout "$target"
fi

echo "Merging $upstream into $target (carries travel forward automatically)..."
if ! git merge --no-edit "$upstream"; then
    echo "MERGE CONFLICT or error."
    echo "Resolve, git add, git commit --no-edit, then re-run this script."
    exit 1
fi

echo "Merge clean. Carries (scene_builder etc.) now on latest upstream."

echo "Running pm install / rebuild..."
if command -v hermes >/dev/null 2>&1; then
    hermes pm install
else
    python -m pm.cli install || python3 -m pm.cli install
fi

echo ""
echo "=== Next steps ==="
echo "1. Full restart of Hermes desktop (or relevant services)."
echo "2. hermes gateway restart as needed."
echo "3. Verify:"
echo '   HERMES_PROFILE=darkeyes python -c "
from hermes_cli.config import load_config_readonly
cfg = load_config_readonly()
print(\"scene_builder present:\", \"scene_builder\" in cfg)
print(\"recipes_root:\", cfg.get(\"scene_builder\", {}).get(\"recipes_root\"))
"'
echo ""
echo "Prefer hermes update after setting updates.parked_branch_strategy: update_in_place (see CARRY.md)."
echo "Commit all future carries to this branch."