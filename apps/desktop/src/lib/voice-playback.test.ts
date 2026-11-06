// Teardown contract for voice-playback (#91991): a barge-in (stopVoicePlayback)
// must drain EVERY live playback — streaming sessions (WebSocket + AudioContext)
// and data-URL audio elements — and a settled session must never resume or
// write stale state into a newer turn.

import type * as SharedTypes from '@hermes/shared'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $voicePlayback, clearVoiceEnvelope, voicePlaybackEnvelope } from '@/store/voice-playback'

import { playSpeechText, startSpeechStream, stopVoicePlayback } from './voice-playback'

const flush = async () => {
  for (let round = 0; round < 4; round += 1) {
    await vi.advanceTimersByTimeAsync(0)
  }
}

const gateway = vi.hoisted(() => {
  let nextUrl: string | null = 'ws://gateway.test/api/ws'

  return {
    currentUrl: () => nextUrl,
    setNextUrl: (url: string | null) => {
      nextUrl = url
    }
  }
})

vi.mock('@hermes/shared', async importOriginal => {
  const actual = await importOriginal<typeof SharedTypes>()

  return {
    ...actual,
    resolveGatewayWsUrl: async () => gateway.currentUrl()
  }
})

// The client-direct rung is the sentence path a lipsync overlay lives on; the
// config is null by default so every other test keeps taking the relay rung,
// exactly as the real resolver answers in this environment.
const direct = vi.hoisted(() => ({ config: null as null | Record<string, unknown> }))

vi.mock('@/lib/voice-client-direct', () => ({
  directTtsConfig: async () => direct.config,
  synthesizeSpeechClientDirect: async () => new ArrayBuffer(16)
}))

vi.mock('@/hermes', () => ({
  getApiRequestProfile: () => null,
  getApiRequestConnection: () => null,
  speakText: vi.fn(async () => ({
    data_url: 'data:audio/mpeg;base64,AAAA',
    mime_type: 'audio/mpeg',
    ok: true
  }))
}))

class FakeSocket {
  static readonly OPEN = 1
  static readonly CONNECTING = 0
  static instances: FakeSocket[] = []

  binaryType = 'blob'
  readyState = FakeSocket.CONNECTING
  sent: string[] = []
  url: string

  close = vi.fn(() => {
    this.readyState = 3
  })
  send = vi.fn((data: string) => {
    this.sent.push(data)
  })

  onopen: (() => void) | null = null
  onmessage: ((event: { data: string | ArrayBuffer }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null

  constructor(url: string) {
    this.url = url
    FakeSocket.instances.push(this)
  }

  serverOpen() {
    this.readyState = FakeSocket.OPEN
    this.onopen?.()
  }

  serverFrame(frame: object) {
    this.onmessage?.({ data: JSON.stringify(frame) })
  }

  serverPcm(bytes: number) {
    this.onmessage?.({ data: new ArrayBuffer(bytes) })
  }

  serverClose() {
    this.onclose?.()
  }

  serverError() {
    this.onerror?.()
  }
}

class FakeAudioContext {
  static instances: FakeAudioContext[] = []

  closed = false
  currentTime = 0
  destination = {}
  state = 'running'

  close = vi.fn(() => {
    this.closed = true

    return Promise.resolve()
  })
  // One second of constant 0.5 at 16 kHz: every frame carries the same energy,
  // so the envelope is a flat 1.0 between a closed head and tail.
  decodeAudioData = vi.fn(async () => ({
    duration: 1,
    getChannelData: () => new Float32Array(16_000).fill(0.5),
    sampleRate: 16_000
  }))
  resume = vi.fn(() => Promise.resolve())
  createBuffer = vi.fn((_channels: number, length: number, rate: number) => ({
    duration: length / rate,
    getChannelData: () => new Float32Array(length)
  }))
  createBufferSource = vi.fn(() => ({
    buffer: null as unknown,
    connect: vi.fn(),
    start: vi.fn()
  }))

  constructor() {
    FakeAudioContext.instances.push(this)
  }
}

class FakeAudio {
  static instances: FakeAudio[] = []

  paused = false
  src: string

  listeners = new Map<string, Set<() => void>>()

  addEventListener = vi.fn((type: string, handler: () => void) => {
    const bucket = this.listeners.get(type) ?? new Set()

    bucket.add(handler)
    this.listeners.set(type, bucket)
  })
  /** Fire `ended` the way a finished sentence does. */
  finish = () => {
    for (const handler of [...(this.listeners.get('ended') ?? [])]) {
      handler()
    }
  }
  load = vi.fn()
  pause = vi.fn(() => {
    this.paused = true
  })
  play = vi.fn(() => {
    this.paused = false

    return Promise.resolve()
  })
  removeEventListener = vi.fn()

  constructor(src: string) {
    this.src = src
    FakeAudio.instances.push(this)
  }
}

async function liveSession() {
  const session = await startSpeechStream({ source: 'voice-conversation' })

  if (!session) {
    throw new Error('expected a live speech stream')
  }

  return session
}

describe('voice-playback teardown (#91991)', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    FakeSocket.instances = []
    FakeAudioContext.instances = []
    FakeAudio.instances = []
    gateway.setNextUrl('ws://gateway.test/api/ws')
    vi.stubGlobal('WebSocket', FakeSocket)
    vi.stubGlobal('AudioContext', FakeAudioContext)
    vi.stubGlobal('Audio', FakeAudio)
    vi.stubGlobal('hermesDesktop', { getConnection: vi.fn(async () => ({ profile: 'test' })) })
    $voicePlayback.set({ audioElement: null, messageId: null, sequence: 0, source: null, status: 'idle' })
    clearVoiceEnvelope()
    direct.config = null
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('abandons a stream start that crosses a stop instead of resuming playback', async () => {
    // Stream discovery (gateway connection) is in flight when the user barges.
    const pending = startSpeechStream({ source: 'voice-conversation' })

    stopVoicePlayback()

    const session = await pending

    expect(session).toBeNull()
    expect(FakeSocket.instances).toHaveLength(0)
    expect($voicePlayback.get().status).toBe('idle')
  })

  it('does not let a settled session reset the playback state of a newer turn', async () => {
    const first = await liveSession()
    const second = await liveSession()

    // Starting the second session settles the first (its socket closes), but
    // the settled session's completion callback must not stomp the state the
    // newer session just wrote.
    expect(FakeSocket.instances[0]?.close).toHaveBeenCalled()
    expect($voicePlayback.get().status).toBe('preparing')

    stopVoicePlayback()
  })

  it('a barge-in closes the socket and audio context of a live streaming session and settles it', async () => {
    const session = await liveSession()
    const ws = FakeSocket.instances[0]

    ws.serverOpen()
    ws.serverFrame({ type: 'start', sample_rate: 24_000 })
    ws.serverPcm(64)

    expect($voicePlayback.get().status).toBe('speaking')

    stopVoicePlayback()

    expect(ws.close).toHaveBeenCalled()
    expect(FakeAudioContext.instances[0]?.closed).toBe(true)
    expect($voicePlayback.get().status).toBe('idle')
    await expect(session.done).resolves.toBe('done')

    // A settled session can no longer feed text — nothing reaches the socket.
    const sentBefore = ws.sent.length

    session.append('stale text')
    session.finish()

    expect(ws.sent.length).toBe(sentBefore)
  })

  it('a barge-in during the drain window settles the session and the late drain is a no-op', async () => {
    const session = await liveSession()
    const ws = FakeSocket.instances[0]

    ws.serverOpen()
    ws.serverFrame({ type: 'start', sample_rate: 24_000 })
    ws.serverPcm(64)
    ws.serverFrame({ type: 'end' }) // drains, then settles — timer pending

    stopVoicePlayback()

    await expect(session.done).resolves.toBe('done')
    expect(ws.close).toHaveBeenCalled()

    // The pending drain timer fires later — it must not double-settle or
    // disturb anything that started since.
    await vi.runOnlyPendingTimersAsync()

    expect($voicePlayback.get().status).toBe('idle')
  })

  it('a barge-in cuts a data-url playback and clears the audio element', async () => {
    gateway.setNextUrl(null) // no streaming endpoint → data-url fallback

    const playback = playSpeechText('hello world', { messageId: 'm1', source: 'read-aloud' })

    await vi.advanceTimersByTimeAsync(0)

    expect($voicePlayback.get().status).toBe('speaking')
    expect(FakeAudio.instances).toHaveLength(1)

    stopVoicePlayback()

    expect(FakeAudio.instances[0]?.pause).toHaveBeenCalled()
    expect(FakeAudio.instances[0]?.src).toBe('')
    expect(FakeAudio.instances[0]?.load).toHaveBeenCalled()
    expect($voicePlayback.get().status).toBe('idle')
    await expect(playback).resolves.toBe(false)
  })

  it('publishes one envelope for the sentence it is about to play, and clears it on a barge', async () => {
    gateway.setNextUrl(null) // no streaming endpoint → data-url rung

    const playback = playSpeechText('hello world', { messageId: 'm1', source: 'read-aloud' })

    await flush()

    expect($voicePlayback.get().status).toBe('speaking')

    const track = voicePlaybackEnvelope()

    expect(track).not.toBeNull()
    expect(track?.fps).toBe(25)
    expect(track?.durationMs).toBe(1000)
    expect(track?.rms).toHaveLength(25)
    expect(track?.rms[0]).toBe(0)
    expect(track?.rms[24]).toBe(0)
    expect(Math.max(...(track?.rms ?? [0]))).toBe(1)
    expect(track?.startedAtMs).toBeTypeOf('number')

    stopVoicePlayback()

    expect(voicePlaybackEnvelope()).toBeNull()
    await playback
  })

  it('publishes the per-sentence track on the client-direct rung and drops it when the sentence ends', async () => {
    direct.config = { min_len: 20 }
    URL.createObjectURL = vi.fn(() => 'blob:sentence')
    URL.revokeObjectURL = vi.fn()

    const playback = playSpeechText('Hello there.', { messageId: 'm1', source: 'read-aloud' })

    await flush()

    expect(FakeAudio.instances).toHaveLength(1)

    const track = voicePlaybackEnvelope()

    expect(track).not.toBeNull()
    expect(track?.rms[0]).toBe(0)
    expect(Math.max(...(track?.rms ?? [0]))).toBe(1)

    // The sentence ends: the overlay must not keep drawing the finished one.
    FakeAudio.instances[0]?.finish()
    await flush()

    expect(voicePlaybackEnvelope()).toBeNull()
    await playback
  })

  it('one stop drains BOTH of two overlapping streaming sessions (#91991)', async () => {
    // The reported overlap: session A is live with an open AudioContext;
    // session B starts (its stopVoicePlayback drains A under the Set
    // registry — the old single currentStop slot left A's socket open and
    // its done unsettled). A final explicit stop must still tear down
    // everything that is live.
    const first = await liveSession()

    const firstWs = FakeSocket.instances[0]

    firstWs.serverOpen()
    firstWs.serverFrame({ type: 'start', sample_rate: 24_000 })
    firstWs.serverPcm(64)
    first.append('turn one')

    const second = await liveSession()

    const secondWs = FakeSocket.instances[1]

    secondWs.serverOpen()
    secondWs.serverFrame({ type: 'start', sample_rate: 24_000 })
    secondWs.serverPcm(64)
    second.append('turn two')

    // Starting B drained A: both its socket and its AudioContext are gone.
    await expect(first.done).resolves.toBe('done')
    expect(firstWs.close).toHaveBeenCalled()
    expect(FakeAudioContext.instances[0]?.closed).toBe(true)

    stopVoicePlayback()

    await expect(second.done).resolves.toBe('done')
    expect(secondWs.close).toHaveBeenCalled()
    expect(FakeAudioContext.instances[1]?.closed).toBe(true)
    expect($voicePlayback.get().status).toBe('idle')
  })
})
