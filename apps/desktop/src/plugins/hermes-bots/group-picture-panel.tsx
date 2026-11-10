/**
 * "My pictures" panel. Settings are per room. Privacy stays ON unless the
 * user turns it off. The folder checkbox is grey, and names the setting,
 * when desktop.group_chat_picture_dirs is unset.
 */

import { Button } from '@hermes/plugin-sdk'

import {
  type GroupPictureSettings,
  PICTURE_BACK_TURN_CHOICES,
  PICTURE_SEND_MINUTE_CHOICES,
  type PictureBackMode,
  pictureFolderOptionState,
  picturePriceLabel,
  type PictureSendMode
} from './group-picture-settings'

export function GroupPicturePanel({
  settings,
  pictureDirs,
  priceTable,
  open,
  onToggle,
  onChange
}: {
  settings: GroupPictureSettings
  pictureDirs: readonly string[] | null
  priceTable: unknown
  open: boolean
  onToggle: () => void
  onChange: (patch: Partial<GroupPictureSettings>) => void
}) {
  const folders = pictureFolderOptionState(pictureDirs)
  const price = picturePriceLabel(priceTable)
  const backOn = settings.picturesBack !== 'off'

  return (
    <div className="relative">
      <Button
        aria-expanded={open}
        aria-label="My pictures"
        data-testid="group-my-pictures"
        onClick={onToggle}
        size="sm"
        variant="ghost"
      >
        📷 My pictures
      </Button>
      {open ? (
        <div
          className="absolute right-0 top-full z-30 mt-1 w-[28rem] max-w-[90vw] rounded-md border border-[var(--border)] bg-[var(--background)] p-3 text-left text-xs shadow-md"
          data-testid="group-picture-panel"
          role="dialog"
        >
          <p data-testid="group-privacy-line">
            {settings.privacyMode
              ? 'PRIVACY MODE: ON — pictures go only into this chat; no copy saved anywhere on the computer'
              : 'PRIVACY MODE: OFF — a copy may stay on disk after the turn'}
          </p>
          <fieldset className="mt-2">
            <legend>MY PICTURES Send</legend>
            <SendChoice
              current={settings.sendMode}
              label="Off"
              onChange={onChange}
              value="off"
            />
            <SendChoice
              current={settings.sendMode}
              label="Only when I tap 📸 or 🖥"
              onChange={onChange}
              value="tap"
            />
            <SendChoice
              current={settings.sendMode}
              label="With every message I send"
              onChange={onChange}
              value="every-message"
            />
            <label className="mt-1 flex items-center gap-2">
              <input
                checked={settings.sendMode === 'every-n-minutes'}
                name="picture-send"
                onChange={() => onChange({ sendMode: 'every-n-minutes' })}
                type="radio"
              />
              Every
              <select
                aria-label="Minutes between webcam pictures"
                onChange={event =>
                  onChange({
                    sendMode: 'every-n-minutes',
                    sendEveryMinutes: Number(event.target.value) as GroupPictureSettings['sendEveryMinutes']
                  })
                }
                value={settings.sendEveryMinutes}
              >
                {PICTURE_SEND_MINUTE_CHOICES.map(minutes => (
                  <option key={minutes} value={minutes}>
                    {minutes}
                  </option>
                ))}
              </select>
              minutes (webcam)
            </label>
            <label className="mt-2 flex items-start gap-2" title={folders.available ? undefined : folders.settingName}>
              <input
                checked={folders.available && settings.shareFolders}
                data-testid="group-share-folders"
                disabled={!folders.available}
                onChange={event => onChange({ shareFolders: event.target.checked })}
                type="checkbox"
              />
              <span>
                Also share new files from my Screenshots + Camera Roll folders
                {folders.available ? ' (off = private)' : ` — set ${folders.settingName}`}
              </span>
            </label>
          </fieldset>
          <fieldset className="mt-2">
            <legend>PICTURES BACK FROM THEM</legend>
            <BackChoice current={settings.picturesBack} label="Off" onChange={onChange} value="off" />
            <BackChoice
              current={settings.picturesBack}
              label="When I send one"
              onChange={onChange}
              value="when-i-send"
            />
            <label className="mt-1 flex items-center gap-2">
              <input
                checked={settings.picturesBack === 'every-n-turns'}
                name="picture-back"
                onChange={() => onChange({ picturesBack: 'every-n-turns' })}
                type="radio"
              />
              Every
              <select
                aria-label="Turns between pictures back"
                onChange={event =>
                  onChange({
                    picturesBack: 'every-n-turns',
                    picturesBackEveryTurns: Number(event.target.value) as GroupPictureSettings['picturesBackEveryTurns']
                  })
                }
                value={settings.picturesBackEveryTurns}
              >
                {PICTURE_BACK_TURN_CHOICES.map(turns => (
                  <option key={turns} value={turns}>
                    {turns}
                  </option>
                ))}
              </select>
              turns
            </label>
            <BackChoice
              current={settings.picturesBack}
              label="Every reply"
              onChange={onChange}
              value="every-reply"
            />
            {backOn ? (
              <p data-testid="group-picture-price">Price per picture: {price}</p>
            ) : null}
            <p>Skipped it? Asked once more, then her line shows "no picture".</p>
          </fieldset>
        </div>
      ) : null}
    </div>
  )
}

function SendChoice({
  current,
  value,
  label,
  onChange
}: {
  current: PictureSendMode
  value: PictureSendMode
  label: string
  onChange: (patch: Partial<GroupPictureSettings>) => void
}) {
  return (
    <label className="mt-1 flex items-center gap-2">
      <input
        checked={current === value}
        name="picture-send"
        onChange={() => onChange({ sendMode: value })}
        type="radio"
      />
      {label}
    </label>
  )
}

function BackChoice({
  current,
  value,
  label,
  onChange
}: {
  current: PictureBackMode
  value: PictureBackMode
  label: string
  onChange: (patch: Partial<GroupPictureSettings>) => void
}) {
  return (
    <label className="mt-1 flex items-center gap-2">
      <input
        checked={current === value}
        name="picture-back"
        onChange={() => onChange({ picturesBack: value })}
        type="radio"
      />
      {label}
    </label>
  )
}
