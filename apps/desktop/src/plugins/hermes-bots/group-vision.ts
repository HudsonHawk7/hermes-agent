/**
 * Group-chat vision receipts: turn user-attached images into a per-member
 * "saw it" / "didn't get it" chip from the gateway's real routing decision
 * (and desktop attach failures). Never invent native vision from a model name.
 */

import type { Attachment, GroupVisionReceipt } from './types'

/** Wire shape on `message.complete.image_routing`. */
export interface GroupImageRoutingWire {
  mode?: string
  count?: number
  reason?: string | null
  attached?: number | null
}

/** Image tiles only — PDFs/files are staged separately and are not vision. */
export function countGroupImageAttachments(images?: Attachment[] | null): number {
  if (!Array.isArray(images)) {
    return 0
  }

  return images.filter(img => img && typeof img.data === 'string' && img.data && img.kind !== 'pdf' && img.kind !== 'file')
    .length
}

export function groupImageAttachmentLabels(images?: Attachment[] | null): string[] {
  if (!Array.isArray(images)) {
    return []
  }

  return images
    .filter(img => img && typeof img.data === 'string' && img.data && img.kind !== 'pdf' && img.kind !== 'file')
    .map((img, i) => img.name || `attachment-${i + 1}.png`)
}

/**
 * Build a vision receipt for a member reply. Returns undefined when the turn
 * carried no images (no chip). `saw` is true only for a native route with at
 * least one attached image_url part — text fallback is a miss with reason.
 */
export function buildGroupVisionReceipt(args: {
  imageCount: number
  /** Labels of image attaches that threw or returned no success. */
  failedImageLabels?: string[]
  routing?: GroupImageRoutingWire | null
}): GroupVisionReceipt | undefined {
  const count = Math.max(0, Math.floor(Number(args.imageCount) || 0))

  if (count <= 0) {
    return undefined
  }

  const failed = (args.failedImageLabels || []).filter(Boolean)
  const routing = args.routing

  if (failed.length >= count && !routing) {
    return {
      count,
      mode: 'text',
      saw: false,
      reason: `attach failed: ${failed.join(', ')}`
    }
  }

  if (routing && typeof routing === 'object') {
    const mode = routing.mode === 'native' ? 'native' : 'text'

    const attached =
      typeof routing.attached === 'number' && Number.isFinite(routing.attached)
        ? Math.max(0, Math.floor(routing.attached))
        : undefined

    const saw = mode === 'native' && (attached === undefined ? true : attached > 0)

    if (saw) {
      return {
        count,
        mode: 'native',
        saw: true,
        ...(attached !== undefined ? { attached } : {}),
        ...(routing.reason ? { reason: String(routing.reason) } : {})
      }
    }

    const reasonParts = [
      routing.reason ? String(routing.reason) : mode === 'text' ? 'routed to text (no native vision)' : 'image not attached natively',
      failed.length ? `attach failed: ${failed.join(', ')}` : null
    ].filter(Boolean)

    return {
      count,
      mode: 'text',
      saw: false,
      reason: reasonParts.join('; ')
    }
  }

  // Images were staged but the gateway never reported routing (older server /
  // harness without the receipt). Surface attach failures only — never invent saw.
  if (failed.length) {
    return {
      count,
      mode: 'text',
      saw: false,
      reason: `attach failed: ${failed.join(', ')}`
    }
  }

  return undefined
}

/** Module-scoped last vision per member key for the round commit path. */
const pendingVisionByMember = new Map<string, GroupVisionReceipt>()

export function stashMemberTurnVision(memberKey: string, vision: GroupVisionReceipt | undefined) {
  if (!memberKey) {
    return
  }

  if (vision) {
    pendingVisionByMember.set(memberKey, vision)
  } else {
    pendingVisionByMember.delete(memberKey)
  }
}

export function takeMemberTurnVision(memberKey: string): GroupVisionReceipt | undefined {
  const vision = pendingVisionByMember.get(memberKey)
  pendingVisionByMember.delete(memberKey)

  return vision
}
