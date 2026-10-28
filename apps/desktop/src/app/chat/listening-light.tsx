/**
 * VH8 — always-visible listening light + one-click stop in the chat header.
 *
 * Driven by $voiceHub (voice_hub.state). Uses the shared working accent glow
 * (same --ui-accent treatment as the session working dot), never a bespoke hex.
 */
import { useStore } from '@nanostores/react'
import { Square } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Tip } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import {
  $voiceHub,
  isVoiceHubFailure,
  isVoiceHubListening,
  stopVoiceHubListening
} from '@/store/voice-hub'
import { $wakeWord, stopClientCapture } from '@/store/wake-word'

/** Shared active-state glow: accent fill + soft accent halo (token-only, no hard hex). */
const LISTENING_GLOW =
  'bg-(--ui-accent)/15 text-(--ui-accent) shadow-[0_0_0_1px_color-mix(in_oklab,var(--ui-accent)_55%,transparent),0_0_12px_color-mix(in_oklab,var(--ui-accent)_45%,transparent)]'

const FAILURE_TONE = 'bg-destructive/10 text-destructive border border-destructive/30'

function reasonLabel(reason: string | null): string {
  if (!reason) {
    return 'stopped'
  }

  switch (reason) {
    case 'start_failed':
      return 'start failed'

    case 'capture_failed':
      return 'capture failed'

    case 'reclaimed':
      return 'reclaimed'

    case 'stop_failed':
      return 'stop failed'

    default:
      return reason.replace(/_/g, ' ')
  }
}

export function ListeningLight({ className }: { className?: string }) {
  const hub = useStore($voiceHub)
  const listening = isVoiceHubListening(hub)
  const failed = isVoiceHubFailure(hub)

  if (!listening && !failed) {
    return null
  }

  if (failed) {
    const label = `Mic: ${reasonLabel(hub.stopReason)}`

    return (
      <div
        aria-live="polite"
        className={cn(
          'inline-flex h-7 max-w-[14rem] items-center gap-1.5 rounded-full px-2.5 text-xs font-medium',
          FAILURE_TONE,
          className
        )}
        data-state="failed"
        data-stop-reason={hub.stopReason ?? ''}
        data-testid="listening-light"
        role="status"
        title={label}
      >
        <span className="size-1.5 shrink-0 rounded-full bg-destructive" />
        <span className="truncate">{label}</span>
      </div>
    )
  }

  const label = 'Listening'

  return (
    <div
      aria-live="polite"
      className={cn(
        'inline-flex h-7 items-center gap-1.5 rounded-full pl-2.5 pr-1 text-xs font-medium',
        LISTENING_GLOW,
        className
      )}
      data-hub-status={hub.status}
      data-state="listening"
      data-testid="listening-light"
      role="status"
      title={label}
    >
      <span
        aria-hidden
        className="size-1.5 shrink-0 rounded-full bg-(--ui-accent) shadow-[0_0_6px_color-mix(in_oklab,var(--ui-accent)_70%,transparent)]"
      />
      <span className="font-medium tracking-wide uppercase">{label}</span>
      <Tip label="Stop listening" placement="control">
        <Button
          aria-label="Stop listening"
          className={cn(
            'size-6 shrink-0 rounded-full p-0',
            'text-(--ui-accent) hover:bg-(--ui-accent)/20 hover:text-(--ui-accent)'
          )}
          data-testid="listening-light-stop"
          disabled={hub.pending}
          onClick={() => {
            // One Hub path only: wake.stop via stopVoiceHubListening. Also drop the
            // client feeder and mirror the composer ear so the two UIs stay honest.
            stopClientCapture()
            const ear = $wakeWord.get()
            $wakeWord.set({
              ...ear,
              enabled: false,
              listening: false,
              notice: '',
              pending: false
            })
            void stopVoiceHubListening(undefined, { persist: true, reason: 'user_stop' })
          }}
          size="icon"
          type="button"
          variant="ghost"
        >
          <Square className="size-2.5 fill-current" />
        </Button>
      </Tip>
    </div>
  )
}
