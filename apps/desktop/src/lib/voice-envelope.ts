/**
 * The sentence envelope behind `host.voice.playback` (the plugin door's mouth
 * track), ported from the S6 lip-sync demo's `envelope()`.
 *
 * Four details are the contract, not decoration:
 *
 *  - **25 fps frame RMS** — the rate an overlay clocks to.
 *  - **p90 normalisation** — loud frames reach 1.0 whatever the recording
 *    level was, so one quiet sentence is not a closed mouth.
 *  - **gamma 0.72** — a half-amplitude frame is ~0.61, not 0.5: quiet syllables
 *    still part the mouth.
 *  - **closed head and tail** — the first and last frame are 0. That is what
 *    makes consecutive frames read as movement; without it a sentence starts
 *    and ends mid-shape.
 *
 * Pure: samples in, numbers out. No DOM, no decoding, no playback, so the math
 * is provable against a committed wav fixture.
 */
export const ENVELOPE_FPS = 25
export const ENVELOPE_GAMMA = 0.72

/** Frames are normalised to this percentile of their own loudness (the demo's
 *  p90): one clipped sample must not flatten the whole sentence. */
const REFERENCE_PERCENTILE = 0.9

/** Used only when a caller cannot tell us the rate. It rescales `durationMs`;
 *  the shape of the track is unchanged. */
const FALLBACK_SAMPLE_RATE = 24_000

export interface VoiceEnvelope {
  durationMs: number
  fps: number
  /** 0..1 per frame, closed head and tail. */
  rms: number[]
}

const toFloats = (samples: Float32Array | Int16Array): Float32Array => {
  if (samples instanceof Float32Array) {
    return samples
  }

  const floats = new Float32Array(samples.length)

  for (let index = 0; index < samples.length; index += 1) {
    floats[index] = samples[index] / 0x8000
  }

  return floats
}

const frameRms = (samples: Float32Array, frameSize: number): number[] => {
  const frames = Math.max(1, Math.ceil(samples.length / frameSize))
  const out: number[] = []

  for (let frame = 0; frame < frames; frame += 1) {
    const start = frame * frameSize
    const end = Math.min(samples.length, start + frameSize)

    let sum = 0

    for (let index = start; index < end; index += 1) {
      sum += samples[index] * samples[index]
    }

    out.push(end > start ? Math.sqrt(sum / (end - start)) : 0)
  }

  return out
}

const percentile = (values: number[], fraction: number): number => {
  if (!values.length) {
    return 0
  }

  const sorted = [...values].sort((left, right) => left - right)
  const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil(fraction * sorted.length) - 1))

  return sorted[index]
}

/** One 3-tap pass with closed edges (out-of-range = 0), so a burst ramps over
 *  three frames instead of snapping the mouth open in one. */
const smooth3 = (values: number[]): number[] => {
  const out: number[] = []

  for (let index = 0; index < values.length; index += 1) {
    const before = values[index - 1] ?? 0
    const after = values[index + 1] ?? 0

    out.push((before + values[index] + after) / 3)
  }

  return out
}

/** Mouth amounts for one sentence: `rms[i]` is the frame at `i / fps` seconds. */
export function envelope(
  samples: Float32Array | Int16Array,
  sampleRate: number,
  fps: number = ENVELOPE_FPS
): VoiceEnvelope {
  const rate = Number.isFinite(sampleRate) && sampleRate > 0 ? sampleRate : FALLBACK_SAMPLE_RATE
  const safeFps = Number.isFinite(fps) && fps > 0 ? fps : ENVELOPE_FPS
  const floats = toFloats(samples)
  const durationMs = Math.round((floats.length / rate) * 1000)
  const frameSize = Math.max(1, Math.round(rate / safeFps))
  const raw = frameRms(floats, frameSize)
  const reference = percentile(raw, REFERENCE_PERCENTILE)

  if (!(reference > 0)) {
    return { durationMs, fps: safeFps, rms: raw.map(() => 0) }
  }

  const scaled = raw.map(value => Math.min(1, value / reference) ** ENVELOPE_GAMMA)
  const smoothed = smooth3(scaled)

  if (smoothed.length) {
    smoothed[0] = 0
    smoothed[smoothed.length - 1] = 0
  }

  return { durationMs, fps: safeFps, rms: smoothed }
}
