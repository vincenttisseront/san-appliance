/**
 * Loopback HTTPS front for the WebUI.
 * The certificate is read from /var/lib/san/webui/tls, created at boot, outside the ISO.
 * Nitro stays on 127.0.0.1:3010. This process owns 127.0.0.1:3000.
 * A release download or this process failing does not stop iSCSI: that path is another service.
 */
import http from 'node:http'
import https from 'node:https'
import fs from 'node:fs'
import path from 'node:path'
import { spawn } from 'node:child_process'
import net from 'node:net'
import crypto from 'node:crypto'
import { auditResponse } from './audit-route.mjs'
import { handleAppliance } from './guard.mjs'
import { SESSION_SECONDS, capSetCookie, createSession, readCookie, sessionCookie, touchSession } from './session.mjs'
import { newestSlot } from './status.mjs'
import { renderChannels, renderLatency, renderPerf, renderScsi } from './boards.mjs'

const testMode = process.env.SAN_FRONT_TEST === '1'
const listenPort = Number(process.env.SAN_FRONT_PORT || 3000)
const upstreamPort = Number(process.env.SAN_UPSTREAM_PORT || 3010)

function sanPath(...parts) {
  return path.join(process.env.SAN_ROOT || '/', ...parts)
}

const tlsKey = process.env.SAN_TLS_KEY || sanPath('var', 'lib', 'san', 'webui', 'tls', 'key.pem')
const tlsCert = process.env.SAN_TLS_CERT || sanPath('var', 'lib', 'san', 'webui', 'tls', 'cert.pem')
const configFile = process.env.SAN_CONFIG_FILE || sanPath('var', 'lib', 'san', 'conf', 'san-config.json')
const stateFile = sanPath('var', 'lib', 'san', 'conf', 'app', 'state.json')
const diffFile = sanPath('var', 'lib', 'san', 'conf', 'app', 'diff.txt')
const writingFile = sanPath('var', 'lib', 'san', 'webui', 'writing')

const sessions = new Map()

function readText(file) {
  try {
    return fs.readFileSync(file, 'utf8')
  } catch {
    return ''
  }
}

function loadState() {
  const text = readText(stateFile)
  if (!text) return { active: null, slots: { a: {}, b: {} } }
  try {
    const parsed = JSON.parse(text)
    return parsed && typeof parsed === 'object' ? parsed : { active: null, slots: {} }
  } catch {
    return { active: null, slots: {}, error: 'état illisible' }
  }
}

function page() {
  const state = loadState()
  const newest = newestSlot(state)
  const slots = state.slots || {}
  const rows = ['a', 'b'].map((name) => {
    const info = slots[name] || {}
    const when = info.installed || 'sans date'
    const marks = []
    if (state.active === name) marks.push('actif')
    if (newest === name) marks.push('le plus récent')
    const mark = marks.length ? marks.join(', ') : 'inactif'
    return `<li>slot ${name} — ${when} — ${mark}</li>`
  }).join('')
  const diff = readText(diffFile) || 'aucun diff'
  return `<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>Mise à jour SAN</title>
</head>
<body>
<main>
<h1>Mise à jour de l'application</h1>
<p>La release plus récente est appliquée toute seule quand aucune session n'écrit. Le diff est là pour être lu. Le slot le plus récent est marqué. Une demande de release depuis cette page exige le compte release. Le bouton ci-dessous annule seulement le dernier choix.</p>
<p>Le boot applique une release plus récente si aucune session iSCSI n'écrit. L'empreinte est vérifiée avant écriture. Un échec laisse les exports en place. Cette page n'applique rien : elle affiche le journal et peut annuler le slot.</p>
<ul>${rows}</ul>
<h2>Diff</h2>
<pre id="diff">${escapeHtml(diff)}</pre>
<pre id="audit"></pre>
<p><a href="/appliance-config">Télécharger la configuration</a></p>
<p><a href="/appliance-audit"><button type="button">Télécharger l'audit</button></a></p>
<p><a href="/scsi-errors">Erreurs SCSI</a></p>
<p><a href="/latency">Latence</a></p>
<p><a href="/perf">IOPS et p99</a></p>
<p><a href="/channels">Canaux</a></p>
<p><a href="/exports">Exports iSCSI</a> · <a href="/nvme">NVMe-oF</a></p>
<p><button id="undo" type="button">Revenir au slot précédent</button></p>
<form id="rollback">
<button type="submit">Revenir d'un slot</button>
</form>
<pre id="out"></pre>
</main>
<script>
fetch("/appliance/audit").then((response) => response.text()).then((text) => {
  document.getElementById("audit").textContent = text;
});
document.getElementById("undo").addEventListener("click", async () => {
  const out = document.getElementById("out");
  out.textContent = "en cours";
  const response = await fetch("/appliance-update", {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify({undo: true})
  });
  out.textContent = await response.text();
});
document.getElementById("rollback").addEventListener("submit", async (event) => {
  event.preventDefault();
  const out = document.getElementById("out");
  const response = await fetch("/appliance-rollback", {method: "POST"});
  out.textContent = await response.text();
});
</script>
</body>
</html>
`
}

function escapeHtml(text) {
  return String(text)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
}

function currentSession(req, now) {
  const id = readCookie(req.headers.cookie, 'san_session')
  const known = id ? sessions.get(id) : null
  const touched = touchSession(known, now)
  if (touched) {
    sessions.set(touched.id, touched)
    return touched
  }
  if (id) sessions.delete(id)
  const created = createSession(crypto.randomBytes(18).toString('hex'), now)
  sessions.set(created.id, created)
  return created
}

function applySession(res, session, ended) {
  const header = ended ? sessionCookie('gone', 0) : sessionCookie(session.id, SESSION_SECONDS)
  const existing = res.getHeader('set-cookie')
  const list = existing == null ? [] : (Array.isArray(existing) ? existing : [existing])
  res.setHeader('set-cookie', [...list.map((item) => capSetCookie(String(item))), header])
}

function beginWrite() {
  fs.mkdirSync(path.dirname(writingFile), { recursive: true })
  fs.writeFileSync(writingFile, '1\n')
}

function endWrite() {
  try {
    fs.unlinkSync(writingFile)
  } catch {
    // already gone
  }
}

function isLocal(req) {
  const local = req.socket.localAddress
  return local === '127.0.0.1' || local === '::ffff:127.0.0.1'
}

function send(res, status, body, type) {
  res.writeHead(status, { 'content-type': type })
  res.end(body)
}

if (!testMode) {
  const childEnv = {
    ...process.env,
    NITRO_HOST: '127.0.0.1',
    HOST: '127.0.0.1',
    NUXT_HOST: '127.0.0.1',
    NITRO_PORT: String(upstreamPort),
    NUXT_PORT: String(upstreamPort),
    PORT: String(upstreamPort),
  }
  const child = spawn('/usr/bin/node', ['/usr/lib/san-webui/scripts/serve-local.mjs'], {
    env: childEnv,
    stdio: 'inherit',
  })
  child.on('exit', (code) => {
    process.exit(code == null ? 1 : code)
  })
}

function proxy(req, res, sessionHeader) {
  const headers = { ...req.headers, host: `127.0.0.1:${upstreamPort}` }
  const upstream = http.request({
    hostname: '127.0.0.1',
    port: upstreamPort,
    path: req.url,
    method: req.method,
    headers,
  }, (incoming) => {
    endWrite()
    const forwarded = { ...incoming.headers }
    const cookies = forwarded['set-cookie']
      ? (Array.isArray(forwarded['set-cookie']) ? forwarded['set-cookie'] : [forwarded['set-cookie']])
      : []
    forwarded['set-cookie'] = [...cookies.map((item) => capSetCookie(String(item))), sessionHeader]
    res.writeHead(incoming.statusCode || 502, forwarded)
    incoming.pipe(res)
  })
  upstream.on('error', () => {
    endWrite()
    if (!res.headersSent) {
      res.writeHead(502, { 'content-type': 'text/plain; charset=utf-8' })
    }
    res.end('webui indisponible')
  })
  req.pipe(upstream)
}

function publicText(text) {
  return text.split('\n').filter((line) => {
    const lower = line.toLowerCase()
    return !lower.includes('password') && !lower.includes('chap_secret') && !lower.includes('token') && !lower.includes('nuxt_')
  }).join('\n')
}

function inspectionPage(title, rows) {
  const body = rows.map((row) => {
    return [row.volume, row.lun, row.initiator, row.state, row.alert].map((cell) => String(cell ?? '')).join(' | ')
  }).join('\n')
  return `<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"><title>${title}</title></head><body><main><h1>${title}</h1><pre>${publicText(body) || 'rien à afficher'}</pre><p><a href="/appliance-update">Mise à jour</a></p></main></body></html>`
}

function statusRows(kind) {
  try {
    const data = JSON.parse(readText('/run/san/public-status.json') || '{}')
    return Array.isArray(data[kind]) ? data[kind] : []
  } catch {
    return []
  }
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

const ROLES_PATH = process.env.SAN_WEBUI_ROLES || '/run/san/webui-roles.json'
const LOCK_PATH = process.env.SAN_WEBUI_LOCK || '/var/lib/san/webui/lock.json'

function readJson(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'))
  } catch {
    return fallback
  }
}

function writeLock(lock) {
  try {
    fs.mkdirSync(LOCK_PATH.replace(/\/[^/]+$/, ''), { recursive: true })
    fs.writeFileSync(LOCK_PATH, JSON.stringify(lock) + '\n', { mode: 0o640 })
  } catch {
    // Un verrou illisible ne doit pas arrêter la page, ni iSCSI.
  }
}


function channelView(file) {
  const state = readJson(file, null)
  if (!state || typeof state !== 'object') return {}
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

let key
let cert
try {
  key = fs.readFileSync(tlsKey)
  cert = fs.readFileSync(tlsCert)
} catch (err) {
  process.stderr.write(`certificat WebUI absent: ${err.message}\n`)
  process.exit(1)
}

const server = https.createServer({ key, cert }, async (req, res) => {
  if (!isLocal(req)) {
    res.writeHead(403)
    res.end()
    return
  }
  const now = Date.now()
  const pathOnly = (req.url || '/').split('?')[0]
  const ended = pathOnly === '/appliance-session/end' && req.method === 'POST'
  const session = currentSession(req, now)
  if (ended) sessions.delete(session.id)
  const header = ended ? sessionCookie('gone', 0) : sessionCookie(session.id, SESSION_SECONDS)
  const mutating = ['POST', 'PUT', 'PATCH', 'DELETE'].includes(req.method || '')
  const ownWrite = pathOnly === '/appliance-update' || pathOnly === '/appliance-session/end' || pathOnly === '/appliance-rollback'
  if (mutating && !ownWrite) {
    beginWrite()
    res.on('finish', endWrite)
    res.on('close', endWrite)
  }
  try {
    if (pathOnly === '/appliance/audit' && req.method === 'GET') {
      applySession(res, session, ended)
      const journal = publicText(readText(sanPath('var', 'lib', 'san', 'conf', 'app', 'audit.jsonl')))
      send(res, 200, journal || 'aucune mise à jour\n', 'text/plain; charset=utf-8')
      return
    }
    if (pathOnly === '/exports' && req.method === 'GET') {
      applySession(res, session, ended)
      send(res, 200, inspectionPage('Exports iSCSI', statusRows('exports')), 'text/html; charset=utf-8')
      return
    }
    if (pathOnly === '/nvme' && req.method === 'GET') {
      applySession(res, session, ended)
      send(res, 200, inspectionPage('NVMe-oF', statusRows('nvme')), 'text/html; charset=utf-8')
      return
    }
    if (pathOnly === '/appliance-rollback' && req.method === 'POST') {
      const answer = await askUpdater(JSON.stringify({ op: 'rollback' }))
      applySession(res, session, ended)
      send(res, 200, answer, 'application/json; charset=utf-8')
      return
    }
    if (pathOnly === '/appliance-audit' && req.method === 'GET') {
      const answer = auditResponse(process.env.SAN_AUDIT_FILE || '/run/san/audit-export.json')
      for (const [key, value] of Object.entries(answer.headers || {})) {
        res.setHeader(key, value)
      }
      applySession(res, session, ended)
      res.writeHead(answer.status)
      res.end(answer.body)
      return
    }
    if (pathOnly === '/scsi-errors' && req.method === 'GET') {
      const board = process.env.SAN_SCSI_BOARD || sanPath('var', 'lib', 'san', 'webui', 'boards', 'scsi-errors.json')
      applySession(res, session, ended)
      send(res, 200, renderScsi(readJson(board, [])), 'text/html; charset=utf-8')
      return
    }
    if (pathOnly === '/latency' && req.method === 'GET') {
      const board = process.env.SAN_LATENCY_BOARD || sanPath('var', 'lib', 'san', 'webui', 'boards', 'latency.json')
      applySession(res, session, ended)
      send(res, 200, renderLatency(readJson(board, { global_p99_us: null, luns: [] })), 'text/html; charset=utf-8')
      return
    }
    if (pathOnly === '/perf' && req.method === 'GET') {
      const board = process.env.SAN_PERF_BOARD || sanPath('var', 'lib', 'san', 'webui', 'boards', 'perf.json')
      applySession(res, session, ended)
      send(res, 200, renderPerf(readJson(board, { table: '' })), 'text/html; charset=utf-8')
      return
    }
    if (pathOnly === '/channels' && req.method === 'GET') {
      applySession(res, session, ended)
      send(res, 200, renderChannels({
        channels: {
          app: channelView(sanPath('var', 'lib', 'san', 'conf', 'app', 'state.json')),
          kernel: channelView(sanPath('var', 'lib', 'san', 'conf', 'kernel', 'state.json')),
        },
      }), 'text/html; charset=utf-8')
      return
    }
    if (pathOnly === '/appliance-update' && req.method === 'GET') {
      applySession(res, session, ended)
      send(res, 200, page(), 'text/html; charset=utf-8')
      return
    }
    if (pathOnly === '/appliance-status' && req.method === 'GET') {
      const state = loadState()
      applySession(res, session, ended)
      send(res, 200, JSON.stringify({
        active: state.active || null,
        newest: newestSlot(state),
        slots: state.slots || {},
        diff: readText(diffFile),
      }), 'application/json; charset=utf-8')
      return
    }
    if (pathOnly === '/appliance-config' && req.method === 'GET') {
      if (!fs.existsSync(configFile)) {
        applySession(res, session, ended)
        send(res, 404, '{"error":"export pas encore construit"}\n', 'application/json; charset=utf-8')
        return
      }
      const body = fs.readFileSync(configFile)
      applySession(res, session, ended)
      res.writeHead(200, {
        'content-type': 'application/json; charset=utf-8',
        'content-disposition': 'attachment; filename="san-config.json"',
      })
      res.end(body)
      return
    }
    if (pathOnly === '/appliance-session/end' && req.method === 'POST') {
      applySession(res, session, true)
      send(res, 200, '{"ended":true}\n', 'application/json; charset=utf-8')
      return
    }
    if ((pathOnly === '/scsi-errors' || pathOnly === '/latency' || pathOnly === '/channels') && req.method === 'POST') {
      const op = pathOnly === '/scsi-errors' ? 'clear-scsi' : (pathOnly === '/latency' ? 'clear-latency' : '')
      let undo = ''
      if (pathOnly === '/channels') {
        const chunks = []
        for await (const chunk of req) chunks.push(chunk)
        const raw = Buffer.concat(chunks).toString('utf8')
        try {
          const parsed = JSON.parse(raw || '{}')
          undo = parsed.undo || ''
        } catch {
          const params = new URLSearchParams(raw)
          undo = params.get('undo') || ''
        }
        if (undo !== 'app' && undo !== 'kernel') {
          applySession(res, session, ended)
          send(res, 400, '{"applied":false,"error":"demande refusée"}\n', 'application/json')
          return
        }
      }
      const message = op ? { op } : { undo }
      const answer = await askUpdater(JSON.stringify(message))
      applySession(res, session, ended)
      send(res, 200, answer, 'application/json; charset=utf-8')
      return
    }
    if (pathOnly === '/appliance-update' && req.method === 'POST') {
      const chunks = []
      let size = 0
      for await (const chunk of req) {
        size += chunk.length
        if (size > 8192) {
          applySession(res, session, ended)
          res.writeHead(413)
          res.end()
          return
        }
        chunks.push(chunk)
      }
      let payload = Buffer.concat(chunks).toString('utf8')
      try {
        const parsed = JSON.parse(payload || '{}')
        if (parsed.token || parsed.github_token) {
          applySession(res, session, ended)
          send(res, 400, '{"applied":false,"error":"jeton refusé dans la demande"}\n', 'application/json')
          return
        }
        if (parsed.undo === true) {
          payload = JSON.stringify({ undo: true })
        } else if (parsed.undo === 'app' || parsed.undo === 'kernel') {
          payload = JSON.stringify({ undo: parsed.undo })
        } else if (parsed.op === 'clear-scsi' || parsed.op === 'clear-latency' || parsed.op === 'rollback') {
          payload = JSON.stringify({ op: parsed.op })
        } else {
          const decision = handleAppliance({
            method: 'POST',
            authorization: req.headers.authorization,
            roles: readJson(ROLES_PATH, {}),
            lock: readJson(LOCK_PATH, {}),
            now: Date.now(),
          })
          writeLock(decision.lock)
          if (!decision.allow) {
            applySession(res, session, ended)
            send(res, decision.status, JSON.stringify({ applied: false, error: decision.error || 'refusé' }) + '\n', 'application/json; charset=utf-8')
            return
          }
          applySession(res, session, ended)
          send(res, 400, '{"applied":false,"error":"la mise à jour est automatique"}\n', 'application/json')
          return
        }
      } catch {
        applySession(res, session, ended)
        send(res, 400, '{"applied":false,"error":"demande refusée"}\n', 'application/json')
        return
      }
      const answer = await askUpdater(payload)
      applySession(res, session, ended)
      send(res, 200, answer, 'application/json; charset=utf-8')
      return
    }
    proxy(req, res, header)
  } catch (err) {
    if (!res.headersSent) {
      applySession(res, session, ended)
      send(res, 500, 'erreur\n', 'text/plain; charset=utf-8')
    }
    throw err
  }
})

server.listen(listenPort, '127.0.0.1')
