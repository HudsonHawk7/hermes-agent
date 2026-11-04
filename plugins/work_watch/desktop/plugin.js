/**
 * Work watch — Desktop half of plugins/work_watch (Path A).
 *
 * Settings ▸ Work mail + Teams (blessed mockup contract) + sidebar Work watch page.
 * Talks to /api/plugins/work_watch/* via ctx.rest. Never displays raw tokens.
 */
import {
  atom,
  Button,
  cn,
  Codicon,
  host,
  ListRow,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  Tip,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'
import { jsx, jsxs, Fragment } from 'react/jsx-runtime'

const SCOPES = [
  { id: 'offline_access', label: 'Stay signed in' },
  { id: 'User.Read', label: 'Who you are' },
  { id: 'Mail.Read', label: 'Read your inbox' },
  { id: 'Chat.Read', label: 'Read your Teams chats' }
]

const STATUS_LABEL = {
  off: 'Off',
  signed_in: 'Signed in',
  sign_in_in_progress: 'Sign-in in progress',
  needs_sign_in: 'Needs sign-in'
}

let rest = null

function bindRest(r) {
  rest = r
  return () => {
    rest = null
  }
}

async function api(path, opts) {
  if (!rest) throw new Error('work_watch: rest not bound')
  return rest(path, opts)
}

function statusColor(status) {
  if (status === 'signed_in') return 'var(--green, #55a583)'
  if (status === 'sign_in_in_progress') return 'var(--amber, #fbbf24)'
  if (status === 'needs_sign_in') return 'var(--red, #e75e78)'
  return 'var(--red, #e75e78)' // Off / NULL honest = red
}

function formatWhen(ts) {
  if (!ts) return 'NULL'
  try {
    return new Date(ts * 1000).toLocaleString('en-US', { timeZone: 'America/Chicago' }) + ' CT'
  } catch {
    return String(ts)
  }
}

function useStatus() {
  return useQuery({
    queryKey: ['work_watch', 'status', useValue(host.state.connectionId) || 'local'],
    queryFn: () => api('/status'),
    refetchInterval: 5000
  })
}

function useFlags() {
  return useQuery({
    queryKey: ['work_watch', 'flags', useValue(host.state.connectionId) || 'local'],
    queryFn: () => api('/flags?local=1'),
    refetchInterval: 10000
  })
}

function StatusDot({ status }) {
  return jsx('span', {
    'aria-hidden': true,
    style: {
      width: 8,
      height: 8,
      borderRadius: 99,
      background: statusColor(status),
      boxShadow: status === 'signed_in' ? `0 0 8px ${statusColor(status)}` : undefined,
      display: 'inline-block'
    }
  })
}

function SettingsPage() {
  const q = useStatus()
  const data = q.data || {
    status: 'off',
    setting_name: 'GRAPH_WATCH_REFRESH_TOKEN',
    has_refresh_token: false,
    revoke_enabled: false,
    null_honest: true,
    scopes: SCOPES.map(s => ({ ...s, admin_consent: false })),
    last_check: { last_check_at: null, last_check_count: 0 },
    identity_masked: null,
    error_code: null,
    error_plain: null,
    user_code: null,
    verification_uri: null
  }
  const status = data.status || 'off'
  const busy = status === 'sign_in_in_progress'

  const onSignIn = async () => {
    try {
      await api('/sign-in', { method: 'POST' })
      q.refetch?.()
    } catch (e) {
      host.toast?.({ title: 'Sign-in failed', description: String(e?.message || e), tone: 'danger' })
    }
  }
  const onCancel = async () => {
    await api('/sign-in/cancel', { method: 'POST' })
    q.refetch?.()
  }
  const onRevoke = async () => {
    if (!data.revoke_enabled) return
    await api('/revoke', { method: 'POST' })
    q.refetch?.()
  }

  return jsxs('div', {
    className: 'flex flex-col gap-3 p-1',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2 text-sm',
        children: [
          jsx(StatusDot, { status }),
          jsx('span', { className: 'font-semibold', children: 'Status' }),
          jsx('span', {
            style: { color: statusColor(status) },
            children: STATUS_LABEL[status] || status
          }),
          data.null_honest && status === 'off'
            ? jsx('span', {
                className: 'text-xs',
                style: { color: 'var(--red, #e75e78)' },
                children: 'NULL'
              })
            : null,
          data.identity_masked
            ? jsx('span', { className: 'text-xs opacity-70', children: data.identity_masked })
            : null
        ]
      }),
      jsx('div', {
        className: 'text-xs opacity-60',
        children: `Setting: ${data.setting_name || 'GRAPH_WATCH_REFRESH_TOKEN'}`
      }),
      data.error_code || data.error_plain
        ? jsxs('div', {
            className: 'rounded-md border px-3 py-2 text-xs',
            style: {
              borderColor: 'color-mix(in srgb, var(--red, #e75e78) 40%, transparent)',
              background: 'color-mix(in srgb, var(--red, #e75e78) 10%, transparent)'
            },
            children: [
              data.error_code ? jsxs('b', { children: [data.error_code, ' — '] }) : null,
              data.error_plain || ''
            ]
          })
        : null,
      busy
        ? jsxs('div', {
            className: 'rounded-md border px-3 py-3 text-sm',
            style: {
              borderColor: 'color-mix(in srgb, var(--amber, #fbbf24) 40%, transparent)',
              background: 'color-mix(in srgb, var(--amber, #fbbf24) 8%, transparent)'
            },
            children: [
              jsx('div', { className: 'font-medium mb-1', children: 'Device code sign-in' }),
              jsx('div', {
                className: 'font-mono text-lg tracking-widest',
                children: data.user_code || '……'
              }),
              jsx('div', {
                className: 'text-xs mt-2 opacity-80',
                children: data.verification_uri || 'https://microsoft.com/devicelogin'
              }),
              jsx('div', {
                className: 'text-xs mt-1 opacity-60',
                children: 'Open the link, enter the code (about 15 min). No client secret.'
              }),
              jsx(Button, {
                className: 'mt-3',
                variant: 'ghost',
                onClick: onCancel,
                children: 'Cancel'
              })
            ]
          })
        : null,
      jsxs('div', {
        className: 'flex flex-col gap-1',
        children: [
          jsx('div', { className: 'text-xs font-semibold opacity-70', children: 'Permissions' }),
          ...(data.scopes || SCOPES).map(s =>
            jsxs(
              'div',
              {
                className: 'flex items-center justify-between text-sm py-1 border-b border-white/5',
                children: [
                  jsxs('span', {
                    children: [s.label || s.id, ' ', jsx('span', { className: 'opacity-40 text-xs', children: s.id })]
                  }),
                  jsx('span', {
                    className: 'text-[10px] uppercase tracking-wide opacity-50',
                    children: 'No admin consent'
                  })
                ]
              },
              s.id
            )
          )
        ]
      }),
      jsxs('div', {
        className: 'text-xs opacity-70',
        children: [
          'Last check: ',
          formatWhen(data.last_check?.last_check_at),
          ' · count ',
          data.last_check?.last_check_count ?? 0,
          ' (never message text)'
        ]
      }),
      jsxs('div', {
        className: 'flex gap-2 mt-2',
        children: [
          jsx(Button, {
            onClick: onSignIn,
            disabled: busy,
            children: busy ? 'Sign-in in progress…' : 'Sign in'
          }),
          jsx(Button, {
            variant: 'danger',
            onClick: onRevoke,
            disabled: !data.revoke_enabled,
            children: 'Revoke'
          }),
          jsx(Button, {
            variant: 'ghost',
            onClick: () => host.navigate('/work-watch'),
            children: 'Open Work watch'
          })
        ]
      })
    ]
  })
}

function TravellingGlow({ active }) {
  if (!active) return null
  return jsx('div', {
    'aria-hidden': true,
    className: 'pointer-events-none absolute inset-0 rounded-xl',
    style: {
      boxShadow: '0 0 0 1px color-mix(in srgb, #22c55e 55%, transparent), 0 0 24px color-mix(in srgb, #22c55e 25%, transparent)',
      background:
        'linear-gradient(90deg, transparent, color-mix(in srgb, #22c55e 18%, transparent), transparent)',
      backgroundSize: '200% 100%',
      animation: 'workWatchScan 2.4s linear infinite'
    }
  })
}

function WorkWatchPage() {
  const statusQ = useStatus()
  const flagsQ = useFlags()
  const scanning = !!(statusQ.data && statusQ.data.scanning)
  const flags = flagsQ.data?.flags || []
  const summary = flagsQ.data?.summary || { total: 0, summary: 'quiet' }

  const sections = [
    { id: 'tier1', title: 'Needs attention (Tier 1)', tier: 1 },
    { id: 'tier2', title: 'Digest (Tier 2)', tier: 2 },
    { id: 'history', title: 'History', tier: null }
  ]

  return jsxs('div', {
    className: 'relative flex h-full flex-col gap-3 p-4',
    children: [
      jsx('style', {
        children:
          '@keyframes workWatchScan{0%{background-position:200% 0}100%{background-position:-200% 0}}'
      }),
      jsx(TravellingGlow, { active: scanning }),
      jsxs('div', {
        className: 'flex items-end justify-between gap-3',
        children: [
          jsxs('div', {
            children: [
              jsx('h1', { className: 'text-lg font-semibold', children: 'Work watch' }),
              jsx('p', {
                className: 'text-xs opacity-60',
                children: 'Local flags only — message content stays on this machine. Needs you stays board-only.'
              })
            ]
          }),
          jsxs('div', {
            className: 'flex items-center gap-2 text-xs',
            children: [
              scanning
                ? jsxs(Fragment, {
                    children: [
                      jsx(StatusDot, { status: 'signed_in' }),
                      jsx('span', { children: 'Scanning…' })
                    ]
                  })
                : jsx('span', { className: 'opacity-60', children: summary.summary || 'quiet' }),
              jsx(Button, {
                variant: 'ghost',
                onClick: async () => {
                  await api('/poll', { method: 'POST' })
                  statusQ.refetch?.()
                  flagsQ.refetch?.()
                },
                children: 'Check now'
              })
            ]
          })
        ]
      }),
      ...sections.map(sec => {
        const rows =
          sec.tier == null
            ? flags
            : flags.filter(f => f.tier === sec.tier)
        return jsxs(
          'details',
          {
            open: sec.tier === 1 || (sec.tier === 2 && rows.length > 0),
            className: 'rounded-xl border border-white/10 bg-black/20 overflow-hidden',
            children: [
              jsxs('summary', {
                className: 'cursor-pointer px-3 py-2 text-sm font-medium flex justify-between',
                children: [
                  jsx('span', { children: sec.title }),
                  jsx('span', { className: 'opacity-50 tabular-nums', children: rows.length })
                ]
              }),
              rows.length === 0
                ? jsx('div', {
                    className: 'px-3 pb-3 text-xs opacity-50',
                    children: sec.tier === 1 ? 'Quiet — nothing urgent.' : 'No rows.'
                  })
                : jsx('ul', {
                    className: 'px-3 pb-3 flex flex-col gap-2',
                    children: rows.map(f =>
                      jsxs(
                        'li',
                        {
                          className: 'rounded-lg border border-white/5 px-3 py-2 text-sm',
                          children: [
                            jsxs('div', {
                              className: 'flex justify-between gap-2',
                              children: [
                                jsx('span', {
                                  className: 'font-medium',
                                  children: f.title_local || f.conversation_id
                                }),
                                jsx('span', {
                                  className: 'text-[10px] uppercase opacity-50',
                                  children: `T${f.tier}`
                                })
                              ]
                            }),
                            jsx('div', {
                              className: 'text-xs mt-1 opacity-70',
                              children: `why: ${f.why}`
                            }),
                            f.preview_local
                              ? jsx('div', {
                                  className: 'text-xs mt-1 opacity-50 line-clamp-2',
                                  children: f.preview_local
                                })
                              : null
                          ]
                        },
                        f.id
                      )
                    )
                  })
            ]
          },
          sec.id
        )
      })
    ]
  })
}

export default {
  id: 'work_watch',
  name: 'Work watch',
  description: 'Work mail + Teams watcher — Settings panel and Work watch page.',
  defaultEnabled: true,
  register(ctx) {
    const unbind = bindRest(ctx.rest)
    ctx.onDispose?.(unbind)

    // Settings page title stays human; sidebar SDK wants label + codicon (Probe defect 3).
    ctx.registerSettingsPage?.({
      id: 'settings',
      title: 'Work mail + Teams',
      icon: 'mail',
      order: 40,
      render: () => jsx(SettingsPage, {})
    })

    ctx.register({
      id: 'work-watch-route',
      area: ROUTES_AREA,
      order: 40,
      data: {
        path: '/work-watch',
        label: 'Work watch',
        render: () => jsx(WorkWatchPage, {})
      }
    })

    ctx.register({
      id: 'work-watch-nav',
      area: SIDEBAR_NAV_AREA,
      order: 42,
      data: {
        id: 'work-watch',
        path: '/work-watch',
        label: 'Work watch',
        codicon: 'mail'
      }
    })
  }
}
