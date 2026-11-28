/**
 * Pictures back: one instruction on an owed turn, one retry, then a badge.
 * She chooses a new picture or one she already made. We never invent one.
 */

import type { GroupPictureSettings } from './group-picture-settings'

export const PICTURE_BACK_INSTRUCTION =
  'include one picture this turn: make a new one or show one you already made, your choice'

export interface PicturesBackLedgerEntry {
  turnsSincePicture: number
  /** True after the one allowed retry was already asked for this owed turn. */
  retried: boolean
}

export function replyHasPicture(text: string | null | undefined): boolean {
  if (!text) {return false}

  return /(?:^|\s)MEDIA:\s*\S+/i.test(text) || /data:image\//i.test(text)
}

export function pictureBackOwed(
  settings: GroupPictureSettings,
  entry: PicturesBackLedgerEntry | undefined,
  userSentPicture: boolean
): boolean {
  if (settings.picturesBack === 'off') {return false}

  if (settings.picturesBack === 'every-reply') {return true}

  if (settings.picturesBack === 'when-i-send') {return userSentPicture}
  const since = entry?.turnsSincePicture ?? 0

  return since + 1 >= settings.picturesBackEveryTurns
}

export function promptWithPictureBack(prompt: string, owed: boolean): string {
  if (!owed) {return prompt}

  return `${prompt}\n\n${PICTURE_BACK_INSTRUCTION}`
}

export function nextPictureBackPrompt(
  prompt: string,
  settings: GroupPictureSettings,
  entry: PicturesBackLedgerEntry | undefined,
  userSentPicture: boolean
): { prompt: string; owed: boolean } {
  const owed = pictureBackOwed(settings, entry, userSentPicture)

  return { prompt: promptWithPictureBack(prompt, owed), owed }
}

export interface PicturesBackSettle {
  entry: PicturesBackLedgerEntry
  askAgain: boolean
  badge: boolean
}

/**
 * Settle one owed-or-not turn. The caller must persist `entry` between the
 * first reply and the single retry, so a second miss badges instead of looping.
 */
export function settlePicturesBack(
  entry: PicturesBackLedgerEntry | undefined,
  owed: boolean,
  hasPicture: boolean
): PicturesBackSettle {
  const current: PicturesBackLedgerEntry = {
    turnsSincePicture: entry?.turnsSincePicture ?? 0,
    retried: entry?.retried === true
  }

  if (!owed) {
    return {
      entry: { turnsSincePicture: current.turnsSincePicture + 1, retried: false },
      askAgain: false,
      badge: false
    }
  }

  if (hasPicture) {
    return { entry: { turnsSincePicture: 0, retried: false }, askAgain: false, badge: false }
  }

  if (!current.retried) {
    return {
      entry: { turnsSincePicture: current.turnsSincePicture, retried: true },
      askAgain: true,
      badge: false
    }
  }

  return { entry: { turnsSincePicture: 0, retried: false }, askAgain: false, badge: true }
}
