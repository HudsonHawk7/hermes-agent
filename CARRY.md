# Force fork branch policy

**`force/carry` is canonical.** Evidence as of 2026-11-05 (card t_d259821d):

- The live install (`%LOCALAPPDATA%\hermes\hermes-agent`) is on `force/carry`.
- PRs 15, 17, 19, 20, 21, and 22 targeted `force/carry`.
- The landing plan uses `force/carry`. Architect fast-forwards this branch into the install at a quiet moment. This card does not touch the live install.

Future HudsonHawk7/hermes-agent PRs target **`force/carry`**.

## `force/live` is retired

`force/live` diverged both ways (32 commits not in carry, 78 not in live), so it cannot fast-forward to carry. Do not open or merge new PRs against `force/live`. Leave the branch as historical snapshot. Retarget leftover open PRs (including #23) to `force/carry`.

## Live-only PRs brought onto `force/carry` (this card)

Cherry-picks of each original merge's first-parent diff:

| Original PR | Carry SHA | What |
|---|---|---|
| #9 `a48ba3e725b` | `62a257c2a94` | native Local media send + cron MEDIA auto-append |
| #7 `413d7bd9dab` | `1303736579c` | group-chat image drop + vision saw/miss receipts |
| #6 `0ba37c12457` | `ec58f9df0f6` | group chat follow-latest + Enter sends |
| #5 `18a1013144d` | `0235ac72aa3` | cuBLAS 12 so local whisper transcribes on GPU |
| #4 `2f319e5041a` | `bdcff002ce2` | kanban pre-heartbeat stall rule 5/15 |
| #13 `7a296075ef4` | `94c7c442c48` | inherited apps-desktop gate noise |
| leftover unique live commit `318d6935545` | `ea70ba44229` | bound version_info git probes (not in the six PRs; brought so nothing is lost) |

After these land, `git diff force/live force/carry` is carry-ahead work only (the 78 commits live never received).

## Ongoing Force plugin carries (scene_builder, image_gen, etc.)

`force/carry` is the long-lived development branch for the local install.

**Preferred update model (GM direction 2026-11-06):** stay on `force/carry` permanently. When upstream advances, merge latest `origin/main` into `force/carry` (exactly as a normal feature branch). Build and run from the merged branch.

Set in the relevant `config.yaml` (profile or install):

```yaml
updates:
  parked_branch_strategy: update_in_place
  auto_switch_parked_branch: false
```

`hermes update` (when safe) will then automatically merge `origin/main` **into** `force/carry` instead of switching away or stashing. Your carry commits (including config preservation for Force plugins) travel forward automatically on every update. On divergence it creates a merge commit behind a safety tag. Conflicts stop cleanly for resolution — the "hard cherry-pick" work happens at merge time, once.

After merge + update, full desktop/gateway restart to pick up source + plugin changes. Verify with:

`HERMES_PROFILE=darkeyes python -c "
from hermes_cli.config import load_config_readonly
cfg = load_config_readonly()
print('scene_builder present:', 'scene_builder' in cfg)
print('recipes_root:', cfg.get('scene_builder', {}).get('recipes_root'))
"`

## Current carries on force/carry (in addition to historical live ports)

| Commit | What | Notes |
|--------|------|-------|
| 8027813a14c | preserve unknown top-level config sections (scene_builder etc) in load_config_readonly() | Hermes core fix so Force plugins always see their sections after merge/canonicalize. Paired with Force_2.1 side patch in Tools/hermes-force-media. |
| 7eb0c0cbf09 | sample video frames when the model cannot take a video part | video_analyze and vision_analyze fall back to ffmpeg frames on a local OpenAI-compatible route or a 400 invalid message format. Card t_56bef84c. |
| bc4e228e3ec | t_b855b275 Hermes Desktop e2e hidden-mode harness and sandbox-home fixes | waitForInteractive under E2E_HIDDEN; ruamel unconditional; S: and doctor --ci documented. Card t_b855b275. |

Add new rows here before pushing a carry. Upstream the individual fix as a normal PR from a short-lived branch off origin/main when appropriate.

## Automation

Two helper scripts live in `scripts/`:

- `carry-merge-upstream.ps1` (Windows, primary for this machine)
- `carry-merge-upstream.sh` (bash)

Run either when you want to bring upstream forward while keeping the branch + all carries.

They do the merge (carries come along), then the pm install / rebuild step, and print the exact verify command + restart instructions.

You can also just run `hermes update` (after the config setting above) — the `update_in_place` strategy makes Hermes itself perform the merge into `force/carry`.

Either way, the result is the same fluent model: your branch is the source of truth for the local install, upstream is merged in on demand, carries persist without manual cherry-picks after every release.
