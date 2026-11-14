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
| 77d2ec30694 | reload a swapped desktop plugin on the next disk poll | The visible-tab poll stays up after folder watches succeed, and a known plugin.js whose mtime or size changed reloads without Reload desktop plugins. Card t_28520a5d. |
| f5c6f1efcd3 | record a stuck worker-temp path once; ACL tests leave a liftable deny | A killed test no longer leaves Everyone (F) on a 0o700 folder. The janitor logs an unremovable path once. Card t_eaf8020a. Upstream not opened. |
| f60589632df | record a desktop plugin fingerprint after a watch reload | A file-watch save and a linked-source resync now store the new mtime and size, so the next poll does not register the pane a second time. Card t_28520a5d. |
| 9eff2de7d72 | pin worker temp from the root config when a profile has no kanban section | A profile-scoped spawn reads the root kanban.worker_temp_* values unless that profile set its own. Card t_b11af14f. Upstream not opened. |
| ac2a1fcb19c | profile-scoped kanban gc self-resolves worker temp settings | `_cmd_gc` no longer passes a load_config snapshot into the sweep. Card t_2f029367. Upstream not opened. |
| 4c7e2f8d05e | refuse a gated model unless the GM answered Yes | models.gm_gated is named and never defaulted. A matching card is not dispatched, and a matching profile model is skipped, unless the GM answered Yes on a hermes kanban ask. An agent cannot approve its own card. Card t_76f52a9c. |
| fb3f882c14b | a later Yes and a thrown check do not unlock a gated model | The Yes must be on that ask and name that model. The profile Yes is filed and looked up on one key. A thrown check does not launch the model and does not keep the gated chain. Card t_76f52a9c. |
| 0c3b3953a6b | an unreadable gate list does not launch a gated model | A thrown config load is not an empty list. The dispatch is refused, and the gated fallback, moa, aux, and profile chain are not kept. Card t_76f52a9c. |
| c5a50c47e85 | a broken settings file does not launch a gated model | A returned FailedConfigRead with no last-known-good list is not an empty list. The dispatch is refused, and the gated fallback, moa, aux, and profile chain are not kept. Card t_76f52a9c. |
| e2b5496fb58 | a good backup does not serve a gated model | A broken settings file is filtered before the backup is returned or cached. A backup with no list does not keep the profile, fallback, moa, or aux chain. Card t_76f52a9c. |
| 560c5b7e361 | report code skew only when runtime files changed | A desktop-only or docs-only fast-forward no longer locks the model picker. Git errors still report skew. Card t_60621b61. Upstream not opened. |
| 673f13534dd | a plugin save does not put a gated model back in memory | save_config stored the merged dict before the gate. A broken file is filtered before that copy is stored, returned, or cached. Card t_76f52a9c. |
| 277f5b0ae3d | skip a gated model on the profile start load | The gateway loader and the CLI start path skip model.default unless the GM answered Yes. A backup or last-known-good on that loader does not put the gated model back. Card t_76f52a9c. |
| 9a32014c4c7 | say so in chat when a reply names a file that was never made | A missing MEDIA file is replaced with the plain notice, and the next turn is told. Card t_4b0bc393. Not installed live. |
| 23d5d4a529e | do not strand Desktop on first-run setup when Hermes is only slow | A timed-out probe no longer starts first-run setup when the checkout is present. The installer URL comes from the checkout remote. The startup launcher lookup stays off the main thread. Card t_713e0080. Upstream not opened. |
| 417f9c66f92 | adopt root xAI pair on stale 403; never burn shared refresh | Empty 10/19 profile stubs no longer shadow root; force recovery adopts the newer pair without POSTing the single-use refresh. Card t_99a52155. Upstream not opened. |
| 7394973f315 | a met park wait can wake, and a landed PR is not a wait | A park note that says still parked is not a hold. A PR already in live HEAD is not a wait. A list of PRs is kept whole. The daily list reads the human note. An assignee the dispatcher will not run stays parked. Card t_8e458816. Not installed. |
| 6348ea86626 | a landed phrase does not cancel the next PR wait | Cancellation stays in the same sentence. "PR 19 is already in live HEAD. Waiting on PR 20." keeps Force_2.1#20. A landed-only sentence still records nothing. Card t_8e458816. Not installed. |

Add new rows here before pushing a carry. Upstream the individual fix as a normal PR from a short-lived branch off origin/main when appropriate.

## Automation

Two helper scripts live in `scripts/`:

- `carry-merge-upstream.ps1` (Windows, primary for this machine)
- `carry-merge-upstream.sh` (bash)

Run either when you want to bring upstream forward while keeping the branch + all carries.

They do the merge (carries come along), then the pm install / rebuild step, and print the exact verify command + restart instructions.

You can also just run `hermes update` (after the config setting above) — the `update_in_place` strategy makes Hermes itself perform the merge into `force/carry`.

Either way, the result is the same fluent model: your branch is the source of truth for the local install, upstream is merged in on demand, carries persist without manual cherry-picks after every release.
