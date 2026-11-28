import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $pluginDecisions, $pluginRecords } from '@/contrib/plugins-store'
import { discoverRuntimePlugins } from '@/contrib/runtime-loader'
import type { HermesReadDirResult } from '@/global'
import { $activeProfile, $profiles } from '@/store/profile'
import { $connection } from '@/store/session'

import { reconcileAgentEnabledDesktopHalves, resetDesktopHalfReconcileState } from './reconcile-desktop-halves'

const getStatus = vi.fn(async () => ({ hermes_home: '/remote/box/.hermes' }))

/** Per-test seam over enablePackageDesktopHalf so one package can refuse while
 *  the rest still run through the real loader path. */
const { enablePackageDesktopHalf } = vi.hoisted(() => ({
  enablePackageDesktopHalf: vi.fn()
}))

vi.mock('@/hermes', async importActual => ({
  ...(await importActual()),
  getStatus: () => getStatus()
}))

vi.mock('@/contrib/plugins-store', async importActual => {
  const actual = await importActual()

  enablePackageDesktopHalf.mockImplementation(
    (packageName, opts) => actual.enablePackageDesktopHalf(packageName, opts)
  )

  return {
    ...actual,
    enablePackageDesktopHalf: (packageName, opts) => enablePackageDesktopHalf(packageName, opts)
  }
})


const desktopPluginsRoot = vi.fn<() => Promise<string>>()
const readDir = vi.fn<(path: string) => Promise<HermesReadDirResult>>()
const readFileText = vi.fn<(path: string) => Promise<{ text: string; truncated?: boolean }>>()
const watchPreviewFile = vi.fn<(path: string) => Promise<{ id: string }>>()
const logLine = vi.fn<(line: string) => void>()
const register = vi.fn()

const root = '/local/.hermes/desktop-plugins'

/** One or more unified packages on disk: each desktop half copied into the app
 *  root with the marker that makes it opt-in, exactly as Electron stamps it.
 *  The folder is the package name the two halves join on. */
function seedDiskPackages(...packages) {
  desktopPluginsRoot.mockResolvedValue(root)

  readDir.mockImplementation(async dir => {
    if (dir === root) {
      return {
        entries: packages.map(({ folder }) => ({
          isDirectory: true,
          name: folder,
          path: `${root}/${folder}`
        }))
      }
    }

    for (const { folder } of packages) {
      if (dir === `${root}/${folder}`) {
        return {
          entries: [
            { isDirectory: false, name: '.hermes-package.json', path: `${root}/${folder}/.hermes-package.json` },
            { isDirectory: false, name: 'plugin.js', path: `${root}/${folder}/plugin.js` }
          ]
        }
      }
    }

    return { entries: [] }
  })
  readFileText.mockImplementation(async file => {
    for (const { folder, pluginId } of packages) {
      if (file === `${root}/${folder}/.hermes-package.json`) {
        return {
          text: JSON.stringify({ package: folder, source: `/x/plugins/${folder}/desktop`, sourceMtimeMs: 1 })
        }
      }

      if (file === `${root}/${folder}/plugin.js`) {
        return {
          text: `export default { id: ${JSON.stringify(pluginId)}, register: globalThis.__reconcileRegister }`
        }
      }
    }

    return { text: '' }
  })
  watchPreviewFile.mockImplementation(async path => {
    const folder = packages.find(pkg => path.includes(`/${pkg.folder}/`))?.folder ?? packages[0]?.folder ?? 'pkg'

    return { id: `w-${folder}` }
  })
}

/** One unified package on disk (single-folder convenience wrapper). */
function seedDiskPackage(folder, pluginId) {
  seedDiskPackages({ folder, pluginId })
}

/** A backend answer for one profile: the agent halves it reports. */
function agentList(...rows: Array<{ name: string; status: string }>) {
  return vi.fn(async (_method: string, _params?: Record<string, unknown>) => ({
    plugins: rows.map(row => ({ description: '', version: '1', ...row }))
  }))
}

const realBlob = globalThis.Blob

beforeEach(() => {
  desktopPluginsRoot.mockReset()
  readDir.mockReset()
  readFileText.mockReset()
  watchPreviewFile.mockReset()
  logLine.mockReset()
  register.mockReset()
  getStatus.mockClear()
  enablePackageDesktopHalf.mockClear()

  enablePackageDesktopHalf.mockImplementation(async (packageName, opts) => {
    const actual = await vi.importActual('@/contrib/plugins-store')

    return actual.enablePackageDesktopHalf(packageName, opts)
  })

  $pluginRecords.set({})
  $pluginDecisions.set({})
  $activeProfile.set('default')
  $profiles.set([])
  $connection.set(null)
  resetDesktopHalfReconcileState()

  ;(globalThis as unknown as { __reconcileRegister: unknown }).__reconcileRegister = register
  ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = {
    desktopPluginsRoot,
    logLine,
    onPreviewFileChanged: vi.fn(),
    readDir,
    readFileText,
    stopPreviewFileWatch: vi.fn(async () => true),
    watchDirectory: vi.fn(async () => ({ id: 'watch' })),
    watchPreviewFile
  }

  // The loader evaluates plugins via blob-URL import(), which vite's module
  // runner cannot resolve in tests. Reroute to a data: URL, which node's native
  // ESM loader handles.
  vi.spyOn(URL, 'createObjectURL').mockImplementation(
    blob =>
      `data:text/javascript;base64,${Buffer.from((blob as unknown as { parts: string[] }).parts.join('')).toString('base64')}`
  )
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
  vi.stubGlobal(
    'Blob',
    class {
      parts: string[]
      constructor(parts: string[]) {
        this.parts = parts
      }
    }
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.stubGlobal('Blob', realBlob)
  delete (globalThis as unknown as { __reconcileRegister?: unknown }).__reconcileRegister
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
})

describe('reconcileAgentEnabledDesktopHalves', () => {
  it('turns a config-enabled agent half into a live pane without a second switch', async () => {
    seedDiskPackage('solara', 'scene-builder')
    await discoverRuntimePlugins()

    // Installed but inert, the posture a marked package ships with: the pane is
    // simply absent, which is the report this rule answers.
    expect($pluginRecords.get()['scene-builder']).toMatchObject({ packageName: 'solara', status: 'disabled' })
    expect(register).not.toHaveBeenCalled()

    const request = agentList({ name: 'solara', status: 'enabled' })
    const done = await reconcileAgentEnabledDesktopHalves(request as never)

    expect(done).toEqual([{ id: 'scene-builder', packageName: 'solara', profile: 'default' }])
    expect($pluginRecords.get()['scene-builder'].status).toBe('loaded')
    expect(register).toHaveBeenCalledTimes(1)
    expect($pluginDecisions.get()['scene-builder']).toBe(true)
  })

  it('answers the next "why can\'t I see it" from the log', async () => {
    seedDiskPackage('kessel', 'kessel-run')
    await discoverRuntimePlugins()

    await reconcileAgentEnabledDesktopHalves(agentList({ name: 'kessel', status: 'enabled' }) as never)

    expect(logLine).toHaveBeenCalledWith('[plugins] enabled desktop half kessel-run: agent half on in default')
  })

  it('leaves an explicit OFF alone even when the agent half is on', async () => {
    $pluginDecisions.set({ 'avatar-map': false })
    seedDiskPackage('avatar', 'avatar-map')
    await discoverRuntimePlugins()

    const request = agentList({ name: 'avatar', status: 'enabled' })
    const done = await reconcileAgentEnabledDesktopHalves(request as never)

    expect(done).toEqual([])
    expect($pluginRecords.get()['avatar-map'].status).toBe('disabled')
    expect(register).not.toHaveBeenCalled()
    // A recorded choice means there is nothing to ask the backend about.
    expect(request).not.toHaveBeenCalled()
  })

  it('leaves the desktop half off when no profile has the agent half enabled', async () => {
    seedDiskPackage('orbit', 'orbiter')
    await discoverRuntimePlugins()

    const request = agentList({ name: 'orbit', status: 'not enabled' })
    const done = await reconcileAgentEnabledDesktopHalves(request as never)

    expect(done).toEqual([])
    expect($pluginRecords.get().orbiter.status).toBe('disabled')
    expect(register).not.toHaveBeenCalled()
  })

  it('changes nothing on a remote backend', async () => {
    $connection.set({ mode: 'remote' } as never)
    seedDiskPackage('remote-pkg', 'remote-pane')
    await discoverRuntimePlugins()

    const request = agentList({ name: 'remote-pkg', status: 'enabled' })
    const done = await reconcileAgentEnabledDesktopHalves(request as never)

    expect(done).toEqual([])
    expect($pluginRecords.get()['remote-pane'].status).toBe('disabled')
    expect(register).not.toHaveBeenCalled()
    // The desktop half lives on THIS machine; a remote backend's plugin list
    // says nothing about it, so it is never even asked.
    expect(request).not.toHaveBeenCalled()
  })

  it('also reads a profile that is not the launch profile', async () => {
    seedDiskPackage('shared-uni', 'shared-pane')
    await discoverRuntimePlugins()
    $profiles.set([
      { has_env: false, is_default: false, model: null, name: 'lab', path: '/p/lab', provider: null, skill_count: 0 }
    ])

    const request = vi.fn(async (_method: string, params?: { profile?: string }) => ({
      plugins:
        params?.profile === 'lab'
          ? [{ description: '', key: 'shared-uni', name: 'shared-uni', status: 'enabled', version: '1' }]
          : []
    }))

    const done = await reconcileAgentEnabledDesktopHalves(request as never)

    expect(done).toEqual([{ id: 'shared-pane', packageName: 'shared-uni', profile: 'lab' }])
    expect($pluginRecords.get()['shared-pane'].status).toBe('loaded')
    expect(logLine).toHaveBeenCalledWith('[plugins] enabled desktop half shared-pane: agent half on in lab')
  })

  it('asks the backend once for an inventory it already swept', async () => {
    seedDiskPackage('poll-pkg', 'poll-pane')
    await discoverRuntimePlugins()

    // The disk poll publishes the same inventory every few seconds; a sweep
    // must not re-read every profile each time.
    const request = agentList({ name: 'poll-pkg', status: 'not enabled' })

    await reconcileAgentEnabledDesktopHalves(request as never)
    const first = request.mock.calls.length

    await reconcileAgentEnabledDesktopHalves(request as never)

    expect(first).toBeGreaterThan(0)
    expect(request.mock.calls.length).toBe(first)
  })

  it('re-checks a swept inventory when a rescan asks it to', async () => {
    seedDiskPackage('rescan-pkg', 'rescan-pane')
    await discoverRuntimePlugins()

    const request = agentList({ name: 'rescan-pkg', status: 'not enabled' })
    await reconcileAgentEnabledDesktopHalves(request as never)

    // The agent half got enabled between sweeps (another profile, another
    // window). A forced sweep must see it instead of trusting the cache.
    request.mockImplementation(async () => ({
      plugins: [{ description: '', key: 'rescan-pkg', name: 'rescan-pkg', status: 'enabled', version: '1' }]
    }))

    const done = await reconcileAgentEnabledDesktopHalves(request as never, { force: true })

    expect(done).toEqual([{ id: 'rescan-pane', packageName: 'rescan-pkg', profile: 'default' }])
    expect($pluginRecords.get()['rescan-pane'].status).toBe('loaded')
  })

  // Negative case first: without the per-package catch, one throw aborts the
  // sweep and the good half stays dark. With the catch, the good half loads
  // and the log names the failing package.
  it('one package that will not load does not keep the rest dark', async () => {
    seedDiskPackages(
      { folder: 'broken-half', pluginId: 'broken-pane' },
      { folder: 'good-half', pluginId: 'good-pane' }
    )
    await discoverRuntimePlugins()

    expect($pluginRecords.get()['broken-pane']).toMatchObject({ packageName: 'broken-half', status: 'disabled' })
    expect($pluginRecords.get()['good-pane']).toMatchObject({ packageName: 'good-half', status: 'disabled' })

    const real = (await vi.importActual('@/contrib/plugins-store')).enablePackageDesktopHalf

    enablePackageDesktopHalf.mockImplementation(async (packageName, opts) => {
      if (packageName === 'broken-half') {
        throw new Error('activate refused: missing entry')
      }

      return real(packageName, opts)
    })

    const request = agentList(
      { name: 'broken-half', status: 'enabled' },
      { name: 'good-half', status: 'enabled' }
    )

    const done = await reconcileAgentEnabledDesktopHalves(request as never)

    expect(done).toEqual([{ id: 'good-pane', packageName: 'good-half', profile: 'default' }])
    expect($pluginRecords.get()['good-pane'].status).toBe('loaded')
    expect($pluginRecords.get()['broken-pane'].status).toBe('disabled')
    expect(logLine).toHaveBeenCalledWith(
      '[plugins] could not enable desktop half broken-half: activate refused: missing entry'
    )
    expect(logLine).toHaveBeenCalledWith('[plugins] enabled desktop half good-pane: agent half on in default')
  })
})
