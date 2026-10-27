// Fulcrum board v2 — read-only Hermes desktop plugin (story US-4252, R&D spike).
//
// Plain ESM disk plugin: no build step, no Hermes rebuild, no settings toggle.
//   - a full page (ROUTES_AREA) at /fulcrum
//   - a sidebar nav row (SIDEBAR_NAV_AREA) labelled "Fulcrum"
//   - a palette command that navigates there
//
// DATA PATH (t_7f07258e, 10/27): prefer host.kanbanBoard — the session-free
// REST door that matches the built-in Kanban page
// (`GET /api/plugins/kanban/board`). That works on page routes when the
// gateway WebSocket is idle. Fall back to slash.exec /kanban list|show only
// when the SDK is older than that door.
//
// READ-ONLY: no Post, no Send back, no ADO calls, no writes anywhere.

import { host, useQuery, useValue, ROUTES_AREA, SIDEBAR_NAV_AREA, PALETTE_AREA } from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const BOARD = 'fulcrum-mymeter'
const ROUTE = '/fulcrum'
const MAX_CARDS = 60
const SHOW_CONCURRENCY = 6

function shortError(err) {
  const message = (err && (err.message || err.error)) || String(err)
  return String(message).slice(0, 160)
}

let routeCache = null

async function profileRoute() {
  if (routeCache) return routeCache
  try {
    const routes = await host.profileRoutes()
    routeCache = Array.isArray(routes) && routes.length ? routes[0] : null
  } catch (err) {
    routeCache = null
  }
  return routeCache
}

// Gateway JSON-RPC via the app's own route first (that is the connected one),
// then the active-gateway call.
async function request(method, params) {
  const errors = []
  const route = await profileRoute()
  if (route) {
    try {
      const value = await host.requestProfile(route, method, params)
      return { value, via: 'requestProfile(' + (route.profile || route.targetProfile || '?') + ')' }
    } catch (err) {
      errors.push('requestProfile ' + method + ' -> ' + shortError(err))
    }
  }
  try {
    const value = await host.request(method, params)
    return { value, via: 'request' }
  } catch (err) {
    errors.push('request ' + method + ' -> ' + shortError(err))
  }
  const error = new Error(errors[errors.length - 1] || 'RPC failed')
  error.attempts = errors
  throw error
}

function readAtom(atom) {
  try {
    const value = atom && typeof atom.get === 'function' ? atom.get() : atom
    return typeof value === 'string' && value ? value : null
  } catch (err) {
    return null
  }
}

// slash.exec runs against a local session. A page route has no focused chat, so
// try every session id the host exposes and finally no session id at all.
function sessionCandidates() {
  const ids = [
    readAtom(host.state.focusedSessionId),
    readAtom(host.state.focusedStoredSessionId),
    readAtom(host.state.activeSessionId),
    null
  ]
  const out = []
  for (const id of ids) if (!out.includes(id)) out.push(id)
  return out
}

async function execSlash(command) {
  const errors = []
  for (const sessionId of sessionCandidates()) {
    const params = sessionId ? { session_id: sessionId, command } : { command }
    try {
      const answered = await request('slash.exec', params)
      const text = textFrom(answered.value)
      if (text) {
        return { text, via: answered.via + ' slash.exec ' + (sessionId ? 'session' : 'no-session') }
      }
      errors.push('slash.exec ' + command + ' -> empty answer')
    } catch (err) {
      const nested = Array.isArray(err && err.attempts) ? err.attempts : null
      if (nested && nested.length) {
        for (const line of nested) errors.push(line)
      } else {
        errors.push('slash.exec ' + command + ' -> ' + shortError(err))
      }
    }
  }
  const error = new Error(errors[errors.length - 1] || 'slash.exec failed')
  error.attempts = errors
  error.rawText = errors.join('\n')
  throw error
}

// slash.exec answers differ by command; find the human text in whatever it sent.
function textFrom(value) {
  if (typeof value === 'string') return value
  if (!value || typeof value !== 'object') return ''
  for (const key of ['text', 'output', 'message', 'content', 'result', 'reply', 'stdout']) {
    const candidate = value[key]
    if (typeof candidate === 'string' && candidate.trim()) return candidate
    if (candidate && typeof candidate === 'object') {
      const nested = textFrom(candidate)
      if (nested) return nested
    }
  }
  for (const key of Object.keys(value)) {
    const nested = textFrom(value[key])
    if (nested) return nested
  }
  return ''
}

const STATUS_WORDS = [
  'scheduled',
  'running',
  'review',
  'blocked',
  'ready',
  'todo',
  'triage',
  'done',
  'archived'
]

// `/kanban list` prints one task per line; ids are t_<hex>.
function parseList(text) {
  const cards = []
  if (!text) return cards
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.replace(/\s+$/, '')
    const idMatch = line.match(/\b(t_[0-9a-f]{6,})\b/i)
    if (!idMatch) continue
    const id = idMatch[1]
    const rest = line.slice(line.indexOf(id) + id.length).trim()
    const status = STATUS_WORDS.find((word) => new RegExp('\\b' + word + '\\b').test(rest)) || ''
    const title = rest
      .replace(new RegExp('\\b' + (status || 'zzzz') + '\\b'), ' ')
      .replace(/\s{2,}/g, ' | ')
      .replace(/^[|\s]+|[|\s]+$/g, '')
    cards.push({ id, status, title: title.slice(0, 160), raw: line })
  }
  return cards
}

function field(text, name) {
  const match = String(text || '').match(new RegExp('^\\s*' + name + ':\\s*(.+)$', 'im'))
  return match ? match[1].trim() : ''
}

// Card body carries the ADO mirror link; its project segment is the tag.
//   .../acceleratedinnovations/AI%20Support/_workitems/edit/31512 => "AI Support" => Support
//   .../acceleratedinnovations/AI/_workitems/edit/31426           => "AI"         => Dev
function adoFrom(text) {
  const match = String(text || '').match(/https?:\/\/\S*?\/([^/\s]+)\/_workitems\/edit\/(\d+)/)
  if (!match) return { url: '', project: '', adoId: '' }
  let project = match[1]
  try {
    project = decodeURIComponent(project)
  } catch (err) {
    project = match[1]
  }
  return { url: match[0], project, adoId: match[2] }
}

function tagFor(project, type) {
  if (/support/i.test(project)) return 'Support'
  if (project) return 'Dev'
  if (/bug|issue/i.test(type || '')) return 'Support'
  return 'Untagged'
}

// The Brief = the card's latest summary/result.
function briefFrom(text) {
  const result = field(text, 'result')
  if (result) return result
  const lines = String(text || '')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
  return lines.slice(-12).join('\n')
}

function cardsFromRestBoard(board) {
  const cards = []
  const columns = (board && board.columns) || []
  for (const col of columns) {
    for (const task of col.tasks || []) {
      if (!task || !task.id) continue
      const body = String(task.body || '')
      const ado = adoFrom(body)
      const type = field(body, 'type')
      cards.push({
        id: task.id,
        title: task.title || task.id,
        status: task.status || col.name || '',
        project: ado.project,
        adoUrl: ado.url,
        adoId: ado.adoId || '',
        tag: tagFor(ado.project, type),
        brief: String(task.latest_summary || briefFrom(body) || ''),
        detailText: body.slice(0, 8000)
      })
    }
  }
  return cards
}

async function enrich(card) {
  try {
    const shown = await execSlash('/kanban show ' + card.id)
    const text = shown.text || ''
    const ado = adoFrom(text)
    const type = field(text, 'type')
    const titleMatch = String(text).match(/^\s*title:\s*(.+)$/im)
    return {
      ...card,
      title: titleMatch ? titleMatch[1].trim() : card.title,
      status: field(text, 'status') || card.status,
      project: ado.project,
      adoUrl: ado.url,
      adoId: ado.adoId || '',
      tag: tagFor(ado.project, type),
      brief: briefFrom(text),
      detailText: text.slice(0, 8000)
    }
  } catch (err) {
    return { ...card, project: '', adoUrl: '', adoId: '', tag: 'Untagged', brief: '', detailText: '' }
  }
}

async function mapLimit(items, limit, fn) {
  const out = new Array(items.length)
  let cursor = 0
  const workers = new Array(Math.min(limit, items.length)).fill(0).map(async () => {
    while (cursor < items.length) {
      const index = cursor
      cursor += 1
      out[index] = await fn(items[index], index)
    }
  })
  await Promise.all(workers)
  return out
}

async function fetchBoardViaRest() {
  if (typeof host.kanbanBoard !== 'function') {
    const error = new Error('host.kanbanBoard unavailable')
    error.attempts = ['host.kanbanBoard -> not on this SDK']
    throw error
  }
  const board = await host.kanbanBoard({ board: BOARD })
  const cards = cardsFromRestBoard(board).slice(0, MAX_CARDS)
  if (!cards.length) {
    const error = new Error('host.kanbanBoard returned no tasks')
    error.attempts = ['host.kanbanBoard -> empty board']
    throw error
  }
  return {
    cards,
    via: 'host.kanbanBoard REST board=' + BOARD,
    rawText: ''
  }
}

async function fetchBoardViaSlash() {
  const listed = await execSlash('/kanban list --board ' + BOARD)
  const parsed = parseList(listed.text).slice(0, MAX_CARDS)
  if (!parsed.length) {
    const error = new Error('slash.exec returned no tasks to parse')
    error.attempts = listed.attempts || []
    error.rawText = (listed.text || '(empty)').slice(0, 1500)
    throw error
  }
  const cards = await mapLimit(parsed, SHOW_CONCURRENCY, enrich)
  return {
    cards,
    via: listed.via + ' "/kanban list --board ' + BOARD + '"',
    rawText: listed.text.slice(0, 1200)
  }
}

async function fetchBoard() {
  const attempts = []
  try {
    return await fetchBoardViaRest()
  } catch (err) {
    const nested = Array.isArray(err && err.attempts) ? err.attempts : null
    if (nested && nested.length) {
      for (const line of nested) attempts.push(line)
    } else {
      attempts.push('host.kanbanBoard -> ' + shortError(err))
    }
  }
  try {
    return await fetchBoardViaSlash()
  } catch (err) {
    const nested = Array.isArray(err && err.attempts) ? err.attempts : null
    if (nested && nested.length) {
      for (const line of nested) attempts.push(line)
    } else {
      attempts.push('slash.exec -> ' + shortError(err))
    }
    if (err && err.rawText) attempts.push(String(err.rawText).slice(0, 800))
  }
  const error = new Error(attempts[attempts.length - 1] || 'Fulcrum could not read the board')
  error.attempts = attempts
  error.rawText = attempts.join('\n')
  throw error
}

const TAG_STYLE = {
  Support: 'bg-(--ui-stroke-secondary) text-(--ui-accent)',
  Dev: 'bg-(--ui-stroke-secondary) text-(--ui-text-secondary)',
  Untagged: 'bg-(--ui-stroke-secondary) text-(--ui-text-tertiary)'
}

function Tag(props) {
  const tag = props.tag
  return jsx('span', {
    className:
      'shrink-0 rounded px-1.5 py-0.5 text-[0.6875rem] font-medium ' +
      (TAG_STYLE[tag] || TAG_STYLE.Untagged),
    children: tag
  })
}

function CardRow(props) {
  const card = props.card
  return jsx('button', {
    type: 'button',
    onClick: () => props.onOpen(card),
    className:
      'flex w-full flex-col gap-1 rounded border border-(--ui-stroke-secondary) px-3 py-2 text-left hover:border-(--ui-accent)',
    children: jsxs('div', {
      className: 'flex w-full items-center gap-2',
      children: [
        jsx('span', {
          className: 'shrink-0 text-[0.6875rem] tabular-nums text-(--ui-text-tertiary)',
          children: card.adoId ? '#' + card.adoId : card.id
        }),
        jsx('span', {
          className: 'min-w-0 flex-1 truncate text-sm text-(--ui-text-primary)',
          children: card.title || card.id
        }),
        card.status
          ? jsx('span', {
              className: 'shrink-0 text-[0.6875rem] text-(--ui-text-tertiary)',
              children: card.status
            })
          : null,
        jsx(Tag, { tag: card.tag })
      ]
    })
  })
}

function BoardScreen(props) {
  const board = props.board
  const groups = ['Support', 'Dev', 'Untagged']
  const byTag = { Support: [], Dev: [], Untagged: [] }
  for (const card of board.cards) (byTag[card.tag] || byTag.Untagged).push(card)

  const sections = []
  for (const tag of groups) {
    if (!byTag[tag].length) continue
    const rows = byTag[tag].map((card) => jsx(CardRow, { card, onOpen: props.onOpen }, card.id))
    sections.push(
      jsxs(
        'section',
        {
          className: 'flex flex-col gap-1.5',
          children: [
            jsx('div', {
              className:
                'text-[0.6875rem] font-semibold uppercase tracking-wide text-(--ui-text-tertiary)',
              children: tag + ' · ' + byTag[tag].length
            }),
            rows
          ]
        },
        tag
      )
    )
  }

  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 overflow-auto p-4',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx('span', {
            className: 'text-sm text-(--ui-text-secondary)',
            children: board.cards.length + ' cards'
          }),
          jsx('span', { className: 'text-(--ui-text-quaternary)', children: '·' }),
          jsx('span', {
            className: 'text-[0.6875rem] text-(--ui-text-quaternary)',
            children: 'via ' + board.via
          }),
          jsx('span', { className: 'flex-1' }),
          jsx('button', {
            type: 'button',
            onClick: props.onRefresh,
            disabled: props.query.isFetching,
            className:
              'rounded border border-(--ui-stroke-secondary) px-2 py-0.5 text-[0.6875rem] text-(--ui-text-secondary) hover:border-(--ui-accent) disabled:opacity-50',
            children: props.query.isFetching ? 'refreshing…' : 'Refresh'
          })
        ]
      }),
      sections
    ]
  })
}

function BriefScreen(props) {
  const card = props.card
  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 overflow-auto p-4',
    children: [
      jsx('button', {
        type: 'button',
        onClick: props.onBack,
        className: 'self-start text-[0.6875rem] text-(--ui-text-secondary) hover:text-(--ui-accent)',
        children: '← Board'
      }),
      jsxs('div', {
        className: 'flex flex-col gap-1',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx('span', {
                className: 'text-[0.6875rem] tabular-nums text-(--ui-text-tertiary)',
                children: card.adoId ? '#' + card.adoId : card.id
              }),
              jsx(Tag, { tag: card.tag }),
              jsx('span', {
                className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                children: card.status || ''
              })
            ]
          }),
          jsx('h2', {
            className: 'text-base font-medium text-(--ui-text-primary)',
            children: card.title || card.id
          })
        ]
      }),
      jsx('div', {
        className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)',
        children: 'Brief'
      }),
      jsx('div', {
        className:
          'whitespace-pre-wrap rounded border border-(--ui-stroke-secondary) p-3 text-sm text-(--ui-text-primary)',
        children: card.brief || 'No summary yet on this card.'
      }),
      card.adoUrl
        ? jsx('div', {
            className: 'text-[0.6875rem] text-(--ui-text-quaternary)',
            children: 'ADO: ' + card.adoUrl + ' (read-only mirror)'
          })
        : null
    ]
  })
}

function FulcrumPage() {
  const [selected, setSelected] = useState(null)
  const gateway = useValue(host.state.gateway)

  const boardQuery = useQuery({
    queryKey: ['fulcrum', BOARD],
    queryFn: fetchBoard,
    staleTime: 30000,
    refetchInterval: 120000,
    retry: false
  })

  if (boardQuery.isPending) {
    return jsx('div', {
      className: 'p-4 text-sm text-(--ui-text-tertiary)',
      children: 'Loading Fulcrum board…'
    })
  }

  if (boardQuery.isError) {
    const errorObject = boardQuery.error || {}
    const attempts = Array.isArray(errorObject.attempts)
      ? errorObject.attempts
      : errorObject.rawText
        ? String(errorObject.rawText).split(/\r?\n/).filter(Boolean)
        : []
    return jsxs('div', {
      className: 'flex h-full flex-col gap-2 overflow-auto p-4 text-sm',
      children: [
        jsx('div', {
          className: 'text-(--ui-text-primary)',
          children: 'Fulcrum could not read the board.'
        }),
        jsx('div', {
          className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
          children: String(errorObject.message || errorObject)
        }),
        jsx('div', {
          className: 'text-[0.6875rem] text-(--ui-text-quaternary)',
          children: 'gateway ' + String(gateway || 'unknown')
        }),
        jsx('div', {
          className: 'text-[0.6875rem] font-medium text-(--ui-text-tertiary)',
          children: attempts.length ? 'Attempts (' + attempts.length + ')' : 'Attempts'
        }),
        jsx('div', {
          className:
            'whitespace-pre-wrap rounded border border-(--ui-stroke-secondary) p-2 text-[0.6875rem] text-(--ui-text-quaternary)',
          children: attempts.length ? attempts.join('\n') : '(no attempts recorded)'
        })
      ]
    })
  }

  const board = boardQuery.data

  const body = selected
    ? jsx(BriefScreen, { card: selected, onBack: () => setSelected(null) })
    : jsx(BoardScreen, {
        board,
        query: boardQuery,
        onOpen: (card) => setSelected(card),
        onRefresh: () => boardQuery.refetch()
      })

  return jsxs('div', {
    className: 'flex h-full flex-col',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2 border-b border-(--ui-stroke-secondary) px-4 py-2',
        children: [
          jsx('span', {
            className: 'text-sm font-medium text-(--ui-text-primary)',
            children: 'Fulcrum'
          }),
          jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: BOARD }),
          jsx('span', {
            className:
              'rounded bg-(--ui-stroke-secondary) px-1.5 py-0.5 text-[0.625rem] uppercase tracking-wide text-(--ui-text-tertiary)',
            children: 'read-only'
          }),
          jsx('span', { className: 'flex-1' }),
          jsx('span', {
            className: 'text-[0.6875rem] text-(--ui-text-quaternary)',
            children: 'gateway ' + String(gateway || 'unknown')
          })
        ]
      }),
      jsx('div', { className: 'min-h-0 flex-1', children: body })
    ]
  })
}

export default {
  id: 'fulcrum-board',
  name: 'Fulcrum',
  register(ctx) {
    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: ROUTE },
        render: () => jsx(FulcrumPage, {})
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        data: { path: ROUTE, label: 'Fulcrum', codicon: 'project' }
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'fulcrum.open',
          label: 'Open Fulcrum board',
          run: () => host.navigate(ROUTE)
        }
      }
    ])
  }
}
