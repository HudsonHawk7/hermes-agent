import { describe, expect, it } from 'vitest'

import {
  CAP,
  clampText,
  cliffNotesFacts,
  type CommentLike,
  countNeedsYouByBoard,
  displayNeedsYouTitle,
  foldRepeats,
  type GmAsk,
  isNeedsYouTask,
  type NeedsYouItem,
  optionsAreImagePair,
  optionSpansFull,
  recommendedOption,
  showRecommend,
  sortCommentsNewestFirst,
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

function ask(partial: Partial<GmAsk> = {}): GmAsk {
  return {
    question: 'Ship cliff notes?',
    context: 'First sentence. Second sentence.',
    options: [
      { key: 'yes', label: 'Yes', effect: 'build it' },
      { key: 'no', label: 'No', effect: 'park it' }
    ],
    pick: 'yes',
    ...partial
  }
}

function c(id: number, author: string, body: string, created_at: number): CommentLike {
  return { id, author, body, created_at }
}

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

describe('clampText', () => {
  it('keeps short text', () => {
    expect(clampText('hello', 10)).toBe('hello')
  })

  it('clamps with ellipsis at the cap', () => {
    const s = 'x'.repeat(120)
    const out = clampText(s, CAP.fact)
    expect(out.length).toBe(CAP.fact)
    expect(out.endsWith('…')).toBe(true)
  })
})

describe('cliffNotesFacts', () => {
  it('prefers ask.facts and clamps each to 110', () => {
    const long = 'f'.repeat(150)
    const out = cliffNotesFacts(ask({ facts: ['one', long, 'three', 'four'] }))
    expect(out).toHaveLength(3)
    expect(out[0]).toBe('one')
    expect(out[1]!.length).toBe(CAP.fact)
    expect(out[2]).toBe('three')
  })

  it('falls back to sentence 1 of context cut at 110 when facts absent', () => {
    const long = `${'a'.repeat(200)}. Second stays hidden.`
    const out = cliffNotesFacts(ask({ facts: undefined, context: long }))
    expect(out).toHaveLength(1)
    expect(out[0]!.length).toBe(CAP.fact)
    expect(out[0]!.startsWith('a')).toBe(true)
  })

  it('returns empty when neither facts nor context', () => {
    expect(cliffNotesFacts(ask({ facts: [], context: '' }))).toEqual([])
  })
})

describe('showRecommend / recommendedOption', () => {
  it('pick null → no Recommend', () => {
    const a = ask({ pick: null })
    expect(showRecommend(a)).toBe(false)
    expect(recommendedOption(a)).toBeNull()
  })

  it('pick set → Recommend resolves option', () => {
    const a = ask({ pick: 'no' })
    expect(showRecommend(a)).toBe(true)
    expect(recommendedOption(a)?.label).toBe('No')
  })
})

describe('sortCommentsNewestFirst', () => {
  it('orders newest first and stays stable on equal stamps', () => {
    const comments = [
      c(1, 'a', 'old', 10),
      c(2, 'a', 'mid-a', 20),
      c(3, 'b', 'mid-b', 20),
      c(4, 'a', 'new', 30)
    ]

    expect(sortCommentsNewestFirst(comments).map(x => x.id)).toEqual([4, 2, 3, 1])
  })
})

describe('foldRepeats', () => {
  it('two identical do NOT fold', () => {
    const comments = sortCommentsNewestFirst([
      c(1, 'hera', 'same', 1),
      c(2, 'hera', 'same', 2)
    ])

    const folded = foldRepeats(comments)
    expect(folded).toHaveLength(2)
    expect(folded.every(r => r.kind === 'single')).toBe(true)
  })

  it('three+ identical same-author fold to one row with count + last time', () => {
    const comments = sortCommentsNewestFirst([
      c(1, 'hera', 'still waiting on the design note', 100),
      c(2, 'hera', 'still waiting on the design note', 200),
      c(3, 'hera', 'still waiting on the design note', 300),
      c(4, 'hera', 'still waiting on the design note', 400)
    ])

    const folded = foldRepeats(comments)
    expect(folded).toHaveLength(1)
    expect(folded[0]).toMatchObject({
      kind: 'fold',
      author: 'hera',
      body: 'still waiting on the design note',
      count: 4,
      lastAt: 400
    })
  })

  it('trailing-space sweep posts fold (trim match)', () => {
    const comments = sortCommentsNewestFirst([
      c(1, 'hera', 'ping  ', 1),
      c(2, 'hera', 'ping', 2),
      c(3, 'hera', ' ping', 3)
    ])

    const folded = foldRepeats(comments)
    expect(folded).toHaveLength(1)
    expect(folded[0]).toMatchObject({ kind: 'fold', count: 3, body: 'ping' })
  })

  it('different authors with same body do not fold together', () => {
    const comments = sortCommentsNewestFirst([
      c(1, 'hera', 'same', 1),
      c(2, 'probe', 'same', 2),
      c(3, 'hera', 'same', 3)
    ])

    expect(foldRepeats(comments).every(r => r.kind === 'single')).toBe(true)
  })

  it('25-comment fixture → 14 rows with one ×12 fold', () => {
    const stamps: CommentLike[] = []

    for (let i = 0; i < 12; i++) {
      stamps.push(c(100 + i, 'hera', 'still waiting on the design note', 1000 + i))
    }

    const others: CommentLike[] = [
      c(1, 'architect', 'mockup v1 attached', 10),
      c(2, 'critiquo', 'contrast fail on amber', 20),
      c(3, 'hera', 'rewrote the strip', 30),
      c(4, 'probe', 'e2e seed ready', 40),
      c(5, 'jocasta', 'AC look good', 50),
      c(6, 'darkeye', 'carry branch ok', 60),
      c(7, 'sabe', 'i18n keys added', 70),
      c(8, 'voss', 'pytest green', 80),
      c(9, 'aura', 'vitest green', 90),
      c(10, 'hera', 'details starts closed', 110),
      c(11, 'hera', 'four-option grid', 120),
      c(12, 'probe', 'tab to fold', 130),
      c(13, 'gm', 'yes build it', 140)
    ]

    const all = sortCommentsNewestFirst([...stamps, ...others])
    expect(all).toHaveLength(25)
    const folded = foldRepeats(all)
    expect(folded).toHaveLength(14)
    const fold = folded.find(r => r.kind === 'fold')
    expect(fold).toMatchObject({ kind: 'fold', count: 12 })
  })
})

describe('optionSpansFull', () => {
  it('3-option third spans both columns', () => {
    expect(optionSpansFull(3, 0)).toBe(false)
    expect(optionSpansFull(3, 1)).toBe(false)
    expect(optionSpansFull(3, 2)).toBe(true)
  })

  it('2 and 4 never span', () => {
    expect(optionSpansFull(2, 0)).toBe(false)
    expect(optionSpansFull(4, 3)).toBe(false)
  })
})

describe('displayNeedsYouTitle', () => {
  it('strips a leading GM yes/no: prefix (any case)', () => {
    expect(displayNeedsYouTitle('GM yes/no: Ship the cliff notes strip?')).toBe('Ship the cliff notes strip?')
    expect(displayNeedsYouTitle('gm YES/NO:  Keep the old card?')).toBe('Keep the old card?')
  })

  it('leaves titles without the prefix alone', () => {
    expect(displayNeedsYouTitle('Ordinary blocked card')).toBe('Ordinary blocked card')
  })

  it('falls back when empty or prefix-only', () => {
    expect(displayNeedsYouTitle('')).toBe('Needs a decision')
    expect(displayNeedsYouTitle('GM yes/no:')).toBe('Needs a decision')
  })
})

