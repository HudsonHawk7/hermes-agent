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
