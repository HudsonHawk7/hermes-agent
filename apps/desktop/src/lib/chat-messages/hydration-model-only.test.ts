import { describe, expect, it } from 'vitest'

import { toChatMessages } from './hydration'

// A micro-compaction merge (`display_metadata.model_only`) is skipped, but the ids of the rows after it
// keep their stored position: filtering first shifted every later message and tool-part id by one.
describe('toChatMessages with a model-only merge row', () => {
  it('skips the merge and keeps later ids keyed by stored position', () => {
    const messages = [
      { role: 'user', content: 'Q1\n\nQ2', timestamp: 100, display_metadata: { model_only: true } },
      { role: 'user', content: 'Q3', timestamp: 200 },
      { role: 'assistant', content: 'A3', timestamp: 201 }
    ]

    const result = toChatMessages(messages as never)

    expect(result.map(m => m.role)).toEqual(['user', 'assistant'])
    expect(result[0].id).toBe('200-1-user')
    expect(result[1].id).toBe('201-2-assistant')
  })

  it('strips mid-turn compaction restatement and out-of-band steer wrappers from user messages', () => {
    const wrappedSteer = [
      '[STILL IN PROGRESS — this is the active request, restated after the compaction boundary because it was not finished yet. Continue it; do not start over.]',
      '[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at this position; not tool output and not a new delivery when replayed from conversation history]',
      'you better check in with architect_Claude_Code and make sure you guys are on the same page, get your brief and resume from mcp',
      '[/OUT-OF-BAND USER MESSAGE]'
    ].join('\n')

    const result = toChatMessages([
      { role: 'user', content: wrappedSteer, display_kind: 'steer', timestamp: 100 },
      { role: 'assistant', content: 'Checked my MCP brief + resume.', timestamp: 101 }
    ] as never)

    expect(result.map(m => [m.role, m.parts[0]?.type === 'text' ? m.parts[0].text : ''])).toEqual([
      [
        'user',
        'you better check in with architect_Claude_Code and make sure you guys are on the same page, get your brief and resume from mcp'
      ],
      ['assistant', 'Checked my MCP brief + resume.']
    ])
  })
})
