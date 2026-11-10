/**
 * Desktop startup rules for a slow-but-present Hermes checkout.
 *
 * A probe timeout is not a missing install. A fork branch is not an upstream
 * URL. A launch must not restart a live gateway while that probe is in flight.
 */
import { execFile } from 'node:child_process'

export const STILL_STARTING_PREFIX = 'Hermes is still starting'
export const PROBE_RETRY_TRIES = 3
export const PROBE_RETRY_BACKOFF_MS = [1_000, 2_000] as const
const UPSTREAM_BRANCHES = new Set(['main', 'master'])

export class CheckoutProbeError extends Error {
  readonly reason: 'timeout' | 'exit'
  readonly exitCode: number | null
  readonly elapsedMs: number
  readonly attempts: number

  constructor(fields: {
    reason: 'timeout' | 'exit'
    exitCode: number | null
    elapsedMs: number
    attempts: number
  }) {
    super(
      `hermes --version probe ${fields.reason}${fields.exitCode != null ? ` exit ${fields.exitCode}` : ''} after ${fields.elapsedMs}ms, ${fields.attempts} tries`
    )
    this.name = 'CheckoutProbeError'
    this.reason = fields.reason
    this.exitCode = fields.exitCode
    this.elapsedMs = fields.elapsedMs
    this.attempts = fields.attempts
  }
}

export function isCheckoutProbeError(err: unknown): err is CheckoutProbeError {
  return err instanceof CheckoutProbeError
}

export function classifyProbeError(err: unknown): { reason: 'timeout' | 'exit'; exitCode: number | null } {
  if (!err || typeof err !== 'object') {
    return { reason: 'exit', exitCode: null }
  }

  const probe = err as { code?: unknown; killed?: boolean; signal?: unknown }

  if (probe.killed === true || probe.code === 'ETIMEDOUT' || probe.signal === 'SIGTERM') {
    return { reason: 'timeout', exitCode: typeof probe.code === 'number' ? probe.code : null }
  }

  return { reason: 'exit', exitCode: typeof probe.code === 'number' ? probe.code : null }
}

let lastProbeFailure: CheckoutProbeError | null = null
let probeDepth = 0

export function noteProbeFailure(err: CheckoutProbeError): void {
  lastProbeFailure = err
}

export function takeLastProbeFailure(): CheckoutProbeError | null {
  const current = lastProbeFailure

  lastProbeFailure = null

  return current
}

export function enterRuntimeProbe(): () => void {
  probeDepth += 1
  let left = false

  return () => {
    if (left) {
      return
    }

    left = true
    probeDepth = Math.max(0, probeDepth - 1)
  }
}

export function runtimeProbeInFlight(): boolean {
  return probeDepth > 0
}

export function resetRuntimeProbeForTests(): void {
  probeDepth = 0
  lastProbeFailure = null
}

export function stillStartingMessage(elapsedMs: number): string {
  const seconds = Math.max(1, Math.round(elapsedMs / 1000))

  return `${STILL_STARTING_PREFIX} (probe took ${seconds} s)`
}

export function quietStaleGatewayLog(stale: { gateway: string; client: string }): string {
  return `[gateway] owner runs ${stale.gateway} but this Hermes is ${stale.client}; gateway is on older code; restart it at a quiet moment`
}

export interface ProbeBackoffOptions {
  tries?: number
  backoffMs?: readonly number[]
  sleep?: (ms: number) => Promise<void>
}

export async function probeWithBackoff(
  probe: () => Promise<void>,
  options: ProbeBackoffOptions = {}
): Promise<{ ok: true; attempts: number; elapsedMs: number } | { ok: false; failure: CheckoutProbeError }> {
  const tries = options.tries ?? PROBE_RETRY_TRIES
  const backoff = options.backoffMs ?? PROBE_RETRY_BACKOFF_MS
  const sleep = options.sleep ?? (ms => new Promise(resolve => setTimeout(resolve, ms)))
  const started = Date.now()
  let last: unknown = null

  for (let attempt = 1; attempt <= tries; attempt += 1) {
    try {
      await probe()

      return { ok: true, attempts: attempt, elapsedMs: Date.now() - started }
    } catch (err) {
      last = err

      if (attempt < tries) {
        const wait = backoff[Math.min(attempt - 1, backoff.length - 1)] ?? 0

        if (wait > 0) {
          await sleep(wait)
        }
      }
    }
  }

  const classified = classifyProbeError(last)

  return {
    ok: false,
    failure: new CheckoutProbeError({
      reason: classified.reason,
      exitCode: classified.exitCode,
      elapsedMs: Date.now() - started,
      attempts: tries
    })
  }
}

export interface FirstRunDecisionInput {
  checkoutPresent: boolean
  probeOk: boolean
  repair: boolean
  ignoreExisting: boolean
  elapsedMs: number
  reason: 'timeout' | 'exit' | 'ok'
  exitCode: number | null
  attempts: number
}

export function refuseFirstRunWhenCheckoutPresent(
  input: FirstRunDecisionInput
):
  | { action: 'use-runtime' }
  | { action: 'bootstrap' }
  | {
      action: 'still-starting'
      message: string
      reason: 'timeout' | 'exit' | 'ok'
      elapsedMs: number
      attempts: number
      exitCode: number | null
    } {
  if (input.probeOk) {
    return { action: 'use-runtime' }
  }

  if (input.repair || input.ignoreExisting || !input.checkoutPresent) {
    return { action: 'bootstrap' }
  }

  return {
    action: 'still-starting',
    message: stillStartingMessage(input.elapsedMs),
    reason: input.reason,
    elapsedMs: input.elapsedMs,
    attempts: input.attempts,
    exitCode: input.exitCode
  }
}

export function githubSlugFromRemote(remoteUrl: string): string | null {
  const trimmed = remoteUrl.trim().replace(/\.git$/, '')
  const ssh = /^(?:git@|ssh:\/\/git@)github\.com[:/]([^/\s]+\/[^/\s]+)$/.exec(trimmed)

  if (ssh?.[1]) {
    return ssh[1]
  }

  const https = /^https?:\/\/github\.com\/([^/\s]+\/[^/\s]+)$/.exec(trimmed)

  return https?.[1] ?? null
}

export function installScriptSource(input: {
  ref: string
  scriptName: string
  remoteUrl?: string | null
}): { url: string; ownerRepo: string } | { error: string } {
  const slug = input.remoteUrl ? githubSlugFromRemote(input.remoteUrl) : null
  const upstreamBranch = UPSTREAM_BRANCHES.has(input.ref)
  const pinnedSha = /^[0-9a-f]{40}$/i.test(input.ref)

  if (slug) {
    return {
      url: `https://raw.githubusercontent.com/${slug}/${input.ref}/scripts/${input.scriptName}`,
      ownerRepo: slug
    }
  }

  if (upstreamBranch || pinnedSha) {
    return {
      url: `https://raw.githubusercontent.com/NousResearch/hermes-agent/${input.ref}/scripts/${input.scriptName}`,
      ownerRepo: 'NousResearch/hermes-agent'
    }
  }

  return {
    error:
      `Cannot resolve installer source for branch ${input.ref}: the checkout git remote could not be read. ` +
      `Desktop will not download ${input.scriptName} from NousResearch/hermes-agent because ${input.ref} is not an upstream branch.`
  }
}

export function readCheckoutGithubRemote(root: string | null | undefined, git = 'git'): Promise<string | null> {
  if (!root) {
    return Promise.resolve(null)
  }

  const read = (name: string) =>
    new Promise<string | null>(resolve => {
      execFile(
        git,
        ['-C', root, 'remote', 'get-url', name],
        { timeout: 5_000, windowsHide: true },
        (err, stdout) => {
          if (err) {
            resolve(null)

            return
          }

          const url = String(stdout).trim()

          resolve(url || null)
        }
      )
    })

  return read('fork').then(url => (url ? url : read('origin')))
}
