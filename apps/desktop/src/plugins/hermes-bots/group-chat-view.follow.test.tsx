import { cleanup, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { translateBots } from './i18n-test-helper'

// First-layout follow: a tall room must land at the latest without waiting
// for a later ResizeObserver tick. Geometry is live on the log viewport
// (overscroll-contain) so the mount-sync cannot hide behind jsdom zeros.
vi.mock('@hermes/plugin-sdk', async () => {
  const { pluginSdkMock, createGroupGateway } = await import('./group-test-utils')
  const base = await pluginSdkMock(createGroupGateway().host)

  const Button = ({ children, onClick, title }: { children?: ReactNode; onClick?: () => void; title?: string }) => (
    <button onClick={onClick} title={title}>
      {children}
    </button>
  )

  return {
    ...base,
    Button,
    RowButton: Button,
    cn: (...values: unknown[]) => values.filter(Boolean).join(' '),
    Codicon: () => null,
    CopyButton: () => null,
    ConfirmDialog: () => null,
    Dialog: () => null,
    DialogContent: () => null,
    DialogDescription: () => null,
    DialogFooter: () => null,
    DialogHeader: () => null,
    DialogTitle: () => null,
    Input: () => null,
    ListeningLight: () => null,
    MessageTextContent: ({ text }: { text: string }) => <span data-testid="message-text-content">{text}</span>,
    ToggleRow: () => null,
    Tip: ({ children }: { children: ReactNode }) => children,
    relativeTime: () => 'now',
    useI18n: () => ({ t: { common: { cancel: 'Cancel', save: 'Save' } } }),
    usePluginI18n: () => translateBots
  }
})
vi.mock('./avatar', () => ({ avatarColor: () => '#888', botAppearance: () => ({}), BotFace: () => null }))
vi.mock('./group-chat-parts', () => ({
  GroupClarifyCard: () => null,
  GroupImageControls: () => null,
  GroupMentionInput: () => null
}))

const VIEWPORT_HEIGHT = 400
const VIEWPORT_CONTENT = 2400
const NEAR_BOTTOM_PX = 80

const originalClientHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientHeight')
const originalScrollHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollHeight')
const originalScrollTop = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollTop')
const originalScrollTo = HTMLElement.prototype.scrollTo
const originalResizeObserver = globalThis.ResizeObserver
const scrollTopByEl = new WeakMap<object, number>()

function isLogViewport(el: HTMLElement) {
  return el.classList.contains('overscroll-contain')
}

beforeEach(() => {
  class SilentResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  vi.stubGlobal('ResizeObserver', SilentResizeObserver)

  Object.defineProperty(HTMLElement.prototype, 'clientHeight', {
    configurable: true,
    get() {
      return isLogViewport(this) ? VIEWPORT_HEIGHT : 0
    }
  })
  Object.defineProperty(HTMLElement.prototype, 'scrollHeight', {
    configurable: true,
    get() {
      return isLogViewport(this) ? VIEWPORT_CONTENT : 0
    }
  })
  Object.defineProperty(HTMLElement.prototype, 'scrollTop', {
    configurable: true,
    get() {
      return scrollTopByEl.get(this) ?? 0
    },
    set(value: number) {
      scrollTopByEl.set(this, Number(value) || 0)
    }
  })

  HTMLElement.prototype.scrollTo = function (this: HTMLElement, arg?: ScrollToOptions | number, y?: number) {
    const top =
      typeof arg === 'number' ? arg : typeof arg === 'object' && arg && 'top' in arg ? Number(arg.top) : Number(y)

    if (Number.isFinite(top)) {
      this.scrollTop = top
    }
  }
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()

  if (originalResizeObserver) {
    globalThis.ResizeObserver = originalResizeObserver
  }

  if (originalClientHeight) {
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', originalClientHeight)
  }

  if (originalScrollHeight) {
    Object.defineProperty(HTMLElement.prototype, 'scrollHeight', originalScrollHeight)
  }

  if (originalScrollTop) {
    Object.defineProperty(HTMLElement.prototype, 'scrollTop', originalScrollTop)
  }

  HTMLElement.prototype.scrollTo = originalScrollTo
})

it('opens a tall room at the latest without the jump pill', async () => {
  const { $groupChats } = await import('./group-chat')
  const { GroupChatWorkspace } = await import('./group-chat-view')

  const log = Array.from({ length: 24 }, (_, i) => ({
    id: `m${i + 1}`,
    thread: 'a',
    from: { kind: 'member' as const, name: 'builder' },
    text: `line-${i + 1}`,
    at: i + 1
  }))

  $groupChats.set({ Room: { log, watermarks: {}, sessions: {} } })

  render(<GroupChatWorkspace group="Room" members={[{ handle: 'builder', name: 'builder', title: '' }]} />)

  const last = await screen.findByText('line-24')

  await waitFor(() => {
    const viewport = last.closest('.overscroll-contain')
    expect(viewport).toBeInstanceOf(HTMLElement)
    expect((viewport as HTMLElement).scrollTop).toBeGreaterThanOrEqual(
      VIEWPORT_CONTENT - VIEWPORT_HEIGHT - NEAR_BOTTOM_PX
    )
  })

  expect(screen.queryByRole('button', { name: 'Jump to latest' })).toBeNull()
})
