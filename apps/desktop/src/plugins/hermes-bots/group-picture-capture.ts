/**
 * One still, in memory. The webcam light is the media tracks, and they stop
 * as soon as the frame is taken. Neither path writes a file.
 */

export interface WebcamVideo {
  srcObject: MediaProvider | null
  videoWidth: number
  videoHeight: number
  play(): Promise<void>
}

export interface WebcamCapture {
  getUserMedia(constraints: MediaStreamConstraints): Promise<MediaStream>
  createVideo(): WebcamVideo
  drawFrame(video: WebcamVideo): string
}

export async function captureWebcamStill(deps: WebcamCapture): Promise<string> {
  const stream = await deps.getUserMedia({ video: true, audio: false })

  try {
    const video = deps.createVideo()
    video.srcObject = stream
    await video.play()

    if (!video.videoWidth || !video.videoHeight) {
      throw new Error('camera produced no frame')
    }

    const data = deps.drawFrame(video)

    if (!data.startsWith('data:image/')) {
      throw new Error('webcam still was not an image')
    }

    return data
  } finally {
    for (const track of stream.getTracks()) {track.stop()}
  }
}

/** Browser webcam. The tracks are the camera light; stop() turns it off. */
export function browserWebcamCapture(): WebcamCapture {
  return {
    getUserMedia: constraints => navigator.mediaDevices.getUserMedia(constraints),
    createVideo: () => document.createElement('video'),
    drawFrame: video => {
      const canvas = document.createElement('canvas')
      canvas.width = video.videoWidth
      canvas.height = video.videoHeight
      const ctx = canvas.getContext('2d')

      if (!ctx) {throw new Error('no canvas')}
      ctx.drawImage(video as unknown as CanvasImageSource, 0, 0)

      return canvas.toDataURL('image/jpeg', 0.85)
    }
  }
}
