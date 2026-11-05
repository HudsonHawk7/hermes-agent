// The plugin voice door hands a plugin one array of mouth amounts per spoken
// sentence (`host.voice.playback`). These tests pin the four things the S6
// demo's `envelope()` promised and a lip-sync overlay actually leans on:
//
//   - a closed head and tail, so the first and last frames of every sentence
//     are a closed mouth (that is what makes consecutive frames move),
//   - a real peak, because an all-zero track draws nothing,
//   - 25 frames per second, the rate the overlay clocks to,
//   - the demo's 3-tap ramp and 0.72 gamma, so a burst does not snap open.
//
// The fixture is a real RIFF/wav (16-bit PCM mono) built and read back here, so
// the numbers come from wav bytes rather than a hand-made float array.
import { describe, expect, it } from 'vitest'

import { envelope, ENVELOPE_FPS, ENVELOPE_GAMMA } from './voice-envelope'

const SAMPLE_RATE = 16_000
const FRAME = SAMPLE_RATE / ENVELOPE_FPS // 640 samples

/** A 1 s 400 Hz tone at 0.8 amplitude surrounded by silence: 25 frames, of
 *  which frames 5..19 carry speech-like energy. 400 Hz divides the 640-sample
 *  frame exactly (16 cycles), so every loud frame has the same RMS. */
function speechishFixture(): Float32Array {
  const samples = new Float32Array(SAMPLE_RATE)

  for (let frame = 5; frame < 20; frame += 1) {
    for (let index = 0; index < FRAME; index += 1) {
      const position = frame * FRAME + index

      samples[position] = 0.8 * Math.sin((2 * Math.PI * 400 * position) / SAMPLE_RATE)
    }
  }

  return samples
}

/** A 1.08 s fixture (27 frames) whose frame RMS is exactly 1.0 / 0.5 / 1.0.
 *  DC levels, so the RMS of a frame is the level itself — this is the fixture
 *  the gamma assertion reads. */
function steppedFixture(): Float32Array {
  const samples = new Float32Array(FRAME * 27)

  for (let frame = 0; frame < 27; frame += 1) {
    const level = frame >= 9 && frame < 18 ? 0.5 : 1

    samples.fill(level, frame * FRAME, (frame + 1) * FRAME)
  }

  return samples
}

/** 16-bit PCM mono wav bytes (RIFF), the smallest real container we can hand
 *  the door's decoder in a test. */
function wavBytes(samples: Float32Array, sampleRate: number): ArrayBuffer {
  const buffer = new ArrayBuffer(44 + samples.length * 2)
  const view = new DataView(buffer)

  const ascii = (offset: number, text: string) => {
    for (let index = 0; index < text.length; index += 1) {
      view.setUint8(offset + index, text.charCodeAt(index))
    }
  }

  ascii(0, 'RIFF')
  view.setUint32(4, 36 + samples.length * 2, true)
  ascii(8, 'WAVE')
  ascii(12, 'fmt ')
  view.setUint32(16, 16, true) // PCM chunk size
  view.setUint16(20, 1, true) // PCM
  view.setUint16(22, 1, true) // mono
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * 2, true) // byte rate
  view.setUint16(32, 2, true) // block align
  view.setUint16(34, 16, true) // bits
  ascii(36, 'data')
  view.setUint32(40, samples.length * 2, true)

  for (let index = 0; index < samples.length; index += 1) {
    const clamped = Math.max(-1, Math.min(1, samples[index]))

    view.setInt16(44 + index * 2, Math.round(clamped * 0x7fff), true)
  }

  return buffer
}

/** Walk the RIFF chunks and hand back the PCM exactly as a decoder would. */
function readWav(buffer: ArrayBuffer): { pcm: Int16Array; sampleRate: number } {
  const view = new DataView(buffer)

  let offset = 12
  let pcm = new Int16Array(0)
  let sampleRate = 0

  while (offset + 8 <= buffer.byteLength) {
    const id = String.fromCharCode(...new Uint8Array(buffer, offset, 4))
    const size = view.getUint32(offset + 4, true)

    if (id === 'fmt ') {
      // fmt body: format (u16), channels (u16), sample rate (u32) → +4 from the body.
      sampleRate = view.getUint32(offset + 12, true)
    } else if (id === 'data') {
      pcm = new Int16Array(buffer.slice(offset + 8, offset + 8 + size))
    }

    offset += 8 + size + (size % 2)
  }

  return { pcm, sampleRate }
}

describe('voice envelope — the demo envelope() the plugin door publishes', () => {
  it('closes the head and the tail of a fixture wav, and keeps a real peak', () => {
    const { pcm, sampleRate } = readWav(wavBytes(speechishFixture(), SAMPLE_RATE))
    const track = envelope(pcm, sampleRate)

    expect(track.fps).toBe(ENVELOPE_FPS)
    expect(track.rms).toHaveLength(25)
    expect(track.durationMs).toBe(1000)

    // Closed head/tail: the mouth starts and ends shut, every sentence.
    expect(track.rms[0]).toBe(0)
    expect(track.rms[track.rms.length - 1]).toBe(0)

    // A peak the overlay can actually draw, and nothing out of range.
    expect(Math.max(...track.rms)).toBeCloseTo(1, 2)
    expect(track.rms.every(value => value >= 0 && value <= 1)).toBe(true)

    // The quiet lead-in stays shut.
    expect(track.rms.slice(1, 4)).toEqual([0, 0, 0])
  })

  it('ramps into a burst over three frames instead of snapping open', () => {
    const { pcm, sampleRate } = readWav(wavBytes(speechishFixture(), SAMPLE_RATE))
    const track = envelope(pcm, sampleRate)

    // 3-tap smoothing, edges closed: 1/3 then 2/3 then the full frame.
    expect(track.rms[4]).toBeCloseTo(1 / 3, 2)
    expect(track.rms[5]).toBeCloseTo(2 / 3, 2)
    expect(track.rms[6]).toBeCloseTo(1, 2)
  })

  it('applies the demo gamma (0.72) so half amplitude is not half mouth', () => {
    const { pcm, sampleRate } = readWav(wavBytes(steppedFixture(), SAMPLE_RATE))
    const track = envelope(pcm, sampleRate)

    expect(track.rms).toHaveLength(27)
    // Interior frames of a steady passage are unchanged by the 3-tap average.
    expect(track.rms[4]).toBeCloseTo(1, 3)
    expect(track.rms[13]).toBeCloseTo(0.5 ** ENVELOPE_GAMMA, 3)
    expect(track.rms[13]).toBeGreaterThan(0.5)
  })

  it('answers silence with zeros, never NaN', () => {
    const track = envelope(new Float32Array(SAMPLE_RATE), SAMPLE_RATE)

    expect(track.rms).toHaveLength(25)
    expect(track.rms.every(value => value === 0)).toBe(true)
    expect(track.durationMs).toBe(1000)
  })

  it('survives a sample rate the caller could not know', () => {
    const track = envelope(new Int16Array(1600), 0)

    expect(Number.isFinite(track.durationMs)).toBe(true)
    expect(track.rms.every(value => Number.isFinite(value) && value === 0)).toBe(true)
  })
})
