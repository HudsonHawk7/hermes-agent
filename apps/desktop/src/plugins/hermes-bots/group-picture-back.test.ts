import { describe, expect, it } from 'vitest'

import {
  nextPictureBackPrompt,
  PICTURE_BACK_INSTRUCTION,
  replyHasPicture,
  settlePicturesBack
} from './group-picture-back'
import { normalizeGroupPictureSettings } from './group-picture-settings'

describe('pictures back (AC5)', () => {
  const everyReply = normalizeGroupPictureSettings({ picturesBack: 'every-reply' })

  it('adds one instruction only when a picture is owed', () => {
    const owed = nextPictureBackPrompt('speak', everyReply, undefined, false)
    const quiet = nextPictureBackPrompt('speak', normalizeGroupPictureSettings(undefined), undefined, false)

    expect(owed.owed).toBe(true)
    expect(owed.prompt).toContain(PICTURE_BACK_INSTRUCTION)
    expect(quiet.owed).toBe(false)
    expect(quiet.prompt).toBe('speak')
  })

  it('asks once more, then badges, and never asks a third time', () => {
    const first = settlePicturesBack(undefined, true, false)
    expect(first.askAgain).toBe(true)
    expect(first.badge).toBe(false)

    const second = settlePicturesBack(first.entry, true, false)
    expect(second.askAgain).toBe(false)
    expect(second.badge).toBe(true)

    const third = settlePicturesBack(second.entry, true, false)
    expect(third.askAgain).toBe(true)
    expect(third.badge).toBe(false)
  })

  it('treats MEDIA and a data image as a picture, and plain text as none', () => {
    expect(replyHasPicture('here MEDIA:/S:/renders/scene.png')).toBe(true)
    expect(replyHasPicture('data:image/png;base64,aaaa')).toBe(true)
    expect(replyHasPicture('just words')).toBe(false)
  })
})
