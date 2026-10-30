import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  GROUP_CHAT_NEAR_BOTTOM_PX,
  isGroupChatNearBottom,
  observeGroupChatLogResize,
  scrollGroupChatToBottom,
  shouldCompleteMentionOnEnter
} from './group-chat-follow'

describe('isGroupChatNearBottom', () => {
  it('is true within the near-bottom threshold and false when scrolled up', () => {
    expect(
      isGroupChatNearBottom({
        clientHeight: 400,
        scrollHeight: 1000,
        scrollTop: 1000 - 400 - (GROUP_CHAT_NEAR_BOTTOM_PX - 1)
      })
    ).toBe(true)
    expect(
      isGroupChatNearBottom({
        clientHeight: 400,
        scrollHeight: 1000,
        scrollTop: 1000 - 400 - (GROUP_CHAT_NEAR_BOTTOM_PX + 1)
      })
    ).toBe(false)
  })
})

describe('scrollGroupChatToBottom', () => {
  it('scrolls the viewport to its scrollHeight', () => {
    const scrollTo = vi.fn()
    const viewport = {
      scrollHeight: 2400,
      scrollTo,
      scrollTop: 0
    } as unknown as HTMLElement

    scrollGroupChatToBottom(viewport, 'auto')
    expect(scrollTo).toHaveBeenCalledWith({
      behavior: 'auto',
      top: 2400
    })
  })
})

describe('observeGroupChatLogResize', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('scrolls while stuck when content height changes, and stays put when not stuck', () => {
    let callback: ResizeObserverCallback | null = null
    const observed: Element[] = []

    class FakeResizeObserver {
      constructor(cb: ResizeObserverCallback) {
        callback = cb
      }

      disconnect() {}

      observe(target: Element) {
        observed.push(target)
      }

      unobserve() {}
    }

    const content = document.createElement('div')
    const scrollTo = vi.fn()
    const viewport = {
      scrollHeight: 800,
      scrollTo,
      scrollTop: 0
    } as unknown as HTMLElement
    let stick = true

    const stop = observeGroupChatLogResize(
      content,
      viewport,
      () => stick,
      FakeResizeObserver as unknown as typeof ResizeObserver
    )

    expect(observed).toEqual([content])
    expect(callback).toBeTruthy()

    // Streaming / late image while stuck → follow.
    callback!([], {} as ResizeObserver)
    expect(scrollTo).toHaveBeenCalledTimes(1)

    // Scrolled up → no yank.
    stick = false
    scrollTo.mockClear()
    callback!([], {} as ResizeObserver)
    expect(scrollTo).not.toHaveBeenCalled()

    stop()
  })
})

describe('shouldCompleteMentionOnEnter', () => {
  it('completes only while the menu is open with a partial prefix', () => {
    expect(shouldCompleteMentionOnEnter(true, 'alpha', 'al')).toBe(true)
    expect(shouldCompleteMentionOnEnter(true, 'alpha', 'alp')).toBe(true)
  })

  it('sends (does not complete) when the full member name is already typed', () => {
    expect(shouldCompleteMentionOnEnter(true, 'alpha', 'alpha')).toBe(false)
    expect(shouldCompleteMentionOnEnter(true, 'Alpha', 'ALPHA')).toBe(false)
  })

  it('never completes when the menu is closed or has no option', () => {
    expect(shouldCompleteMentionOnEnter(false, 'alpha', 'al')).toBe(false)
    expect(shouldCompleteMentionOnEnter(true, null, 'al')).toBe(false)
    expect(shouldCompleteMentionOnEnter(true, '', 'al')).toBe(false)
  })
})
