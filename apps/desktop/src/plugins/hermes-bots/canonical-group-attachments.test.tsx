import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, expect, it, vi } from 'vitest'

import { expectDownloaded, observeDownloads } from './canonical-download-test-utils'

const request = vi.hoisted(() => vi.fn())
vi.mock('@hermes/plugin-sdk', () => ({
  host: { requestProfile: request },
  Button: (props: ComponentProps<'button'>) => <button {...props} />
}))
vi.mock('./canonical-group-labels', () => ({
  useCanonicalGroupLabels: () => ({ attachFiles: 'Attach files', download: 'Download',
    removeAttachment: 'Remove', uploadFailed: 'Attachment failed', previewFailed: 'Could not load preview' })
}))

import { CanonicalGroupAttachments } from './canonical-group-attachments'

const binding = { connectionId: 'remote-owner', profile: 'reviewer', roomId: 'room' }
const originalDesktop = window.hermesDesktop
afterEach(() => { cleanup(); request.mockReset(); vi.restoreAllMocks(); vi.unstubAllGlobals(); window.hermesDesktop = originalDesktop })

it.each([32, 1_048_576])('uploads all %i bytes using the canonical owner method', async size => {
  const bytes = Uint8Array.from({ length: size }, (_, index) => index % 256)
  const file = new File([bytes], 'report.bin', { type: 'application/octet-stream' })
  // jsdom lacks Blob.arrayBuffer; preserve its real FileReader implementation.
  Object.defineProperty(file, 'arrayBuffer', { value: async () => bytes.buffer })
  const uploaded = { attachment_id: 'file-one', kind: 'file', name: file.name, mime: file.type, size }
  request.mockImplementation(async (route, method, params) => {
    expect(route).toMatchObject({ connectionId: binding.connectionId, targetProfile: binding.profile })

    if (method !== 'groups.attachment.upload') {throw new Error('Unknown gateway method')}
    expect(params).toMatchObject({ profile: binding.profile, room_id: binding.roomId, name: file.name })
    const decoded = Uint8Array.from(atob(params.data_base64), value => value.charCodeAt(0))
    expect(Buffer.from(decoded).equals(Buffer.from(bytes))).toBe(true)

    return { ...uploaded, sha256: 'receipt-digest', state: 'uploaded', created_at: 123,
      idempotent: false, room_id: binding.roomId, authority: { gateway_id: 'owner', epoch: 1 } }
  })
  const changed = vi.fn()
  const submit = vi.fn(event => event.preventDefault())
  const view = render(<form onSubmit={submit}><CanonicalGroupAttachments attachments={[]} binding={binding} disabled={false} onChange={changed} /></form>)
  fireEvent.click(screen.getByRole('button', { name: 'Attach files' }))
  fireEvent.change(view.container.querySelector('input')!, { target: { files: [file] } })
  await waitFor(() => expect(changed).toHaveBeenCalledWith([uploaded]), { timeout: 2000 })
  expect(request).toHaveBeenCalledTimes(1)
  expect(submit).not.toHaveBeenCalled()
})

it('downloads only the selected committed attachment from its captured owner', async () => {
  const attachment = { attachment_id: 'file-one', event_id: 'event-one', kind: 'file', name: 'report.bin', mime: 'application/octet-stream' }
  const save = vi.fn().mockResolvedValue(undefined)
  const observed = observeDownloads()
  window.hermesDesktop = { saveImageBuffer: save } as unknown as typeof window.hermesDesktop
  request.mockImplementation(async (route, method, params) => {
    expect(route).toMatchObject({ connectionId: binding.connectionId, targetProfile: binding.profile })

    if (method !== 'groups.attachment.download') {throw new Error('Unknown gateway method')}
    expect(params).toEqual({ profile: binding.profile, room_id: binding.roomId,
      event_id: attachment.event_id, attachment_id: attachment.attachment_id })

    return { ...attachment, data_base64: 'AAEC/w==' }
  })
  const submit = vi.fn(event => event.preventDefault())
  const changed = vi.fn()
  render(<form onSubmit={submit}><CanonicalGroupAttachments attachments={[attachment]} binding={binding} disabled={false} onChange={changed} /></form>)
  fireEvent.click(screen.getByRole('button', { name: 'Download' }))
  await waitFor(() => expect(observed.downloads).toHaveLength(1))
  await expectDownloaded(observed, new Uint8Array([0, 1, 2, 255]), attachment.name, attachment.mime)
  expect(save).not.toHaveBeenCalled()
  expect(request).toHaveBeenCalledTimes(1)
  await waitFor(() => expect((screen.getByRole('button', { name: 'Remove' }) as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(screen.getByRole('button', { name: 'Remove' }))
  expect(changed).toHaveBeenCalledWith([])
  expect(submit).not.toHaveBeenCalled()
})

function observeViewport() {
  const observers: IntersectionObserverCallback[] = []

  vi.stubGlobal('IntersectionObserver', class {
    constructor(callback: IntersectionObserverCallback) { observers.push(callback) }
    observe() {}
    disconnect() {}
    unobserve() {}
    takeRecords() { return [] }
  })

  return { scrollIntoView: () => act(() => observers.forEach(notify => notify([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver))) }
}

const image = { attachment_id: 'image-one', event_id: 'event-one', kind: 'image', name: 'picture.png', mime: 'image/png' }
const video = { attachment_id: 'video-one', event_id: 'event-one', kind: 'video', name: 'clip.mp4', mime: 'video/mp4' }

it('renders downloaded member images and videos inline once they are near the viewport, retaining Download', async () => {
  const createObjectURL = vi.fn(() => 'blob:member-media')
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL: vi.fn() })
  const viewport = observeViewport()
  request.mockImplementation(async (_route, method, params) => {
    expect(method).toBe('groups.attachment.download')

    return { ...(params.attachment_id === image.attachment_id ? image : video), data_base64: 'AAEC/w==' }
  })

  const view = render(<CanonicalGroupAttachments attachments={[image, video]} binding={binding} disabled={false} readOnly />)

  expect(request).not.toHaveBeenCalled()
  expect(screen.getAllByRole('button', { name: 'Download' })).toHaveLength(2)
  viewport.scrollIntoView()
  await waitFor(() => expect(screen.getByRole('img', { name: image.name })).not.toBeNull())
  expect(view.container.querySelector('video')).not.toBeNull()
  expect(request).toHaveBeenCalledTimes(2)
  const blobs = (createObjectURL.mock.calls as unknown as Blob[][]).map(([blob]) => [blob.size, blob.type])
  expect(blobs).toEqual(expect.arrayContaining([[4, 'image/png'], [4, 'video/mp4']]))
})

it('fetches no preview while the room is disabled, then fetches once it is enabled', async () => {
  vi.stubGlobal('URL', { createObjectURL: vi.fn(() => 'blob:member-media'), revokeObjectURL: vi.fn() })
  const viewport = observeViewport()
  request.mockResolvedValue({ ...image, data_base64: 'AAEC/w==' })
  const props = { attachments: [image], binding, readOnly: true as const }
  const view = render(<CanonicalGroupAttachments {...props} disabled />)

  viewport.scrollIntoView()
  await act(async () => { await Promise.resolve() })
  expect(request).not.toHaveBeenCalled()
  view.rerender(<CanonicalGroupAttachments {...props} disabled={false} />)
  await waitFor(() => expect(screen.getByRole('img', { name: image.name })).not.toBeNull())
  expect(request).toHaveBeenCalledTimes(1)
})

it('says the preview could not load, next to Download', async () => {
  vi.stubGlobal('URL', { createObjectURL: vi.fn(() => 'blob:member-media'), revokeObjectURL: vi.fn() })
  const viewport = observeViewport()
  request.mockRejectedValue(new Error('gateway offline'))
  render(<CanonicalGroupAttachments attachments={[image]} binding={binding} disabled={false} readOnly />)

  viewport.scrollIntoView()

  const alert = await screen.findByRole('alert')
  expect(alert.textContent).toBe('Could not load preview: gateway offline')
  expect(screen.getByRole('button', { name: 'Download' })).not.toBeNull()
  expect(screen.queryByRole('img')).toBeNull()
})

it('says the preview could not load when the bytes are not valid base64', async () => {
  vi.stubGlobal('URL', { createObjectURL: vi.fn(() => 'blob:member-media'), revokeObjectURL: vi.fn() })
  const viewport = observeViewport()
  request.mockResolvedValue({ ...image, data_base64: '***not base64***' })
  render(<CanonicalGroupAttachments attachments={[image]} binding={binding} disabled={false} readOnly />)

  viewport.scrollIntoView()

  expect((await screen.findByRole('alert')).textContent).toContain('Could not load preview')
})

it('revokes the preview object URL when the room unmounts', async () => {
  const revokeObjectURL = vi.fn()
  vi.stubGlobal('URL', { createObjectURL: vi.fn(() => 'blob:member-media'), revokeObjectURL })
  const viewport = observeViewport()
  request.mockResolvedValue({ ...image, data_base64: 'AAEC/w==' })
  const view = render(<CanonicalGroupAttachments attachments={[image]} binding={binding} disabled={false} readOnly />)

  viewport.scrollIntoView()
  await waitFor(() => expect(screen.getByRole('img', { name: image.name })).not.toBeNull())
  expect(revokeObjectURL).not.toHaveBeenCalled()
  view.unmount()

  expect(revokeObjectURL).toHaveBeenCalledWith('blob:member-media')
})
