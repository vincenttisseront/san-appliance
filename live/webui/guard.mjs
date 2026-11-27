/**
 * Read-only WebUI by default. Only the release account may apply a release.
 * Failed attempts lock the WebUI. They do not touch iSCSI.
 */

export const FAILURE_LIMIT = 5
export const LOCK_MS = 15 * 60 * 1000

export function parseBasic(header) {
  if (!header || !header.startsWith('Basic ')) {
    return null
  }
  let decoded
  try {
    decoded = Buffer.from(header.slice(6).trim(), 'base64').toString('utf8')
  } catch {
    return null
  }
  const index = decoded.indexOf(':')
  if (index < 0) {
    return null
  }
  return { user: decoded.slice(0, index), password: decoded.slice(index + 1) }
}

export function handleAppliance({ method, authorization, roles, lock, now }) {
  if (method !== 'POST') {
    return { status: 200, lock: normalize(lock), read: true }
  }
  return authorizeUpdate({ authorization, roles, lock, now })
}

export function authorizeUpdate({ authorization, roles, lock, now }) {
  const state = normalize(lock)
  const accounts = roles && typeof roles === 'object' ? roles : {}
  if (state.locked_until && now < state.locked_until) {
    return { status: 423, lock: state, error: 'webui verrouillée' }
  }
  if (state.locked_until && now >= state.locked_until) {
    state.failures = 0
    state.locked_until = 0
  }
  const creds = parseBasic(authorization)
  const viewer = accounts.viewer || {}
  const release = accounts.release || {}
  if (sameAccount(creds, viewer)) {
    return { status: 403, lock: state, error: 'rôle lecture seule' }
  }
  if (sameAccount(creds, release)) {
    state.failures = 0
    return { status: 200, lock: state, allow: true }
  }
  state.failures += 1
  if (state.failures >= FAILURE_LIMIT) {
    state.locked_until = now + LOCK_MS
  }
  return { status: 401, lock: state, error: 'authentification refusée' }
}

function sameAccount(creds, account) {
  if (!creds || !account || !account.user || !account.password) {
    return false
  }
  return creds.user === account.user && creds.password === account.password
}

function normalize(lock) {
  const src = lock && typeof lock === 'object' ? lock : {}
  return {
    failures: Number.isInteger(src.failures) && src.failures > 0 ? src.failures : 0,
    locked_until: Number.isInteger(src.locked_until) && src.locked_until > 0 ? src.locked_until : 0,
  }
}
