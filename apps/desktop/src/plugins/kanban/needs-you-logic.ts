/**
 * Pure helpers for the amber "Needs you" column — unit-tested without React.
 * Cards parked as blocked/needs_input leave the normal Blocked column and live
 * in Needs you (cross-board). Structured asks carry options; legacy items have
 * ask:null and a free-text reason.
 */

import type { KanbanBoard, KanbanTask } from './types'

export const NEEDS_YOU = 'needs_you' as const
export const NEEDS_YOU_TONE = '#f59e0b'

export type GmAskOption = {
  key: string
  label: string
  effect: string
  image?: string
  image_url?: string
}

export type GmAsk = {
  question: string
  context: string
  /** Cliff Notes facts (≤3). Prefer over context on the card face. */
  facts?: string[]
  options: GmAskOption[]
  pick: null | string
  asked_by?: string
}

export type NeedsYouItem = {
  task_id: string
  title: string
  board: string
  board_name: string
  status: string
  block_kind: null | string
  block_reason: null | string
  legacy_reason: null | string
  assignee: null | string
  asked_by: null | string
  asked_at: null | number
  ask: GmAsk | null
}

export type NeedsYouResponse = {
  count: number
  items: NeedsYouItem[]
}

/** A blocked card waiting on the human (any needs_input kind). */
export function isNeedsYouTask(task: Pick<KanbanTask, 'status' | 'block_kind'>): boolean {
  return task.status === 'blocked' && task.block_kind === 'needs_input'
}

/** Drop needs_input cards from Blocked so they never show twice. */
export function stripNeedsYouFromBoard(board: KanbanBoard): KanbanBoard {
  return {
    ...board,
    columns: board.columns.map(col => {
      if (col.name !== 'blocked') {
        return col
      }

      return { ...col, tasks: col.tasks.filter(task => !isNeedsYouTask(task)) }
    })
  }
}

/** Per-board open-ask counts for switcher badges (hidden at 0 by the UI). */
export function countNeedsYouByBoard(items: readonly NeedsYouItem[]): Record<string, number> {
  const counts: Record<string, number> = {}

  for (const item of items) {
    const slug = item.board || 'default'
    counts[slug] = (counts[slug] ?? 0) + 1
  }

  return counts
}

export function optionsAreImagePair(ask: GmAsk | null | undefined): boolean {
  if (!ask || ask.options.length < 2) {
    return false
  }

  return ask.options.every(opt => Boolean(opt.image || opt.image_url))
}

/** Write-time caps (design note v4). Over-cap filed asks render clamped. */
export const CAP = {
  question: 90,
  label: 60,
  effect: 110,
  fact: 110,
  facts: 3,
  contextSentences: 2,
  contextChars: 220
} as const

export type CommentLike = {
  id: number | string
  author: string
  body: string
  created_at: number
}

export type FoldedComment =
  | { kind: 'single'; comment: CommentLike }
  | {
      kind: 'fold'
      author: string
      body: string
      count: number
      lastAt: number
      ids: Array<number | string>
      stamps: CommentLike[]
    }

/** Soft clamp for display of over-cap filed asks (never throws). */
export function clampText(value: string, max: number): string {
  const s = value.trim()

  if (s.length <= max) {
    return s
  }

  if (max <= 1) {
    return s.slice(0, max)
  }

  return `${s.slice(0, Math.max(0, max - 1)).trimEnd()}…`
}

/** Facts for the Cliff Notes strip: prefer ask.facts; else first context sentence cut at 110. */
export function cliffNotesFacts(ask: GmAsk): string[] {
  const raw = ask.facts

  if (Array.isArray(raw) && raw.length > 0) {
    return raw
      .filter((f): f is string => typeof f === 'string')
      .map(f => clampText(f, CAP.fact))
      .filter(Boolean)
      .slice(0, CAP.facts)
  }

  const ctx = typeof ask.context === 'string' ? ask.context.trim() : ''

  if (!ctx) {
    return []
  }

  // First sentence-ish chunk; keep URL dots whole by stopping on .?! + space/end only.
  const m = ctx.match(/^(.+?[.!?])(?:\s+|$)/)
  const first = (m ? m[1] : ctx).trim()

  return first ? [clampText(first, CAP.fact)] : []
}

/** Newest created_at first; equal stamps keep input order (stable). */
export function sortCommentsNewestFirst<T extends { created_at: number }>(comments: readonly T[]): T[] {
  return comments
    .map((c, index) => ({ c, index }))
    .sort((a, b) => b.c.created_at - a.c.created_at || a.index - b.index)
    .map(x => x.c)
}

/**
 * Normalise comment body for fold matching (design note v4 §4 / build-v4.cjs):
 * strip clock stamps, collapse whitespace, trim, lower-case. Two bodies that
 * only differ by a leading/embedded "9:18 AM" or trailing space still match.
 */
export function normalizeCommentBody(body: string): string {
  return body
    .replace(/\b\d{1,2}:\d{2}\s*(?:AM|PM)\b/gi, '')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase()
}

/**
 * Fold identical same-author bodies when count ≥ 3 — globally across the
 * thread, not only consecutive runs. Two identical stay two. The fold sits
 * where its newest member sits; stamps are newest-first. Input should already
 * be newest-first (see sortCommentsNewestFirst).
 */
export function foldRepeats(comments: readonly CommentLike[]): FoldedComment[] {
  const groups = new Map<string, CommentLike[]>()

  for (const comment of comments) {
    const key = `${comment.author}\0${normalizeCommentBody(comment.body)}`
    const list = groups.get(key)

    if (list) {
      list.push(comment)
    } else {
      groups.set(key, [comment])
    }
  }

  /** newest comment id → fold row to emit in its place */
  const foldAtNewest = new Map<string | number, FoldedComment>()
  /** older members of a fold — skip when walking the thread */
  const skipIds = new Set<string | number>()

  for (const members of groups.values()) {
    if (members.length < 3) {
      continue
    }

    const stamps = [...members].sort(
      (a, b) => b.created_at - a.created_at || String(b.id).localeCompare(String(a.id))
    )

    const newest = stamps[0]!
    // Display the newest member's text (trimmed); match key is separate.
    const body = newest.body.replace(/\s+/g, ' ').trim()

    foldAtNewest.set(newest.id, {
      kind: 'fold',
      author: newest.author,
      body,
      count: stamps.length,
      lastAt: newest.created_at,
      ids: stamps.map(s => s.id),
      stamps
    })

    for (const member of stamps) {
      if (member.id !== newest.id) {
        skipIds.add(member.id)
      }
    }
  }

  const out: FoldedComment[] = []

  for (const comment of comments) {
    if (skipIds.has(comment.id)) {
      continue
    }

    const fold = foldAtNewest.get(comment.id)

    if (fold) {
      out.push(fold)
    } else {
      out.push({ kind: 'single', comment })
    }
  }

  return out
}

/** True when this option is the last of a 3-option set and should span both columns. */
export function optionSpansFull(count: number, index: number): boolean {
  return count === 3 && index === 2
}

/**
 * Titles often start with "GM yes/no:" because the card was filed that way.
 * The Needs you column already says what it is — drop the redundant prefix so
 * the question starts at the left edge.
 */
const GM_YES_NO_PREFIX = /^\s*gm\s*yes\s*\/\s*no\s*:\s*/i

export function displayNeedsYouTitle(title: string, fallback = 'Needs a decision'): string {
  const trimmed = (title || '').trim()

  if (!trimmed) {
    return fallback
  }

  const stripped = trimmed.replace(GM_YES_NO_PREFIX, '').trim()

  return stripped || fallback
}

/** Resolve the asker's recommended option, if any. */
export function recommendedOption(ask: GmAsk): GmAskOption | null {
  if (!ask.pick) {
    return null
  }

  return ask.options.find(o => o.key === ask.pick) ?? null
}

/** True when Recommend line should render. */
export function showRecommend(ask: GmAsk): boolean {
  return recommendedOption(ask) != null
}
