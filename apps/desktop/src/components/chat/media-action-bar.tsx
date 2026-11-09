'use client'

import { useStore } from '@nanostores/react'

import {
  type ChatMediaAction,
  type ChatMediaActionContext,
  chatMediaActionFrom,
  type ChatMediaKind,
  chatMediaProfile,
  chatMediaSessionId,
  MEDIA_ACTION_AREA
} from '@/app/chat/media-actions'
import { useSessionView } from '@/app/chat/session-view'
import { Codicon } from '@/components/ui/codicon'
import { useContributions } from '@/contrib/react/use-contributions'
import { cn } from '@/lib/utils'
import { $activeGatewayProfile } from '@/store/profile'
import { $sessions } from '@/store/session'

/**
 * Host chrome for plugin media actions. Renders nothing until a plugin
 * registers into MEDIA_ACTION_AREA. Must sit inside a `group/image` so hover
 * (and a tap on a device with no hover) reveals the buttons. The parent owns
 * the picture or clip; this only paints the actions.
 */
export function ChatMediaActionBar({
  kind,
  path
}: {
  kind: ChatMediaKind
  path: string
}) {
  const contributions = useContributions(MEDIA_ACTION_AREA)
  const view = useSessionView()
  const runtimeId = useStore(view.$runtimeId)
  const storedId = useStore(view.$storedId)
  const sessions = useStore($sessions)
  const activeProfile = useStore($activeGatewayProfile)
  const mediaPath = path.trim()

  if (!mediaPath || contributions.length === 0) {
    return null
  }

  const ctx: ChatMediaActionContext = {
    path: mediaPath,
    kind,
    sessionId: chatMediaSessionId(runtimeId, storedId),
    profile: chatMediaProfile(sessions, storedId, runtimeId, activeProfile)
  }

  const actions = contributions.flatMap(contribution => {
    const action = chatMediaActionFrom(contribution.data)

    if (!action || !actionAllowed(action, ctx)) {
      return []
    }

    return [{ id: contribution.id, action }]
  })

  if (actions.length === 0) {
    return null
  }

  return (
    <span
      className={cn(
        'absolute left-2 top-2 z-10 flex max-w-[calc(100%-4.5rem)] flex-wrap gap-1',
        'opacity-0 transition-opacity',
        'group-hover/image:opacity-100 group-focus-within/image:opacity-100',
        '[@media(hover:none)]:opacity-100'
      )}
      data-slot="chat-media-actions"
    >
      {actions.map(({ id, action }) => (
        <button
          aria-label={action.label}
          className="inline-flex h-8 items-center gap-1 rounded-full border border-border/70 bg-background/80 px-2 text-xs text-muted-foreground shadow-sm backdrop-blur hover:bg-accent hover:text-foreground focus-visible:opacity-100"
          data-media-action={id}
          key={id}
          onClick={event => {
            event.preventDefault()
            event.stopPropagation()
            void action.run(ctx)
          }}
          onPointerDown={event => event.stopPropagation()}
          type="button"
        >
          {action.icon ? <Codicon name={action.icon} size={14} /> : null}
          <span>{action.label}</span>
        </button>
      ))}
    </span>
  )
}

function actionAllowed(action: ChatMediaAction, ctx: ChatMediaActionContext): boolean {
  if (!action.when) {
    return true
  }

  try {
    return action.when(ctx) !== false
  } catch {
    return false
  }
}
