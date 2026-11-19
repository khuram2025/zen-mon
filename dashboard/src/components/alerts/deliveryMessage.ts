/**
 * "Message sent" model for the incident delivery list (migrate-197, T-01).
 *
 * The delivery log keeps the subject and the text exactly as handed to the
 * transport. For SMS the length decides how many parts the phone receives:
 * GSM-7 fits 160 characters in one message and 153 per part when split;
 * any character outside GSM-7 (an em dash, Arabic, an emoji) switches the
 * whole text to UCS-2: 70 in one message, 67 per part. Characters of the
 * GSM-7 extension table (^ { } \ [ ] ~ | €) take two of the 160.
 *
 * Imports nothing, so scripts/test-delivery-message.mjs runs it without the app.
 */

const GSM7_BASIC =
  '@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !"#¤%&\'()*+,-./0123456789:;<=>?' +
  '¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà'
const GSM7_EXTENDED = '^{}\\[~]|€\f'

export type SmsEncoding = 'GSM-7' | 'UCS-2'

export type SmsLength = {
  encoding: SmsEncoding
  /** Characters the recipient sees. */
  chars: number
  /** Units the network counts: GSM-7 septets or UTF-16 code units. */
  units: number
  /** Parts the phone receives (0 for an empty text). */
  segments: number
  /** Units that fit in one message, or in each part once split. */
  perSegment: number
}

export function smsLength(text: string): SmsLength {
  const chars = [...text].length
  let septets = 0
  let gsm = true
  for (const ch of text) {
    if (GSM7_BASIC.includes(ch)) septets += 1
    else if (GSM7_EXTENDED.includes(ch)) septets += 2
    else { gsm = false; break }
  }
  if (gsm) {
    const perSegment = septets <= 160 ? 160 : 153
    return { encoding: 'GSM-7', chars, units: septets, segments: septets === 0 ? 0 : Math.ceil(septets / perSegment), perSegment }
  }
  const units = text.length // UTF-16 code units: an emoji takes two
  const perSegment = units <= 70 ? 70 : 67
  return { encoding: 'UCS-2', chars, units, segments: Math.ceil(units / perSegment), perSegment }
}

/** One line under an SMS: "142 characters · UCS-2 · 3 messages". */
export function smsLengthLabel(len: SmsLength): string {
  const parts = len.segments === 1 ? '1 message' : `${len.segments} messages`
  return `${len.chars.toLocaleString('en-US')} character${len.chars === 1 ? '' : 's'} · ${len.encoding} · ${parts}`
}

/** Why an SMS was split, in words; null when it fits in one message. */
export function smsSplitNote(len: SmsLength): string | null {
  if (len.segments <= 1) return null
  const single = len.encoding === 'GSM-7' ? 160 : 70
  const why = len.encoding === 'UCS-2'
    ? 'it contains characters outside the basic SMS alphabet, so one message holds only 70'
    : 'one message holds 160'
  return `Longer than ${single}: ${why}. The phone receives ${len.segments} parts; some gateways and phones cut it after the first.`
}

export type DeliveryMessageInput = {
  channel_type: string | null
  subject?: string | null
  content?: string | null
  content_withheld?: boolean
  recipients_count?: number | null
}

export type DeliveryMessageView = {
  subject: string | null
  content: string | null
  /** The text exists but this user may not read it (needs alerts.manage). */
  withheld: boolean
  recipients: string | null
  sms: SmsLength | null
}

/** What the "Message sent" panel shows; null when nothing was recorded. */
export function deliveryMessage(d: DeliveryMessageInput): DeliveryMessageView | null {
  const subject = d.subject || null
  const content = d.content || null
  const withheld = !!d.content_withheld && !content
  if (!subject && !content && !withheld) return null
  const n = d.recipients_count
  return {
    subject,
    content,
    withheld,
    recipients: n == null ? null : `${n.toLocaleString('en-US')} recipient${n === 1 ? '' : 's'}`,
    sms: d.channel_type === 'sms' && content ? smsLength(content) : null,
  }
}
