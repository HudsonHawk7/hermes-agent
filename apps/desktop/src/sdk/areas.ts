// Host-owned contribution areas with no feature module of their own; the SDK
// barrel re-exports them.
//
// A button on a chat picture or clip is not one of these mounts. Plugins
// register into `chat.mediaActions` (MEDIA_ACTION_AREA in app/chat/media-actions.ts).
// The host shows it on hover/tap and passes the media path, session id and profile.

export const PANES_AREA = 'panes'
export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' } as const
/** Titlebar slots are PERMANENT mount points: a component registered here
 *  stays mounted across chat ↔ page navigation, so `useEffect` setup/cleanup
 *  runs once per registration, not once per route. Page-owned controls that
 *  should exist only while a page is up go to `WORKSPACE_PAGE_HEADER_AREA`. */
export const TITLEBAR_AREAS = { center: 'titleBar.center', left: 'titleBar.left', right: 'titleBar.right' } as const
