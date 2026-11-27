import test from 'node:test'
import assert from 'node:assert/strict'
import { renderChannels, renderLatency, renderPerf, renderScsi } from './boards.mjs'

test('scsi page drops the payload', () => {
  const token = 'PAYLOADDEADBEEF00112233445566778899'
  const html = renderScsi([{
    time: '2026-11-27T00:00:00Z',
    iqn: 'iqn.2026-11.local.san:disk',
    lun: 0,
    opcode: '0x2a',
    status: 'CHECK',
    sense_key: '0x02',
    asc: '0x04',
    ascq: '0x01',
    payload: token,
    data: token,
  }])
  assert.equal(html.includes(token), false)
  assert.equal(html.includes('0x2a'), true)
  assert.equal(html.includes('Effacer'), true)
})

test('latency page shows the global number and each lun', () => {
  const html = renderLatency({
    global_p99_us: 80,
    luns: [
      { iqn: 'iqn.2026-11.local.san:one', lun: 0, p99_us: 40, ok: true },
      { iqn: 'iqn.2026-11.local.san:two', lun: 1, ok: false, error: 'OSError' },
    ],
  })
  assert.equal(html.includes('>80<') || html.includes('80'), true)
  assert.equal(html.includes('iqn.2026-11.local.san:one'), true)
  assert.equal(html.includes('iqn.2026-11.local.san:two'), true)
  assert.equal(html.includes('OSError'), true)
})

test('perf page is the measured table and has no button', () => {
  const html = renderPerf({
    table: 'réglage\tcharge\tiops\tp99_us\tdébit_o/s\tcpu_par_go\tgardé\nbaseline\tread\t1000\t40\t100\t1\tnon\n',
  })
  assert.equal(html.includes('1000'), true)
  assert.equal(html.includes('40'), true)
  assert.equal(html.includes('<button'), false)
  assert.equal(html.includes('<form'), false)
})

test('channel page has no version field', () => {
  const html = renderChannels({
    channels: {
      app: { active: 'b', previous: 'a', p99_us: 12, signed: true, kernel: '' },
      kernel: { active: 'a', previous: null, p99_us: 12, signed: false, kernel: '6.8.0-151.151' },
    },
  })
  assert.equal(html.includes('name="tag"'), false)
  assert.equal(html.includes('Annuler app'), true)
  assert.equal(html.includes('6.8.0-151.151'), true)
})

import { spawn } from 'node:child_process'
import fs from 'node:fs'
import http from 'node:http'
import https from 'node:https'
import net from 'node:net'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer()
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address()
      server.close(() => resolve(port))
    })
    server.on('error', reject)
  })
}

function request(port, cert, urlPath, options = {}) {
  return new Promise((resolve, reject) => {
    const req = https.request({
      hostname: '127.0.0.1',
      port,
      path: urlPath,
      method: options.method || 'GET',
      ca: cert,
      headers: options.headers || {},
      rejectUnauthorized: true,
    }, (res) => {
      const chunks = []
      res.on('data', (chunk) => chunks.push(chunk))
      res.on('end', () => resolve({
        status: res.statusCode,
        headers: res.headers,
        body: Buffer.concat(chunks),
      }))
    })
    req.on('error', reject)
    if (options.body) req.write(options.body)
    req.end()
  })
}

test('the front serves the built file over TLS and a short session', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'san-front-'))
  const tls = path.join(root, 'tls')
  fs.mkdirSync(tls)
  const key = path.join(tls, 'key.pem')
  const cert = path.join(tls, 'cert.pem')
  await new Promise((resolve, reject) => {
    const child = spawn('openssl', [
      'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256',
      '-sha256', '-days', '2', '-nodes',
      '-keyout', key, '-out', cert, '-subj', '/CN=localhost',
      '-addext', 'subjectAltName=IP:127.0.0.1,DNS:localhost',
    ], { stdio: 'ignore' })
    child.on('exit', (code) => (code === 0 ? resolve() : reject(new Error('openssl ' + code))))
  })
  const conf = path.join(root, 'var', 'lib', 'san', 'conf')
  const app = path.join(conf, 'app')
  fs.mkdirSync(app, { recursive: true })
  const built = Buffer.from('ALREADY-BUILT\n')
  const configFile = path.join(conf, 'san-config.json')
  fs.writeFileSync(configFile, built)
  fs.writeFileSync(path.join(app, 'state.json'), JSON.stringify({
    version: 1,
    active: 'a',
    slots: {
      a: { installed: '2020-01-01T00:00:00Z' },
      b: { installed: '2020-06-01T00:00:00Z' },
    },
  }))
  fs.writeFileSync(path.join(app, 'diff.txt'), '~ version\n')
  const writing = path.join(root, 'var', 'lib', 'san', 'webui', 'writing')
  const upstreamPort = await freePort()
  let sawWrite = false
  const upstream = http.createServer((req, res) => {
    if (req.method === 'POST') sawWrite = fs.existsSync(writing)
    res.setHeader('set-cookie', 'auth_token=sekret; Path=/; Max-Age=604800')
    res.end('upstream')
  })
  await new Promise((resolve) => upstream.listen(upstreamPort, '127.0.0.1', resolve))
  const port = await freePort()
  const child = spawn(process.execPath, [path.join(here, 'front.mjs')], {
    env: {
      ...process.env,
      SAN_FRONT_TEST: '1',
      SAN_FRONT_PORT: String(port),
      SAN_UPSTREAM_PORT: String(upstreamPort),
      SAN_ROOT: root,
      SAN_TLS_KEY: key,
      SAN_TLS_CERT: cert,
      SAN_CONFIG_FILE: configFile,
    },
    stdio: 'ignore',
  })
  try {
    const certificate = fs.readFileSync(cert)
    let page
    for (let i = 0; i < 40; i += 1) {
      try {
        page = await request(port, certificate, '/appliance-update')
        break
      } catch {
        await new Promise((resolve) => setTimeout(resolve, 50))
      }
    }
    assert.ok(page, 'front muet')
    assert.equal(page.status, 200)
    const html = page.body.toString('utf8')
    assert.match(html, /Mise à jour/)
    assert.match(html, /le plus récent/)
    assert.match(html, /2020-06-01T00:00:00Z/)
    assert.match(html, /~ version/)
    assert.doesNotMatch(html, /name="tag"/)
    assert.doesNotMatch(html, /Appliquer/)
    assert.doesNotMatch(html, /<input/)
    const cookie = String(page.headers['set-cookie'])
    assert.match(cookie, /san_session=/)
    assert.match(cookie, /Max-Age=600/)
    assert.match(cookie, /Secure/)
    assert.match(cookie, /HttpOnly/)
    const id = cookie.match(/san_session=([a-f0-9]+)/)[1]
    const again = await request(port, certificate, '/appliance-status', {
      headers: { cookie: `san_session=${id}` },
    })
    assert.match(String(again.headers['set-cookie']), new RegExp(`san_session=${id}`))
    assert.match(again.body.toString('utf8'), /"newest":"b"/)
    const downloaded = await request(port, certificate, '/appliance-config')
    assert.equal(downloaded.status, 200)
    assert.deepEqual(downloaded.body, built)
    assert.match(String(downloaded.headers['content-disposition']), /san-config.json/)
    fs.unlinkSync(configFile)
    const missing = await request(port, certificate, '/appliance-config')
    assert.equal(missing.status, 404)
    assert.match(missing.body.toString('utf8'), /pas encore construit/)
    assert.equal(fs.existsSync(configFile), false)
    const ended = await request(port, certificate, '/appliance-session/end', {
      method: 'POST',
      headers: { cookie: `san_session=${id}` },
    })
    assert.match(String(ended.headers['set-cookie']), /Max-Age=0/)
    const fresh = await request(port, certificate, '/appliance-update')
    const freshId = String(fresh.headers['set-cookie']).match(/san_session=([a-f0-9]+)/)[1]
    assert.notEqual(freshId, id)
    const posted = await request(port, certificate, '/api/save', { method: 'POST', body: '{}' })
    assert.equal(posted.status, 200)
    assert.equal(sawWrite, true)
    assert.equal(fs.existsSync(writing), false)
    const proxiedCookie = String(posted.headers['set-cookie'])
    assert.match(proxiedCookie, /Max-Age=600/)
    assert.doesNotMatch(proxiedCookie, /604800/)
    assert.match(proxiedCookie, /san_session=/)
  } finally {
    child.kill('SIGTERM')
    upstream.close()
    fs.rmSync(root, { recursive: true, force: true })
  }
})
