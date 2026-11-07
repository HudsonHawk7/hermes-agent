# carry-merge-upstream.ps1
# Automatic merge of upstream into force/carry + rebuild for fluent Force carries.
# Run this (or just `hermes update` after setting parked_branch_strategy) when you want latest upstream + all carries.
#
# Usage (from the hermes-agent dir or PATH):
#   powershell -File scripts/carry-merge-upstream.ps1
#
# This keeps us on the long-lived feature branch model:
#   - Our carries (scene_builder preservation, future plugin fixes) live on force/carry forever.
#   - Merge brings upstream forward; our commits come along automatically.
#   - On conflict: stop and let human resolve (the "hard" part happens here once).
#   - Then rebuilds (pm install) and suggests gateway restart.
#
# Recommended: also set in profile config.yaml (or the config the updater reads):
#   updates:
#     parked_branch_strategy: update_in_place
#     auto_switch_parked_branch: false
# This makes plain `hermes update` do the merge-in-place automatically.

$ErrorActionPreference = "Stop"
$repo = (Get-Location).Path
if (-not (Test-Path ".git")) {
    Write-Error "Run this from inside the hermes-agent git checkout (the force/carry tree)."
}

Write-Host "=== Force carry merge + rebuild ===" -ForegroundColor Cyan
Write-Host "Current branch: $(git branch --show-current)"
Write-Host "Fetching origin..."
git fetch origin

$targetBranch = "force/carry"
$upstream = "origin/main"

# Ensure we are on the carry branch
$current = git branch --show-current
if ($current -ne $targetBranch) {
    Write-Host "Switching to $targetBranch..."
    git checkout $targetBranch
}

Write-Host "Merging $upstream into $targetBranch (this brings all previous carries forward automatically)..."
$mergeOutput = git merge --no-edit $upstream 2>&1
$exit = $LASTEXITCODE

if ($exit -ne 0) {
    Write-Host "MERGE CONFLICT or error." -ForegroundColor Red
    Write-Host "Resolve the conflicts in the usual way (edit files, git add, git commit --no-edit), then re-run this script."
    Write-Host "The carry commits that were already on this branch will survive the merge."
    exit 1
}

Write-Host "Merge clean. Upstream advanced, our carries (e.g. scene_builder config preservation) are now on top of latest." -ForegroundColor Green

# Rebuild / install step (equivalent to what hermes update does after the code pull)
Write-Host "Running dependency + build step (pm install)..."
# The exact command used by install/update is python -m pm.cli install (or hermes pm install after bootstrap)
# Use the hermes launcher if available, fall back to python -m
if (Get-Command hermes -ErrorAction SilentlyContinue) {
    & hermes pm install
} else {
    # Bootstrap python
    $python = "python"
    if (Test-Path ".venv\Scripts\python.exe") { $python = ".venv\Scripts\python.exe" }
    & $python -m pm.cli install
}

if ($LASTEXITCODE -ne 0) {
    Write-Host "pm install step had issues. Check output above. You may need to run `hermes pm install` or `hermes update` manually after fixing."
}

Write-Host ""
Write-Host "=== Next steps ===" -ForegroundColor Cyan
Write-Host "1. Full restart of Hermes desktop app (close completely, reopen)."
Write-Host "2. For any running gateways: hermes gateway restart (or per-profile)."
Write-Host "3. Verify the scene_builder carry (example for darkeyes):"
Write-Host '   $env:HERMES_PROFILE="darkeyes"; python -c "from hermes_cli.config import load_config_readonly; cfg=load_config_readonly(); print(\"scene_builder:\", \"scene_builder\" in cfg); print(\"recipes_root:\", cfg.get(\"scene_builder\",{}).get(\"recipes_root\"))"'
Write-Host ""
Write-Host "To keep this automatic going forward, prefer `hermes update` after setting the parked_branch_strategy in config (see CARRY.md)."
Write-Host "All future Force plugin carries should be committed to this branch so they travel with the merges." -ForegroundColor Green

# Optional: show current status
git status --short | Select-Object -First 5
git log --oneline -3

Write-Host "Carry merge + rebuild complete." -ForegroundColor Green