import assert from 'node:assert/strict'
import test from 'node:test'
import {
  SESSION_SECONDS,
  capSetCookie,
  createSession,
  readCookie,
  sessionCookie,
  touchSession,
} from './session.mjs'
import { newestSlot } from './status.mjs'

test('a session lasts ten minutes and a later touch extends it', () => {
  assert.equal(SESSION_SECONDS, 600)
  const started = createSession('abc', 1_000)
  assert.equal(started.expires, 1_000 + 600_000)
  const next = touchSession(started, 1_000 + 60_000)
  assert.equal(next.id, 'abc')
  assert.equal(next.expires, 1_000 + 60_000 + 600_000)
})

test('an idle session is not reused', () => {
  const started = createSession('abc', 1_000)
  assert.equal(touchSession(started, started.expires + 1), null)
  assert.equal(touchSession(null, 5), null)
})

test('ending a session clears the cookie', () => {
  const header = sessionCookie('gone', 0)
  assert.match(header, /Max-Age=0/)
  assert.match(header, /HttpOnly/)
  assert.match(header, /Secure/)
  assert.equal(readCookie(header, 'san_session'), 'gone')
})

test('an upstream auth cookie is shortened', () => {
  const capped = capSetCookie('auth_token=secret; Path=/; Max-Age=604800', 600)
  assert.match(capped, /Max-Age=600/)
  assert.doesNotMatch(capped, /604800/)
  assert.match(capped, /Secure/)
  assert.equal(capSetCookie('esos_theme=dark; Path=/', 600), 'esos_theme=dark; Path=/')
})

test('the newest slot is the later timestamp', () => {
  const state = {
    active: 'a',
    slots: {
      a: { installed: '2020-01-01T00:00:00Z' },
      b: { installed: '2020-06-01T00:00:00Z' },
    },
  }
  assert.equal(newestSlot(state), 'b')
  assert.equal(newestSlot({ slots: { a: {}, b: {} } }), null)
})
