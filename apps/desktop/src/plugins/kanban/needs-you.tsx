/**
 * Amber "Needs you" column + shared decision panel (card face + drawer bar).
 * Lives ON the board — no extra page. Tap options, don't type.
 */

import {
  Button,
  cn,
  Codicon,
  host,
  Textarea,
  Tip,
  useMutation,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { type CSSProperties, useState, useSyncExternalStore } from 'react'

import {
  $boardSlug,
  answerTask,
  boardKeyPrefix,
  fetchNeedsYou,
  needsYouKey,
  routedToScope,
  taskKey,
  undoAnswer,
  useKanbanScope
} from './api'
import { useKanban } from './i18n'
import {
  countNeedsYouByBoard,
  type GmAsk,
  type GmAskOption,
  NEEDS_YOU,
  NEEDS_YOU_TONE,
  type NeedsYouItem,
  optionsAreImagePair
} from './needs-you-logic'
import { ago, errText, shortId } from './ui'

const BTN = 'min-h-11 h-11'

export type RecentDecision = {
  label: string
  at: number
  board: string
  ask: GmAsk | null
}

type RecentStore = {
  epoch: number
  listeners: Set<() => void>
  map: Map<string, RecentDecision>
}

/** One store on globalThis so board + drawer never split across dual module copies. */
function recentStore(): RecentStore {
  const g = globalThis as typeof globalThis & { __hermesNeedsYouRecent?: RecentStore }

  if (!g.__hermesNeedsYouRecent) {
    g.__hermesNeedsYouRecent = {
      epoch: 0,
      listeners: new Set(),
      map: new Map()
    }
  }

  return g.__hermesNeedsYouRecent
}

function bumpRecentDecisions(): void {
  const store = recentStore()
  store.epoch += 1

  for (const listener of store.listeners) {listener()}
}

export function getRecentDecision(id: string): RecentDecision | undefined {
  return recentStore().map.get(id)
}

export function clearRecentDecision(id: string): void {
  if (!recentStore().map.delete(id)) {return}
  bumpRecentDecisions()
}

function stampDecision(id: string, label: string, board: string, ask: GmAsk | null): void {
  recentStore().map.set(id, { label, at: Date.now(), board, ask })
  bumpRecentDecisions()
}

/** Subscribe so drawer/board re-render when a stamp is set or cleared. */
export function useRecentDecision(id: string | null | undefined): RecentDecision | undefined {
  const epoch = useSyncExternalStore(
    onStoreChange => {
      const store = recentStore()
      store.listeners.add(onStoreChange)

      return () => {
        store.listeners.delete(onStoreChange)
      }
    },
    () => recentStore().epoch,
    () => 0,
  )

  // epoch is the subscription signal; read the map after it changes
  void epoch

  return id ? recentStore().map.get(id) : undefined
}

function askedWhen(item: NeedsYouItem): string {
  return ago(item.asked_at) ?? ''
}

function pickLabel(ask: GmAsk | null | undefined): string | null {
  if (!ask?.pick) {
    return null
  }

  const opt = ask.options.find(o => o.key === ask.pick)

  return opt ? opt.label : ask.pick
}

/** Shared option list / A-B images / send-back / undo. */
export function DecisionPanel({
  ask,
  answeredLabel,
  board,
  compact,
  legacyReason,
  onAnswered,
  onUndone,
  size = 'card',
  taskId
}: {
  ask: GmAsk | null
  /** Parent-held label after a tap (drawer sticky) so Undo survives remount. */
  answeredLabel?: null | string
  board: string
  compact?: boolean
  legacyReason?: null | string
  onAnswered?: (label: string) => void
  onUndone?: () => void
  size?: 'card' | 'drawer'
  taskId: string
}) {
  const k = useKanban()
  const qc = useQueryClient()
  const scope = useKanbanScope()
  const slug = useValue($boardSlug)
  const stamped = useRecentDecision(taskId)
  const [noteOpen, setNoteOpen] = useState(false)
  const [note, setNote] = useState('')
  const [undoVisible, setUndoVisible] = useState(false)
  const [lastLabel, setLastLabel] = useState<null | string>(null)
  const [error, setError] = useState<null | string>(null)
  const large = size === 'drawer'
  // Prefer live stamp / parent sticky so drawer survives remount after answer.
  const effectiveLabel = answeredLabel || stamped?.label || lastLabel
  const effectiveUndo = Boolean(effectiveLabel) && (Boolean(answeredLabel) || Boolean(stamped) || undoVisible)

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: needsYouKey(scope) })
    void qc.invalidateQueries({ queryKey: boardKeyPrefix(scope) })
    void qc.invalidateQueries({ queryKey: taskKey(scope, slug, taskId) })

    if (board && board !== slug) {
      void qc.invalidateQueries({ queryKey: taskKey(scope, board, taskId) })
    }
  }

  const answerMut = useMutation({
    mutationFn: (body: { key?: string; note?: string; sent_back?: boolean }) => {
      // E2E seam: contextBridge freezes hermesDesktop.api, so Playwright can't
      // stub the IPC layer. Flip this flag to force a POST failure + rollback.
      if (typeof window !== 'undefined' && (window as unknown as { __HERMES_E2E_FAIL_ANSWER?: boolean }).__HERMES_E2E_FAIL_ANSWER) {
        return Promise.reject(new Error('forced fail'))
      }

      return answerTask(taskId, body, board)
    },
    onMutate: (vars: { key?: string; note?: string; sent_back?: boolean }) => {
      // Optimistic leave Needs you + stamp "You said … · just now" before the POST returns.
      const label = vars.sent_back
        ? k.needsYouSentBack
        : (ask?.options.find(o => o.key === vars.key)?.label ?? vars.key ?? k.needsYouAnswered)

      stampDecision(taskId, label, board, ask)
      setLastLabel(label)
      setUndoVisible(true)
      setNoteOpen(false)
      setError(null)
      onAnswered?.(label)
    },
    onError: (err: unknown) => {
      clearRecentDecision(taskId)
      setUndoVisible(false)
      setLastLabel(null)
      setError(errText(err))
      host.notify({ kind: 'error', message: errText(err) })
      onUndone?.()
      invalidate()
    },
    onSuccess: () => {
      setError(null)
      setNote('')
      invalidate()
    }
  })

  const undoMut = useMutation({
    mutationFn: () => undoAnswer(taskId, board),
    onError: (err: unknown) => {
      // Server refusal ("already picked up") — plain message, no silent fail.
      setError(errText(err))
      host.notify({ kind: 'info', message: errText(err) })
      invalidate()
    },
    onSuccess: () => {
      clearRecentDecision(taskId)
      setUndoVisible(false)
      setLastLabel(null)
      setError(null)
      onUndone?.()
      invalidate()
    }
  })

  const busy = answerMut.isPending || undoMut.isPending

  if (effectiveUndo && effectiveLabel) {
    return (
      <div className="flex flex-col gap-2" data-testid="needs-you-decision-bar">
        <p className="text-[0.75rem] text-foreground">
          {k.needsYouYouSaid(effectiveLabel)} · {k.needsYouJustNow}
        </p>
        <Button
          className={BTN}
          disabled={busy}
          onClick={() => undoMut.mutate()}
          type="button"
          variant="outline"
        >
          {k.needsYouUndo}
        </Button>
        {error && <p className="text-[0.6875rem] text-destructive">{error}</p>}
      </div>
    )
  }

  // Legacy free-text needs_input — never a dead card.
  if (!ask) {
    return (
      <div className="flex flex-col gap-2" data-testid="needs-you-decision-bar">
        {legacyReason && (
          <p className="text-[0.75rem] leading-snug text-(--ui-text-secondary)">{legacyReason}</p>
        )}
        <p className="text-[0.625rem] leading-snug text-(--ui-text-quaternary)">{k.needsYouLegacyHint}</p>
        {noteOpen ? (
          <NoteBox
            busy={busy}
            note={note}
            onCancel={() => {
              setNoteOpen(false)
              setNote('')
            }}
            onChange={setNote}
            onSend={() => {
              if (!note.trim()) {
                return
              }

              answerMut.mutate({ sent_back: true, note: note.trim() })
            }}
          />
        ) : (
          <button
            className="self-start text-left text-[0.75rem] text-amber-600 underline-offset-2 hover:underline"
            onClick={() => setNoteOpen(true)}
            type="button"
          >
            {k.needsYouSendBack}
          </button>
        )}
        {error && <p className="text-[0.6875rem] text-destructive">{error}</p>}
      </div>
    )
  }

  const imagePair = optionsAreImagePair(ask)
  const pick = pickLabel(ask)

  return (
    <div className={cn('flex flex-col gap-2', large && 'gap-3')} data-testid="needs-you-decision-bar">
      {!compact && (
        <>
          <p className={cn('font-medium leading-snug text-foreground', large ? 'text-sm' : 'text-[0.8125rem]')}>
            {ask.question}
          </p>
          {ask.context && (
            <p className="text-[0.6875rem] leading-snug text-(--ui-text-tertiary)">{ask.context}</p>
          )}
        </>
      )}
      {pick && (
        <p className="text-[0.6875rem] text-amber-600">
          {k.needsYouAskerPick}: <span className="font-medium">{pick}</span>
        </p>
      )}

      {imagePair ? (
        <div className={cn('grid gap-2', ask.options.length === 2 ? 'grid-cols-2' : 'grid-cols-2 sm:grid-cols-3')}>
          {ask.options.map(opt => (
            <ImageOption
              busy={busy}
              key={opt.key}
              large={large}
              onPick={() => answerMut.mutate({ key: opt.key })}
              option={opt}
              recommended={ask.pick === opt.key}
            />
          ))}
        </div>
      ) : (
        <div className="flex flex-col gap-1.5">
          {ask.options.map(opt => (
            <Button
              className={cn(BTN, 'w-full justify-start gap-2 px-3 text-left')}
              data-option-key={opt.key}
              disabled={busy}
              key={opt.key}
              onClick={() => answerMut.mutate({ key: opt.key })}
              type="button"
              variant={ask.pick === opt.key ? 'default' : 'outline'}
            >
              <span className="flex min-w-0 flex-1 flex-col items-start gap-0.5">
                <span className="text-[0.8125rem] font-medium">{opt.label}</span>
                <span className="line-clamp-2 text-[0.625rem] font-normal text-(--ui-text-tertiary)">
                  {opt.effect}
                </span>
              </span>
              {ask.pick === opt.key && (
                <span className="shrink-0 text-[0.625rem] uppercase tracking-wide text-amber-200">
                  {k.needsYouAskerPick}
                </span>
              )}
            </Button>
          ))}
        </div>
      )}

      {noteOpen ? (
        <NoteBox
          busy={busy}
          note={note}
          onCancel={() => {
            setNoteOpen(false)
            setNote('')
          }}
          onChange={setNote}
          onSend={() => {
            if (!note.trim()) {
              return
            }

            answerMut.mutate({ sent_back: true, note: note.trim() })
          }}
        />
      ) : (
        <button
          className="self-start text-left text-[0.75rem] text-amber-700 underline-offset-2 hover:underline dark:text-amber-400"
          onClick={() => setNoteOpen(true)}
          type="button"
        >
          {k.needsYouSendBack}
        </button>
      )}
      {error && <p className="text-[0.6875rem] text-destructive">{error}</p>}
    </div>
  )
}

function NoteBox({
  busy,
  note,
  onCancel,
  onChange,
  onSend
}: {
  busy: boolean
  note: string
  onCancel: () => void
  onChange: (v: string) => void
  onSend: () => void
}) {
  const k = useKanban()

  return (
    <div className="flex flex-col gap-1.5 rounded-md border border-amber-500/40 bg-amber-500/5 p-2">
      <Textarea
        aria-label={k.needsYouNotePlaceholder}
        className="min-h-16 text-[0.75rem]"
        onChange={e => onChange(e.target.value)}
        placeholder={k.needsYouNotePlaceholder}
        value={note}
      />
      <div className="flex gap-1.5">
        <Button className={BTN} disabled={busy || !note.trim()} onClick={onSend} type="button">
          {k.needsYouSendNote}
        </Button>
        <Button className={BTN} disabled={busy} onClick={onCancel} type="button" variant="ghost">
          {k.cancel}
        </Button>
      </div>
    </div>
  )
}

function ImageOption({
  busy,
  large,
  onPick,
  option,
  recommended
}: {
  busy: boolean
  large: boolean
  onPick: () => void
  option: GmAskOption
  recommended: boolean
}) {
  const k = useKanban()
  const src = option.image_url || undefined

  return (
    <div
      className={cn(
        'flex flex-col gap-1.5 rounded-md border p-1.5',
        recommended ? 'border-amber-500 bg-amber-500/10' : 'border-(--ui-stroke-tertiary)'
      )}
    >
      <div
        className={cn(
          'flex items-center justify-center overflow-hidden rounded bg-(--ui-bg-quaternary)',
          large ? 'min-h-40' : 'min-h-24'
        )}
      >
        {src ? (
          <img alt={option.label} className="max-h-48 w-full object-contain" src={src} />
        ) : (
          <span className="px-2 text-center text-[0.625rem] text-(--ui-text-quaternary)">
            {option.image || option.label}
          </span>
        )}
      </div>
      <span className="px-0.5 text-[0.75rem] font-medium leading-snug">{option.label}</span>
      <span className="px-0.5 text-[0.625rem] leading-snug text-(--ui-text-tertiary)">{option.effect}</span>
      <Button className={cn(BTN, 'w-full')} disabled={busy} onClick={onPick} type="button">
        {k.needsYouPick} {option.label}
      </Button>
    </div>
  )
}

function NeedsYouCard({ item, onOpen }: { item: NeedsYouItem; onOpen: (item: NeedsYouItem) => void }) {
  const k = useKanban()
  const qc = useQueryClient()
  const scope = useKanbanScope()
  const who = item.asked_by || item.assignee || k.someone
  const when = askedWhen(item)

  return (
    <div
      className="kanban-needs-you-card group relative flex flex-col gap-2 rounded-md border border-amber-500/50 border-l-[3px] bg-(--ui-bg-elevated) p-2.5"
      data-task-id={item.task_id}
      data-testid="needs-you-card"
      style={{ borderLeftColor: NEEDS_YOU_TONE } as CSSProperties}
    >
      <button
        className="line-clamp-2 cursor-pointer text-left text-[0.8125rem] font-medium leading-snug text-foreground hover:underline"
        data-testid="needs-you-open"
        onClick={() => onOpen(item)}
        type="button"
      >
        {item.title || item.task_id}
      </button>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[0.625rem] text-(--ui-text-tertiary)">
        <span className="font-medium text-amber-700 dark:text-amber-400">{item.board_name || item.board}</span>
        <span>·</span>
        <span>{who}</span>
        {when && (
          <>
            <span>·</span>
            <span>{when}</span>
          </>
        )}
        <span className="ml-auto font-mono text-(--ui-text-quaternary)">{shortId(item.task_id)}</span>
      </div>
      <DecisionPanel
        ask={item.ask}
        board={item.board}
        legacyReason={item.legacy_reason || item.block_reason}
        onAnswered={() => {
          // Optimistic leave: drop from the Needs you cache immediately.
          qc.setQueryData(needsYouKey(scope), (prev: { count: number; items: NeedsYouItem[] } | undefined) => {
            if (!prev) {
              return prev
            }

            const items = prev.items.filter(row => row.task_id !== item.task_id)

            return { count: items.length, items }
          })
        }}
        taskId={item.task_id}
      />
    </div>
  )
}

export function NeedsYouColumn({
  collapsed,
  onOpen,
  onToggle
}: {
  collapsed: boolean
  onOpen: (item: NeedsYouItem) => void
  onToggle: () => void
}) {
  const k = useKanban()
  const scope = useKanbanScope()

  const { data, isLoading } = useQuery({
    enabled: query => routedToScope(query),
    queryFn: fetchNeedsYou,
    queryKey: needsYouKey(scope),
    refetchInterval: 30_000
  })

  const items = data?.items ?? []
  const count = items.length
  const label = k.needsYouLabel

  if (collapsed) {
    return (
      <button
        aria-label={k.expand(label)}
        className="kanban-needs-you-col flex h-full w-8 shrink-0 flex-col items-center gap-1.5 rounded-lg border border-amber-500/30 bg-amber-500/5 p-2 transition-colors hover:bg-amber-500/10"
        data-testid="needs-you-column"
        onClick={onToggle}
        type="button"
      >
        <span className="grid h-5 shrink-0 place-items-center">
          <span className="size-1.5 rounded-full" style={{ backgroundColor: NEEDS_YOU_TONE }} />
        </span>
        <span className="text-[0.6875rem] font-medium uppercase tracking-wide text-amber-700 [writing-mode:vertical-rl] dark:text-amber-400">
          {label}
        </span>
        {count > 0 && <span className="text-[0.625rem] tabular-nums text-amber-700 dark:text-amber-400">{count}</span>}
      </button>
    )
  }

  return (
    <div
      className="kanban-needs-you-col group/col flex h-full w-72 shrink-0 flex-col rounded-lg border border-amber-500/40 bg-amber-500/5 p-2"
      data-testid="needs-you-column"
    >
      <header className="mb-1.5 flex h-5 items-center gap-1.5 px-1">
        <span className="size-1.5 rounded-full" style={{ backgroundColor: NEEDS_YOU_TONE }} />
        <Tip label={k.needsYouHelp}>
          <span className="cursor-help text-[0.6875rem] font-medium uppercase tracking-wide text-amber-800 dark:text-amber-300">
            {label}
          </span>
        </Tip>
        <span
          className="rounded-full bg-amber-500/20 px-1.5 py-px text-[0.625rem] tabular-nums font-medium text-amber-800 dark:text-amber-200"
          data-testid="needs-you-count"
        >
          {count}
        </span>
        <button
          aria-label={k.collapse(label)}
          className="ml-auto grid size-5 place-items-center rounded text-amber-700 opacity-0 transition-opacity hover:bg-amber-500/20 focus-visible:opacity-100 group-hover/col:opacity-100 dark:text-amber-300"
          onClick={onToggle}
          type="button"
        >
          <Codicon name="chevron-left" size="0.75rem" />
        </button>
      </header>
      <div className="relative flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto">
        {isLoading && items.length === 0 && (
          <div className="grid flex-1 place-items-center text-[0.6875rem] text-(--ui-text-quaternary)">…</div>
        )}
        {!isLoading && items.length === 0 && (
          <div
            className="grid flex-1 place-items-center px-2 text-center text-[0.6875rem] text-(--ui-text-quaternary)"
            data-testid="needs-you-empty"
          >
            {k.needsYouEmpty}
          </div>
        )}
        {items.map(item => (
          <NeedsYouCard item={item} key={`${item.board}:${item.task_id}`} onOpen={onOpen} />
        ))}
      </div>
    </div>
  )
}

/** Board-tab amber badge count (0 → hidden by caller). */
export function NeedsYouBadge({ count }: { count: number }) {
  if (count <= 0) {
    return null
  }

  return (
    <span
      className="inline-flex min-w-[1.1rem] items-center justify-center rounded-full bg-amber-500/25 px-1 text-[0.625rem] font-medium tabular-nums text-amber-800 dark:text-amber-200"
      data-testid="needs-you-board-badge"
    >
      {count}
    </span>
  )
}

export function useNeedsYouCounts(): Record<string, number> {
  const scope = useKanbanScope()

  const { data } = useQuery({
    enabled: query => routedToScope(query),
    queryFn: fetchNeedsYou,
    queryKey: needsYouKey(scope),
    refetchInterval: 30_000,
    staleTime: 10_000
  })

  return countNeedsYouByBoard(data?.items ?? [])
}

export { NEEDS_YOU }
