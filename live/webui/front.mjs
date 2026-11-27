/**
 * Loopback front for the WebUI. Nitro stays on 127.0.0.1:3010.
 * Appliance pages are served here. The updater on 127.0.0.1:9753 only undoes.
 */
import http from 'node:http'
import fs from 'node:fs'
import { spawn } from 'node:child_process'
import net from 'node:net'
import { renderChannels, renderLatency, renderScsi } from './boards.mjs'

const BOARDS = '/var/lib/san/webui/boards'
const APP_STATE = '/var/lib/san/conf/app/state.json'
const KERNEL_STATE = '/var/lib/san/conf/kernel/state.json'

function readJson(path, fallback) {
  try {
    return JSON.parse(fs.readFileSync(path, 'utf8'))
  } catch {
    return fallback
  }
}

function channelView(path) {
  const state = readJson(path, null)
  if (!state || typeof state !== 'object') {
    return {}
  }
  const active = state.active
  const slot = (state.slots && state.slots[active]) || {}
  return {
    active,
    previous: state.previous || null,
    p99_us: slot.p99_us,
    kernel: slot.kernel || '',
    signed: Boolean(state.signature),
    signing_gap: state.signing_gap || '',
  }
}

const childEnv = {
  ...process.env,
  NITRO_HOST: '127.0.0.1',
  HOST: '127.0.0.1',
  NUXT_HOST: '127.0.0.1',
  NITRO_PORT: '3010',
  NUXT_PORT: '3010',
  PORT: '3010',
}
const child = spawn('/usr/bin/node', ['/usr/lib/san-webui/scripts/serve-local.mjs'], {
  env: childEnv,
  stdio: 'inherit',
})
child.on('exit', (code) => {
  process.exit(code == null ? 1 : code)
})

function proxy(req, res) {
  const headers = { ...req.headers, host: '127.0.0.1:3010' }
  const upstream = http.request({
    hostname: '127.0.0.1',
    port: 3010,
    path: req.url,
    method: req.method,
    headers,
  }, (incoming) => {
    res.writeHead(incoming.statusCode || 502, incoming.headers)
    incoming.pipe(res)
  })
  upstream.on('error', () => {
    if (!res.headersSent) {
      res.writeHead(502, { 'content-type': 'text/plain; charset=utf-8' })
    }
    res.end('webui indisponible')
  })
  req.pipe(upstream)
}

function askUpdater(body) {
  return new Promise((resolve) => {
    const client = net.connect({ host: '127.0.0.1', port: 9753 }, () => {
      client.end(body + '\n')
    })
    const chunks = []
    client.on('data', (chunk) => chunks.push(chunk))
    client.on('error', () => resolve('{"applied":false,"error":"mise à jour injoignable"}\n'))
    client.on('end', () => resolve(Buffer.concat(chunks).toString('utf8') || '{"applied":false}\n'))
  })
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = []
    let size = 0
    req.on('data', (chunk) => {
      size += chunk.length
      if (size > 8192) {
        reject(Object.assign(new Error('too big'), { status: 413 }))
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    req.on('error', reject)
  })
}

function html(res, body) {
  res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
  res.end(body)
}

const server = http.createServer(async (req, res) => {
  const local = req.socket.localAddress
  if (local !== '127.0.0.1' && local !== '::ffff:127.0.0.1') {
    res.writeHead(403)
    res.end()
    return
  }
  const path = (req.url || '/').split('?')[0]
  if (path === '/scsi-errors' && req.method === 'GET') {
    html(res, renderScsi(readJson(`${BOARDS}/scsi-errors.json`, [])))
    return
  }
  if (path === '/latency' && req.method === 'GET') {
    html(res, renderLatency(readJson(`${BOARDS}/latency.json`, { global_p99_us: null, luns: [] })))
    return
  }
  if (path === '/appliance-update' && req.method === 'GET') {
    html(res, renderChannels({
      channels: { app: channelView(APP_STATE), kernel: channelView(KERNEL_STATE) },
    }))
    return
  }
  if ((path === '/scsi-errors' || path === '/latency' || path === '/appliance-update') && req.method === 'POST') {
    let raw = ''
    try {
      raw = await readBody(req)
    } catch (err) {
      res.writeHead(err.status || 400)
      res.end()
      return
    }
    let payload = {}
    const contentType = String(req.headers['content-type'] || '')
    if (contentType.includes('application/json') && raw) {
      try {
        payload = JSON.parse(raw)
      } catch {
        res.writeHead(400, { 'content-type': 'application/json' })
        res.end('{"applied":false,"error":"demande refusée"}\n')
        return
      }
    } else if (raw.includes('undo=')) {
      const params = new URLSearchParams(raw)
      payload = { undo: params.get('undo') || '' }
    }
    if (payload.token || payload.github_token || payload.tag) {
      res.writeHead(400, { 'content-type': 'application/json' })
      res.end('{"applied":false,"error":"la mise à jour est automatique"}\n')
      return
    }
    let message = {}
    if (path === '/scsi-errors') message = { op: 'clear-scsi' }
    else if (path === '/latency') message = { op: 'clear-latency' }
    else if (payload.undo === 'app' || payload.undo === 'kernel') message = { undo: payload.undo }
    else {
      res.writeHead(400, { 'content-type': 'application/json' })
      res.end('{"applied":false,"error":"demande refusée"}\n')
      return
    }
    const answer = await askUpdater(JSON.stringify(message))
    res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' })
    res.end(answer)
    return
  }
  proxy(req, res)
})

server.listen(3000, '127.0.0.1')
