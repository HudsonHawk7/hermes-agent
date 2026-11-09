// Chat media actions — a host slot a plugin can register into.
//
// The host owns the hover/tap chrome on a picture or clip in a message.
// A plugin registers a data contribution (label, icon, handler). It does not
// patch the chat renderer. Scene Builder's Refine button is one such plugin;
// this file is the slot, not the button.
//
// Area id: `chat.mediaActions` (MEDIA_ACTION_AREA).
//
//   ctx.register({
//     id: 'refine',
//     area: 'chat.mediaActions',
//     data: {
//       label: 'Refine',
//       icon: 'sparkle',          // codicon name, optional
//       when: ctx => ctx.profile !== 'solara',
//       run: ctx => { /* path, kind, sessionId, profile */ }
//     }
//   })
//
// The host calls `when` and `run` with the picture or clip the user is on:
// the original media path (not a blob URL), image|video, the chat's session
// id, and that chat's profile (the row's owner, else the active profile).

import { normalizeProfileKey } from '@/store/profile'
import { knownSessionProfile } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

export const MEDIA_ACTION_AREA = 'chat.mediaActions'

export type ChatMediaKind = 'image' | 'video'

export interface ChatMediaActionContext {
  /** Original media path or URL. Not the blob URL the player uses. */
  path: string
  kind: ChatMediaKind
  /** Runtime session id when the chat has one, else the stored id, else ''. */
  sessionId: string
  /** Profile that owns this chat. Never the gateway home when the row names another. */
  profile: string
}

export interface ChatMediaAction {
  label: string
  /** Codicon name. Omitted actions still show the label. */
  icon?: string
  /** Return false to hide this action on this item. Called at render. */
  when?: (ctx: ChatMediaActionContext) => boolean
  run: (ctx: ChatMediaActionContext) => void | Promise<void>
}

/** Pull a usable action out of a contribution's data. Bad rows are skipped. */
export function chatMediaActionFrom(data: unknown): ChatMediaAction | null {
  if (!data || typeof data !== 'object') {
    return null
  }

  const row = data as Partial<ChatMediaAction>
  const label = typeof row.label === 'string' ? row.label.trim() : ''

  if (!label || typeof row.run !== 'function') {
    return null
  }

  return {
    label,
    icon: typeof row.icon === 'string' && row.icon.trim() ? row.icon.trim() : undefined,
    when: typeof row.when === 'function' ? row.when : undefined,
    run: row.run
  }
}

/**
 * Profile of the chat this media sits in. Prefer the session row (stored id,
 * then runtime id). Fall back to the active profile only when neither row
 * knows — a single-profile chat has no tag.
 */
export function chatMediaProfile(
  sessions: readonly SessionInfo[],
  storedId: string | null,
  runtimeId: string | null,
  activeProfile: string | null
): string {
  return (
    knownSessionProfile(sessions, storedId) ||
    knownSessionProfile(sessions, runtimeId) ||
    normalizeProfileKey(activeProfile)
  )
}

/** Session id a plugin should see: the live runtime id, else the stored id. */
export function chatMediaSessionId(runtimeId: string | null, storedId: string | null): string {
  return (runtimeId || storedId || '').trim()
}
