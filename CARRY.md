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
