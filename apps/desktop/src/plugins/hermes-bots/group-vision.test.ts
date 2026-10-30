import { afterEach, describe, expect, it } from 'vitest'

import {
  buildGroupVisionReceipt,
  countGroupImageAttachments,
  groupImageAttachmentLabels,
  stashMemberTurnVision,
  takeMemberTurnVision
} from './group-vision'
import type { Attachment } from './types'

const img = (name: string): Attachment => ({
  data: 'data:image/png;base64,aaa',
  kind: 'image',
  name
})

const pdf = (name: string): Attachment => ({
  data: 'data:application/pdf;base64,bbb',
  kind: 'pdf',
  name
})

describe('countGroupImageAttachments', () => {
  it('counts image tiles only', () => {
    expect(countGroupImageAttachments([img('a.png'), pdf('x.pdf'), img('b.png')])).toBe(2)
    expect(countGroupImageAttachments([])).toBe(0)
    expect(countGroupImageAttachments(null)).toBe(0)
  })
})

describe('groupImageAttachmentLabels', () => {
  it('labels image tiles', () => {
    expect(groupImageAttachmentLabels([img('shot.png'), pdf('doc.pdf')])).toEqual(['shot.png'])
  })
})

describe('buildGroupVisionReceipt', () => {
  it('returns undefined when the turn carried no images', () => {
    expect(buildGroupVisionReceipt({ imageCount: 0 })).toBeUndefined()
  })

  it('shows saw for native routing with attached images', () => {
    expect(
      buildGroupVisionReceipt({
        imageCount: 1,
        routing: { mode: 'native', count: 1, attached: 1, reason: 'model supports vision' }
      })
    ).toEqual({
      count: 1,
      mode: 'native',
      saw: true,
      attached: 1,
      reason: 'model supports vision'
    })
  })

  it('shows miss when routing fell back to text', () => {
    const receipt = buildGroupVisionReceipt({
      imageCount: 2,
      routing: { mode: 'text', count: 2, reason: 'model lacks vision', attached: 0 }
    })

    expect(receipt?.saw).toBe(false)
    expect(receipt?.mode).toBe('text')
    expect(receipt?.reason).toContain('model lacks vision')
  })

  it('shows miss when every image attach failed and no routing arrived', () => {
    const receipt = buildGroupVisionReceipt({
      imageCount: 1,
      failedImageLabels: ['shot.png']
    })

    expect(receipt).toEqual({
      count: 1,
      mode: 'text',
      saw: false,
      reason: 'attach failed: shot.png'
    })
  })

  it('never invents saw when images existed but gateway sent no routing', () => {
    expect(
      buildGroupVisionReceipt({
        imageCount: 1,
        routing: null
      })
    ).toBeUndefined()
  })

  it('native with attached 0 is a miss', () => {
    const receipt = buildGroupVisionReceipt({
      imageCount: 1,
      routing: { mode: 'native', attached: 0, reason: 'parts dropped' }
    })

    expect(receipt?.saw).toBe(false)
    expect(receipt?.reason).toContain('parts dropped')
  })
})

describe('stash/takeMemberTurnVision', () => {
  afterEach(() => {
    takeMemberTurnVision('m1')
    takeMemberTurnVision('m2')
  })

  it('hands the receipt once to the round commit path', () => {
    stashMemberTurnVision(
      'm1',
      buildGroupVisionReceipt({
        imageCount: 1,
        routing: { mode: 'native', attached: 1 }
      })
    )

    expect(takeMemberTurnVision('m1')?.saw).toBe(true)
    expect(takeMemberTurnVision('m1')).toBeUndefined()
  })

  it('clears when stashing undefined', () => {
    stashMemberTurnVision('m2', {
      count: 1,
      mode: 'text',
      saw: false,
      reason: 'x'
    })
    stashMemberTurnVision('m2', undefined)
    expect(takeMemberTurnVision('m2')).toBeUndefined()
  })
})
