/**
 * Picture settings for one group chat.
 *
 * A new room starts with every send option Off and privacy ON. Nothing here
 * invents a price: an unmeasured table stays "not measured yet".
 */

export const PICTURE_SEND_MINUTE_CHOICES = [1, 2, 5, 10] as const
export const PICTURE_BACK_TURN_CHOICES = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10] as const
export const PICTURE_FOLDER_SETTING = 'desktop.group_chat_picture_dirs'
export const PICTURE_PRICE_UNMEASURED = 'not measured yet'

export type PictureSendMode = 'off' | 'tap' | 'every-message' | 'every-n-minutes'
export type PictureBackMode = 'off' | 'when-i-send' | 'every-n-turns' | 'every-reply'

export interface GroupPictureSettings {
  /** ON means the picture is for this chat only. No copy is kept after the turn. */
  privacyMode: boolean
  sendMode: PictureSendMode
  sendEveryMinutes: (typeof PICTURE_SEND_MINUTE_CHOICES)[number]
  shareFolders: boolean
  picturesBack: PictureBackMode
  picturesBackEveryTurns: (typeof PICTURE_BACK_TURN_CHOICES)[number]
}

export const DEFAULT_GROUP_PICTURE_SETTINGS: GroupPictureSettings = {
  privacyMode: true,
  sendMode: 'off',
  sendEveryMinutes: 2,
  shareFolders: false,
  picturesBack: 'off',
  picturesBackEveryTurns: 3
}

function pickChoice<T extends number>(value: unknown, choices: readonly T[], fallback: T): T {
  const n = Number(value)

  return (choices as readonly number[]).includes(n) ? (n as T) : fallback
}

export function normalizeGroupPictureSettings(raw: unknown): GroupPictureSettings {
  const source = raw && typeof raw === 'object' ? (raw as Partial<GroupPictureSettings>) : {}
  const sendMode = source.sendMode
  const picturesBack = source.picturesBack

  return {
    privacyMode: source.privacyMode !== false,
    sendMode:
      sendMode === 'tap' || sendMode === 'every-message' || sendMode === 'every-n-minutes' || sendMode === 'off'
        ? sendMode
        : 'off',
    sendEveryMinutes: pickChoice(source.sendEveryMinutes, PICTURE_SEND_MINUTE_CHOICES, 2),
    shareFolders: source.shareFolders === true,
    picturesBack:
      picturesBack === 'when-i-send' ||
      picturesBack === 'every-n-turns' ||
      picturesBack === 'every-reply' ||
      picturesBack === 'off'
        ? picturesBack
        : 'off',
    picturesBackEveryTurns: pickChoice(source.picturesBackEveryTurns, PICTURE_BACK_TURN_CHOICES, 3)
  }
}

/** A timed webcam still only while the room is open and the page is visible. */
export function cadenceCaptureDue(
  settings: GroupPictureSettings,
  lastCaptureAt: number | null,
  now: number,
  roomVisible: boolean
): boolean {
  if (!roomVisible || settings.sendMode !== 'every-n-minutes') {return false}
  const minutes = pickChoice(settings.sendEveryMinutes, PICTURE_SEND_MINUTE_CHOICES, 2)

  if (lastCaptureAt == null) {return true}

  return now - lastCaptureAt >= minutes * 60_000
}

export function shouldAttachFreshStill(settings: GroupPictureSettings): boolean {
  return settings.sendMode === 'every-message'
}

export function pictureDirsFromConfig(record: unknown): string[] {
  return readPictureDirsFromConfig(record) || []
}

export function pictureFolderOptionState(dirs: readonly string[] | null | undefined): {
  available: boolean
  settingName: string
} {
  return {
    available: Array.isArray(dirs) && dirs.length > 0,
    settingName: PICTURE_FOLDER_SETTING
  }
}

/** Unset means the checkbox stays grey and the setting is named. */
function readPictureDirsFromConfig(config: unknown): string[] | null {
  if (!config || typeof config !== 'object') {return null}
  const desktop = (config as { desktop?: unknown }).desktop

  if (!desktop || typeof desktop !== 'object') {return null}
  const raw = (desktop as { group_chat_picture_dirs?: unknown }).group_chat_picture_dirs

  if (!Array.isArray(raw)) {return null}
  const dirs = raw.filter((item): item is string => typeof item === 'string' && item.trim().length > 0)

  return dirs.length ? dirs : null
}

/**
 * Echo a measured per-picture price. A missing or blank table is
 * "not measured yet" — never a made-up number.
 */
export function picturePriceLabel(table: unknown): string {
  if (!table || typeof table !== 'object') {return PICTURE_PRICE_UNMEASURED}
  const record = table as { perPicture?: unknown; per_picture?: unknown }
  const value = record.perPicture ?? record.per_picture

  if (typeof value === 'number' && Number.isFinite(value)) {return `${value} Spark`}

  if (typeof value === 'string') {
    const trimmed = value.trim()

    if (trimmed && trimmed !== PICTURE_PRICE_UNMEASURED) {return trimmed}
  }

  return PICTURE_PRICE_UNMEASURED
}
