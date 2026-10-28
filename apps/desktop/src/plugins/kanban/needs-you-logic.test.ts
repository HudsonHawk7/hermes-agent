import { describe, expect, it } from 'vitest'

import {
  countNeedsYouByBoard,
  isNeedsYouTask,
  type NeedsYouItem,
  optionsAreImagePair,
  stripNeedsYouFromBoard
} from './needs-you-logic'
import type { KanbanBoard } from './types'

const item = (board: string, id: string): NeedsYouItem => ({
  ask: null,
  asked_at: null,
  asked_by: null,
  assignee: null,
  block_kind: 'needs_input',
  block_reason: 'pick one',
  board,
  board_name: board,
  legacy_reason: null,
  status: 'blocked',
  task_id: id,
  title: id
})

describe('isNeedsYouTask', () => {
  it('matches blocked + needs_input only', () => {
    expect(isNeedsYouTask({ block_kind: 'needs_input', status: 'blocked' })).toBe(true)
    expect(isNeedsYouTask({ block_kind: 'dependency', status: 'blocked' })).toBe(false)
    expect(isNeedsYouTask({ block_kind: 'needs_input', status: 'ready' })).toBe(false)
  })
})

describe('stripNeedsYouFromBoard', () => {
  it('removes needs_input cards from blocked and leaves other columns alone', () => {
    const board = {
      columns: [
        {
          name: 'ready',
          tasks: [{ block_kind: null, id: 't_ready', status: 'ready', title: 'r' }]
        },
        {
          name: 'blocked',
          tasks: [
            { block_kind: 'needs_input', id: 't_ask', status: 'blocked', title: 'ask' },
            { block_kind: 'dependency', id: 't_dep', status: 'blocked', title: 'dep' },
            { block_kind: 'needs_input', id: 't_ask2', status: 'blocked', title: 'ask2' }
          ]
        }
      ]
    } as unknown as KanbanBoard

    const next = stripNeedsYouFromBoard(board)
    const blocked = next.columns.find(c => c.name === 'blocked')!
    const ready = next.columns.find(c => c.name === 'ready')!

    expect(blocked.tasks.map(t => t.id)).toEqual(['t_dep'])
    expect(ready.tasks.map(t => t.id)).toEqual(['t_ready'])
  })
})

describe('countNeedsYouByBoard', () => {
  it('tallies open asks per board slug and defaults missing board to default', () => {
    expect(countNeedsYouByBoard([])).toEqual({})
    expect(
      countNeedsYouByBoard([item('alpha', 't1'), item('alpha', 't2'), item('beta', 't3'), item('', 't4')])
    ).toEqual({ alpha: 2, beta: 1, default: 1 })
  })
})

describe('optionsAreImagePair', () => {
  it('requires every option to carry an image or image_url', () => {
    expect(optionsAreImagePair(null)).toBe(false)
    expect(
      optionsAreImagePair({
        context: '',
        options: [
          { effect: '', key: 'a', label: 'A', image_url: '/a.png' },
          { effect: '', key: 'b', label: 'B', image: 'att_b' }
        ],
        pick: null,
        question: 'q'
      })
    ).toBe(true)
    expect(
      optionsAreImagePair({
        context: '',
        options: [
          { effect: '', key: 'a', label: 'A', image_url: '/a.png' },
          { effect: '', key: 'b', label: 'B' }
        ],
        pick: null,
        question: 'q'
      })
    ).toBe(false)
  })
})
