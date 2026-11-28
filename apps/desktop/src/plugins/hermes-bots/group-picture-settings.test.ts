import { describe, expect, it } from 'vitest'

import {
  cadenceCaptureDue,
  DEFAULT_GROUP_PICTURE_SETTINGS,
  normalizeGroupPictureSettings,
  PICTURE_FOLDER_SETTING,
  PICTURE_PRICE_UNMEASURED,
  PICTURE_SEND_MINUTE_CHOICES,
  pictureFolderOptionState,
  picturePriceLabel,
  shouldAttachFreshStill
} from './group-picture-settings'

describe('group picture settings (AC3, AC4, AC6)', () => {
  it('starts Off with privacy on, and only offers the measured minute choices', () => {
    expect(normalizeGroupPictureSettings(undefined)).toEqual(DEFAULT_GROUP_PICTURE_SETTINGS)
    expect(DEFAULT_GROUP_PICTURE_SETTINGS.privacyMode).toBe(true)
    expect(DEFAULT_GROUP_PICTURE_SETTINGS.sendMode).toBe('off')
    expect(DEFAULT_GROUP_PICTURE_SETTINGS.shareFolders).toBe(false)
    expect(PICTURE_SEND_MINUTE_CHOICES).toEqual([1, 2, 5, 10])
    expect(normalizeGroupPictureSettings({ sendEveryMinutes: 3 }).sendEveryMinutes).toBe(2)
  })

  it('attaches a fresh still only in every-message mode', () => {
    expect(shouldAttachFreshStill(normalizeGroupPictureSettings({ sendMode: 'every-message' }))).toBe(true)
    expect(shouldAttachFreshStill(normalizeGroupPictureSettings({ sendMode: 'tap' }))).toBe(false)
  })

  it('does not fire the webcam cadence while the room is hidden', () => {
    const settings = normalizeGroupPictureSettings({ sendMode: 'every-n-minutes', sendEveryMinutes: 1 })
    expect(cadenceCaptureDue(settings, 0, 60_000, true)).toBe(true)
    expect(cadenceCaptureDue(settings, 0, 60_000, false)).toBe(false)
  })

  it('greys the folder option and names the setting when no folders are configured', () => {
    expect(pictureFolderOptionState(null)).toEqual({
      available: false,
      settingName: PICTURE_FOLDER_SETTING
    })
    expect(pictureFolderOptionState(['S:/Camera'])).toEqual({
      available: true,
      settingName: PICTURE_FOLDER_SETTING
    })
  })

  it('never invents a price', () => {
    expect(picturePriceLabel(undefined)).toBe(PICTURE_PRICE_UNMEASURED)
    expect(picturePriceLabel({ perPicture: 4 })).toBe('4 Spark')
  })
})
