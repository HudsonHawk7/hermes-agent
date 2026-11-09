import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, describe, expect, it } from 'vitest'

import { chatMediaActionFrom, chatMediaProfile, chatMediaSessionId } from '@/app/chat/media-actions'
import { PRIMARY_SESSION_VIEW, type SessionView, SessionViewProvider } from '@/app/chat/session-view'
import { ChatMediaActionBar } from '@/components/chat/media-action-bar'
import { registry } from '@/contrib/registry'
import { $activeGatewayProfile } from '@/store/profile'
import { $sessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

const disposers: Array<() => void> = []

afterEach(() => {
  cleanup()
  disposers.splice(0).forEach(dispose => dispose())
  $sessions.set([])
  $activeGatewayProfile.set('default')
})

function view(runtimeId: string, storedId: string): SessionView {
  return {
    ...PRIMARY_SESSION_VIEW,
    $runtimeId: atom(runtimeId),
    $storedId: atom(storedId)
  }
}

function row(id: string, profile: string): SessionInfo {
  return { id, profile } as SessionInfo
}

describe('chat media action helpers', () => {
  it('keeps a labeled handler and drops a row that cannot run', () => {
    const run = () => undefined

    expect(chatMediaActionFrom({ label: ' Refine ', icon: ' sparkle ', run })?.label).toBe('Refine')
    expect(chatMediaActionFrom({ label: 'Refine' })).toBeNull()
    expect(chatMediaActionFrom(null)).toBeNull()
  })

  it('uses the chat row profile, not the gateway home', () => {
    const sessions = [row('stored-probe', 'probe')]

    expect(chatMediaProfile(sessions, 'stored-probe', 'runtime-probe', 'default')).toBe('probe')
    expect(chatMediaProfile([], null, null, 'default')).toBe('default')
    expect(chatMediaSessionId('runtime-probe', 'stored-probe')).toBe('runtime-probe')
    expect(chatMediaSessionId(null, 'stored-probe')).toBe('stored-probe')
  })
})

describe('ChatMediaActionBar', () => {
  it('shows a registered action and passes path, session id and profile', () => {
    const seen: Array<Record<string, string>> = []

    $sessions.set([row('stored-probe', 'probe')])
    $activeGatewayProfile.set('default')
    disposers.push(
      registry.register({
        id: 'refine-test',
        area: 'chat.mediaActions',
        data: {
          label: 'Refine test',
          icon: 'sparkle',
          run: (ctx: { path: string; kind: string; sessionId: string; profile: string }) => {
            seen.push(ctx)
          }
        }
      })
    )

    render(
      <SessionViewProvider value={view('runtime-probe', 'stored-probe')}>
        <span className="group/image relative">
          <ChatMediaActionBar kind="image" path="C:/renders/garden.png" />
        </span>
      </SessionViewProvider>
    )

    fireEvent.click(screen.getByRole('button', { name: 'Refine test' }))

    expect(seen).toEqual([
      {
        path: 'C:/renders/garden.png',
        kind: 'image',
        sessionId: 'runtime-probe',
        profile: 'probe'
      }
    ])
    expect(screen.getByRole('button', { name: 'Refine test' }).getAttribute('data-media-action')).toBe('refine-test')
  })

  it('hides an action whose when() refuses solara or aura', () => {
    $sessions.set([row('stored-solara', 'solara')])
    disposers.push(
      registry.register({
        id: 'refine-test',
        area: 'chat.mediaActions',
        data: {
          label: 'Refine test',
          when: (ctx: { profile: string }) => ctx.profile !== 'solara' && ctx.profile !== 'aura',
          run: () => undefined
        }
      })
    )

    const { rerender } = render(
      <SessionViewProvider value={view('runtime-solara', 'stored-solara')}>
        <ChatMediaActionBar kind="video" path="C:/renders/clip.mp4" />
      </SessionViewProvider>
    )

    expect(screen.queryByRole('button', { name: 'Refine test' })).toBeNull()

    $sessions.set([row('stored-aura', 'aura')])
    rerender(
      <SessionViewProvider value={view('runtime-aura', 'stored-aura')}>
        <ChatMediaActionBar kind="image" path="C:/renders/aura.png" />
      </SessionViewProvider>
    )

    expect(screen.queryByRole('button', { name: 'Refine test' })).toBeNull()
  })

  it('renders nothing when no plugin registered', () => {
    const { container } = render(<ChatMediaActionBar kind="image" path="C:/renders/garden.png" />)

    expect(container.querySelector('[data-slot="chat-media-actions"]')).toBeNull()
    expect(container.textContent).toBe('')
  })
})
