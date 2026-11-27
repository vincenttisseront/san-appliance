import assert from 'node:assert/strict'
import http from 'node:http'
import test from 'node:test'
import { FAILURE_LIMIT, LOCK_MS, handleAppliance } from './guard.mjs'

const roles = {
  viewer: { user: 'viewer', password: 'viewer-secret' },
  release: { user: 'release', password: 'release-secret' },
}

function basic(user, password) {
  return 'Basic ' + Buffer.from(`${user}:${password}`).toString('base64')
}

function listen(handler) {
  return new Promise((resolve) => {
    const server = http.createServer(handler)
    server.listen(0, '127.0.0.1', () => resolve(server))
  })
}

function request(port, method, authorization) {
  return new Promise((resolve, reject) => {
    const req = http.request({
      hostname: '127.0.0.1',
      port,
      path: '/appliance-update',
      method,
      headers: authorization ? { authorization } : {},
    }, (res) => {
      const chunks = []
      res.on('data', (chunk) => chunks.push(chunk))
      res.on('end', () => resolve({
        status: res.statusCode,
        body: Buffer.concat(chunks).toString('utf8'),
      }))
    })
    req.on('error', reject)
    req.end()
  })
}

test('viewer can read and cannot apply a release', async () => {
  const box = { lock: { failures: 0, locked_until: 0 }, now: 1_000 }
  const server = await listen((req, res) => {
    const decision = handleAppliance({
      method: req.method,
      authorization: req.headers.authorization,
      roles,
      lock: box.lock,
      now: box.now,
    })
    box.lock = decision.lock
    res.writeHead(decision.status, { 'content-type': 'application/json' })
    res.end(JSON.stringify({ error: decision.error || '', allow: Boolean(decision.allow) }))
  })
  const port = server.address().port
  try {
    const read = await request(port, 'GET')
    assert.equal(read.status, 200)
    const viewer = await request(port, 'POST', basic('viewer', 'viewer-secret'))
    assert.equal(viewer.status, 403)
    assert.match(viewer.body, /lecture seule/)
    assert.equal(box.lock.failures, 0)
    const release = await request(port, 'POST', basic('release', 'release-secret'))
    assert.equal(release.status, 200)
    assert.match(release.body, /"allow":true/)
  } finally {
    server.close()
  }
})

test('failed release attempts lock the webui and then expire', async () => {
  const box = { lock: { failures: 0, locked_until: 0 }, now: 5_000 }
  const server = await listen((req, res) => {
    const decision = handleAppliance({
      method: 'POST',
      authorization: req.headers.authorization,
      roles,
      lock: box.lock,
      now: box.now,
    })
    box.lock = decision.lock
    res.writeHead(decision.status)
    res.end(decision.error || 'ok')
  })
  const port = server.address().port
  try {
    for (let i = 0; i < FAILURE_LIMIT; i += 1) {
      const failed = await request(port, 'POST', basic('release', 'wrong'))
      assert.equal(failed.status, 401)
    }
    assert.equal(box.lock.locked_until, box.now + LOCK_MS)
    const locked = await request(port, 'POST', basic('release', 'release-secret'))
    assert.equal(locked.status, 423)
    box.now = box.lock.locked_until
    const open = await request(port, 'POST', basic('release', 'release-secret'))
    assert.equal(open.status, 200)
  } finally {
    server.close()
  }
})
