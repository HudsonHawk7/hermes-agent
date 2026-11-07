# Delegates to the real CLI. Does not fetch origin. Does not switch branch.
# The repo is this script's parent, not the process cwd. A failed step exits
# non-zero and this script does not claim success.
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $repo ".git"))) {
    Write-Error "hermes-agent checkout not found at $repo"
    exit 1
}
$env:PYTHONPATH = $repo
python -m hermes_cli.main carry merge --repo $repo
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
