/**
 * Screenshot still for the group-chat 🖥 button.
 *
 * Returns a data URL and writes no file. Only a trusted Hermes window may ask.
 */
import { desktopCapturer, ipcMain, type WebContents } from 'electron'

const CHANNEL = 'hermes:group-screenshot'

function trusted(contents: WebContents | null | undefined): boolean {
  if (!contents || contents.isDestroyed()) {return false}

  try {
    const url = new URL(contents.getURL())

    return url.protocol === 'file:' || url.hostname === '127.0.0.1' || url.hostname === 'localhost'
  } catch {
    return false
  }
}

export function installGroupPictureCapture(): void {
  ipcMain.removeHandler(CHANNEL)
  ipcMain.handle(CHANNEL, async event => {
    if (!trusted(event.sender)) {
      throw new Error('screenshot refused: untrusted window')
    }

    const sources = await desktopCapturer.getSources({
      types: ['screen'],
      thumbnailSize: { width: 1280, height: 720 }
    })

    const source = sources.find(item => !item.thumbnail.isEmpty())

    if (!source) {throw new Error('no screen')}
    const url = source.thumbnail.toDataURL()

    if (!url.startsWith('data:image/')) {throw new Error('screenshot was not an image')}

    return url
  })
}
