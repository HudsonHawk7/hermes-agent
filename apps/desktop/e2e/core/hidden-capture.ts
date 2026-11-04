import { createHash } from 'node:crypto'
import fs from 'node:fs'

import type { ElectronApplication, Page } from 'playwright'

type WindowDump = {
  id: number
  visible: boolean
  url: string
  title: string
}

export async function listBrowserWindows(app: ElectronApplication): Promise<WindowDump[]> {
  return app.evaluate(({ BrowserWindow }) =>
    BrowserWindow.getAllWindows()
      .filter(w => !w.isDestroyed())
      .map(w => ({
        id: w.id,
        visible: w.isVisible(),
        url: w.webContents.getURL(),
        title: w.getTitle(),
      })),
  )
}

/** Main-window PNG via Electron `webContents.capturePage()` (not an OS screenshot). */
export async function captureMainWindowPng(app: ElectronApplication): Promise<Buffer> {
  const b64 = await app.evaluate(async ({ BrowserWindow }) => {
    const win = BrowserWindow.getAllWindows().find(w => !w.isDestroyed())

    if (!win) {
      throw new Error('webContents.capturePage: no BrowserWindow')
    }

    win.webContents.invalidate()
    await win.webContents.executeJavaScript(
      'new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))',
    )
    const image = await win.webContents.capturePage()

    if (image.isEmpty()) {
      throw new Error('webContents.capturePage returned an empty image')
    }

    return image.toPNG().toString('base64')
  })

  return Buffer.from(b64, 'base64')
}

export async function visibleBrowserWindowCount(app: ElectronApplication): Promise<number> {
  return app.evaluate(({ BrowserWindow }) =>
    BrowserWindow.getAllWindows().filter(w => !w.isDestroyed() && w.isVisible()).length,
  )
}

export function sha256Hex(buf: Buffer): string {
  return createHash('sha256').update(buf).digest('hex')
}

/** Poll capturePage until the PNG hash leaves `previousSha` (hidden paints can lag). */
export async function waitForCaptureChange(
  app: ElectronApplication,
  page: Page,
  previousSha: string,
  timeoutMs = 8_000,
  minBytes = 2_000,
): Promise<{ png: Buffer; sha: string }> {
  const deadline = Date.now() + timeoutMs
  let png = Buffer.alloc(0)
  let sha = ''

  while (Date.now() < deadline) {
    png = await captureMainWindowPng(app)
    sha = sha256Hex(png)

    if (sha !== previousSha && png.byteLength >= minBytes) {
      return { png, sha }
    }

    await page.waitForTimeout(150)
  }

  throw new Error(
    `capturePage did not change from ${previousSha.slice(0, 12)} last=${sha.slice(0, 12)} bytes=${png.byteLength} minBytes=${minBytes}`,
  )
}

/**
 * Inject the fixture clip into the SAME BrowserWindow capturePage reads.
 * Playwright's page.evaluate can target a different webContents than getAllWindows()[0].
 */
export async function playFixtureVideoOnCapturedWindow(
  app: ElectronApplication,
  filePath: string,
): Promise<{ duration: number; readyState: number }> {
  const b64 = fs.readFileSync(filePath).toString('base64')

  return app.evaluate(async ({ BrowserWindow }, payload: { b64: string }) => {
    const win = BrowserWindow.getAllWindows().find(w => !w.isDestroyed())

    if (!win) {
      throw new Error('playFixtureVideoOnCapturedWindow: no BrowserWindow')
    }

    const src = JSON.stringify(payload.b64)

    return win.webContents.executeJavaScript(`(async () => {
      const b64 = ${src};
      document.getElementById('hermes-hidden-capture-video')?.remove();
      const binary = atob(b64);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      const blob = new Blob([bytes], { type: 'video/mp4' });
      const video = document.createElement('video');
      video.id = 'hermes-hidden-capture-video';
      video.setAttribute('data-testid', 'hidden-capture-video');
      video.src = URL.createObjectURL(blob);
      video.muted = true;
      video.autoplay = true;
      video.playsInline = true;
      video.preload = 'auto';
      video.controls = false;
      video.loop = true;
      video.style.cssText = 'position:fixed;inset:0;width:100vw;height:100vh;object-fit:contain;background:#000;z-index:2147483647';
      document.body.appendChild(video);
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('video loadeddata timeout')), 10000);
        video.addEventListener('error', () => {
          clearTimeout(timer);
          reject(new Error('video error ' + (video.error && video.error.code)));
        }, { once: true });
        video.addEventListener('loadeddata', () => {
          clearTimeout(timer);
          resolve();
        }, { once: true });
      });
      await video.play();
      return { duration: video.duration, readyState: video.readyState };
    })()`)
  }, { b64 })
}

export async function seekCapturedWindowVideo(
  app: ElectronApplication,
  timeSeconds: number,
): Promise<number> {
  const script = `(async () => {
      const video = document.getElementById('hermes-hidden-capture-video');
      if (!video) throw new Error('hidden-capture-video missing');
      video.pause();
      video.currentTime = ${Number(timeSeconds)};
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('seek timeout')), 5000);
        video.addEventListener('seeked', () => {
          clearTimeout(timer);
          resolve();
        }, { once: true });
      });
      return video.currentTime;
    })()`

  return app.evaluate(async ({ BrowserWindow }, src: string) => {
    const win = BrowserWindow.getAllWindows().find(w => !w.isDestroyed())

    if (!win) {
      throw new Error('seekCapturedWindowVideo: no BrowserWindow')
    }

    return win.webContents.executeJavaScript(src)
  }, script)
}
