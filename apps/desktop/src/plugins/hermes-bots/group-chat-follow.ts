/**
 * Group-chat "follow the newest message" helpers.
 *
 * The room used to re-anchor only when `log.length` / `running` flipped, and
 * only when an ancestor with `data-slot="scroll-area-viewport"` existed (the
 * room body is a plain overflow div, so that listener never attached). Streaming
 * into the same entry and late-loading images both grow height without those
 * deps — the view stopped following. These helpers are the single source for:
 * near-bottom detection, scrolling the log viewport, wiring a ResizeObserver
 * that keeps a stuck view glued while content grows, and the Enter/mention
 * rule (complete only while a real option is still a prefix of what was typed).
 */

export const GROUP_CHAT_NEAR_BOTTOM_PX = 80

export type GroupChatScrollMetrics = Pick<HTMLElement, 'clientHeight' | 'scrollHeight' | 'scrollTop'>

/** True when the viewport is within `thresholdPx` of its bottom edge. */
export function isGroupChatNearBottom(
  viewport: GroupChatScrollMetrics,
  thresholdPx: number = GROUP_CHAT_NEAR_BOTTOM_PX
): boolean {
  return viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < thresholdPx
}

/** Jump the log viewport to its bottom. Prefer `scrollTo` so smooth works. */
export function scrollGroupChatToBottom(viewport: HTMLElement, behavior: ScrollBehavior = 'auto'): void {
  const top = viewport.scrollHeight

  if (typeof viewport.scrollTo === 'function') {
    viewport.scrollTo({
      behavior,
      top
    })

    return
  }

  viewport.scrollTop = top
}

/**
 * Observe content size. While `getStick()` is true, any height change scrolls
 * the viewport to the bottom (covers streaming text + late images). Returns
 * the disconnect cleanup.
 */
export function observeGroupChatLogResize(
  content: Element,
  viewport: HTMLElement,
  getStick: () => boolean,
  ResizeObserverCtor: typeof ResizeObserver = ResizeObserver
): () => void {
  const ro = new ResizeObserverCtor(() => {
    if (getStick()) {
      scrollGroupChatToBottom(viewport, 'auto')
    }
  })

  ro.observe(content)

  if (viewport) {ro.observe(viewport)}

  return () => ro.disconnect()
}

/**
 * Enter completes a mention only while the menu is open with a real option AND
 * the typed query is still a PREFIX of the listed options. A full exact match
 * of ANY listed handle means the user finished the name and wants to send —
 * never re-insert a sibling that sorts first (`@second` must not become
 * `@second-bot`). A closed or empty menu never swallows Enter. A single
 * handle string is accepted the same as a one-item list.
 */
export function shouldCompleteMentionOnEnter(
  menuOpen: boolean,
  optionHandles: null | readonly (null | string | undefined)[] | string | undefined,
  tokenQuery: string
): boolean {
  if (!menuOpen) {
    return false
  }

  const handles = (Array.isArray(optionHandles) ? optionHandles : [optionHandles])
    .map(handle =>
      String(handle || '')
        .trim()
        .toLowerCase()
    )
    .filter(Boolean)

  if (!handles.length) {
    return false
  }

  const query = String(tokenQuery || '')
    .trim()
    .toLowerCase()

  // Full unique handle already typed among the listed options → send.
  if (handles.some(handle => handle === query)) {
    return false
  }

  return true
}
