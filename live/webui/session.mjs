/** Short local WebUI session. Ten minutes of inactivity, then a new one is issued. */

export const SESSION_SECONDS = 600

const AUTH_COOKIE = /session|token|auth|jwt/i

export function createSession(id, nowMs) {
  return { id, expires: nowMs + SESSION_SECONDS * 1000 }
}

export function touchSession(session, nowMs) {
  if (!session || typeof session.expires !== 'number' || nowMs > session.expires) {
    return null
  }
  return { id: session.id, expires: nowMs + SESSION_SECONDS * 1000 }
}

export function readCookie(header, name) {
  if (!header) return ''
  for (const part of String(header).split(';')) {
    const trimmed = part.trim()
    const eq = trimmed.indexOf('=')
    if (eq < 0) continue
    if (trimmed.slice(0, eq) === name) return trimmed.slice(eq + 1)
  }
  return ''
}

export function sessionCookie(id, maxAge) {
  const age = maxAge == null ? SESSION_SECONDS : maxAge
  return `san_session=${id}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=${age}`
}

export function capSetCookie(header, maxAge = SESSION_SECONDS) {
  if (!header) return header
  const parts = String(header).split(';').map((item) => item.trim()).filter(Boolean)
  const name = parts[0].split('=')[0]
  if (name === 'san_session' || !AUTH_COOKIE.test(name)) return header
  const kept = parts.filter((item) => !/^max-age=/i.test(item) && !/^expires=/i.test(item))
  kept.push(`Max-Age=${maxAge}`)
  if (!kept.some((item) => /^secure$/i.test(item))) kept.push('Secure')
  if (!kept.some((item) => /^httponly$/i.test(item))) kept.push('HttpOnly')
  return kept.join('; ')
}
