import { $pluginDecisions, $pluginRecords, enablePackageDesktopHalf, type PluginRecord } from '@/contrib/plugins-store'
import type { AgentPluginRow, GatewayRequest } from '@/store/agent-plugins'
import { $activeProfile, $profiles } from '@/store/profile'
import { $connection } from '@/store/session'

import { desktopPackageName, mergePluginPackages } from './plugin-packages'

/**
 * RECONCILE A UNIFIED PACKAGE'S TWO HALVES.
 *
 * A package whose folder sits under `plugins/<name>/desktop/` ships its desktop
 * half opt-in (`runtime-loader.ts`: `defaultEnabled: marker ? false : undefined`)
 * to match an agent half that is inert until the user turns it on. Two UI paths
 * used to turn the half on with it: the install dialog's "Enable after install"
 * and the per-package Agent switch, both through `enablePackageDesktopHalf`.
 *
 * A third path exists and had no door: an agent half enabled by CONFIG rather
 * than by this app (`config.yaml` -> `plugins.enabled`, which is how a scripted
 * or Force-provisioned profile arrives). Nothing fired, the desktop half stayed
 * disabled, and its pane plus composer button never registered, with no load
 * error to explain the absence, while the Plugins card read the AGENT half and
 * happily showed the package as on.
 *
 * This module is that door. The rule is the one the other two already apply:
 * the desktop half follows the agent half, and an explicit user OFF still wins.
 * The trigger is the user's enabled agent half, never the plugin itself, so a
 * plugin still cannot self-enable.
 */

/** One desktop half this sweep turned on. */
export interface ReconciledDesktopHalf {
  /** Desktop-half plugin id, the record whose pane now registers. */
  id: string
  /** Package folder joining the two halves. */
  packageName: string
  /** Profile whose enabled agent half triggered it. */
  profile: string
}

interface ProfileScope {
  /** Human name used in the log line. */
  label: string
  /** `plugins.manage` scope: null is the backend's launch profile. */
  scope: null | string
}

/** The launch profile plus every profile the rail knows. "Enabled in ANY local
 *  profile" is the rule, and the launch profile is the one a config-driven
 *  installer writes. Names are deduped because the launch profile usually also
 *  appears in the list. */
function profileScopes(): ProfileScope[] {
  const launch = $activeProfile.get().trim() || 'default'
  const scopes: ProfileScope[] = [{ label: launch, scope: null }]
  const seen = new Set([launch])

  for (const profile of $profiles.get()) {
    const name = (profile.name ?? '').trim()

    if (!name || seen.has(name)) {
      continue
    }

    seen.add(name)
    scopes.push({ label: name, scope: name })
  }

  return scopes
}

async function listAgentPlugins(request: GatewayRequest, scope: null | string): Promise<AgentPluginRow[]> {
  const result = await request<{ plugins?: AgentPluginRow[] }>(
    'plugins.manage',
    scope ? { action: 'list', profile: scope } : { action: 'list' }
  )

  return result?.plugins ?? []
}

/** Halves the rule applies to: a marker'd desktop half that is off, with no
 *  decision recorded for it. An explicit `false` is a decision and is skipped
 *  here, which is what keeps a deliberate OFF winning. */
function undecidedDesktopHalves(records: readonly PluginRecord[], decisions: Record<string, boolean>): PluginRecord[] {
  return records.filter(
    record => desktopPackageName(record) !== null && record.status === 'disabled' && !(record.id in decisions)
  )
}

/** One sweep's identity: which halves are still waiting. A repeat publish of
 *  the same inventory (the disk poll) then costs no backend calls. */
function pendingSignature(halves: readonly PluginRecord[]): string {
  return halves
    .map(record => record.id)
    .sort()
    .join('|')
}

let inFlight: Promise<ReconciledDesktopHalf[]> | null = null
let lastSignature: null | string = null

async function sweep(
  request: GatewayRequest,
  records: readonly PluginRecord[],
  decisions: Record<string, boolean>,
  signature: string
): Promise<ReconciledDesktopHalf[]> {
  // packageName -> the profile whose agent half is on; first one wins.
  const enabledIn = new Map<string, string>()

  for (const { label, scope } of profileScopes()) {
    let rows: AgentPluginRow[]

    try {
      rows = await listAgentPlugins(request, scope)
    } catch {
      // A profile that cannot answer (an older backend, a scope that died
      // mid-sweep) must not stop the whole reconciliation: the launch profile
      // is the one a config-driven install writes, and it is asked first.
      continue
    }

    for (const pkg of mergePluginPackages(records, rows)) {
      // The halves must disagree in the one direction this rule repairs: agent
      // on, desktop off, and no recorded choice either way about the desktop.
      if (
        pkg.agent?.status !== 'enabled' ||
        !pkg.desktop ||
        pkg.desktop.status !== 'disabled' ||
        pkg.desktop.id in decisions
      ) {
        continue
      }

      if (!enabledIn.has(pkg.key)) {
        enabledIn.set(pkg.key, label)
      }
    }
  }

  const done: ReconciledDesktopHalf[] = []

  for (const [packageName, profile] of enabledIn) {
    try {
      await enablePackageDesktopHalf(packageName, { keepUserChoice: true })
    } catch (error) {
      // One package that will not load must not keep the others dark, and it
      // must not vanish: say which one failed and why.
      window.hermesDesktop?.logLine?.(
        `[plugins] could not enable desktop half ${packageName}: ${error instanceof Error ? error.message : String(error)}`
      )

      continue
    }

    for (const record of Object.values($pluginRecords.get())) {
      if (desktopPackageName(record) === packageName && record.status === 'loaded') {
        done.push({ id: record.id, packageName, profile })
      }
    }
  }

  // One line per half, so the next "why can't I see the pane" is answered from
  // the log instead of from the source. logLine is the renderer -> desktop.log
  // bridge; it is absent on non-Electron hosts.
  for (const half of done) {
    window.hermesDesktop?.logLine?.(`[plugins] enabled desktop half ${half.id}: agent half on in ${half.profile}`)
  }

  lastSignature = signature

  return done
}

/**
 * Turn on every desktop half whose agent half is enabled somewhere, once.
 *
 * Call it when the plugin inventory loads and after an agent rescan. Safe on
 * every inventory publish: it returns early when nothing is waiting, and a
 * concurrent call joins the sweep already running.
 *
 * `force` re-checks a set a previous sweep already looked at (a rescan may have
 * enabled an agent half in a profile this app had not read yet).
 */
export function reconcileAgentEnabledDesktopHalves(
  request: GatewayRequest,
  { force = false }: { force?: boolean } = {}
): Promise<ReconciledDesktopHalf[]> {
  // A remote/cloud backend keeps its own plugin state; this half is local to
  // THIS machine's app, and a remote backend's `plugins.enabled` says nothing
  // about what this app should load.
  if ($connection.get()?.mode === 'remote') {
    return Promise.resolve([])
  }

  const records = Object.values($pluginRecords.get())
  const decisions = $pluginDecisions.get()
  const halves = undecidedDesktopHalves(records, decisions)

  if (!halves.length) {
    // Nothing waiting: a later half (a fresh install, a rescan) must re-check
    // from scratch rather than be suppressed by an old sweep's signature.
    lastSignature = null

    return Promise.resolve([])
  }

  const signature = pendingSignature(halves)

  if (!force && signature === lastSignature) {
    return Promise.resolve([])
  }

  if (inFlight) {
    return inFlight
  }

  const run = sweep(request, records, decisions, signature)

  inFlight = run

  const clear = () => {
    if (inFlight === run) {
      inFlight = null
    }
  }

  run.then(clear, clear)

  return run
}

/** Test seam: forget the last sweep so a fresh inventory is re-checked. */
export function resetDesktopHalfReconcileState(): void {
  inFlight = null
  lastSignature = null
}
