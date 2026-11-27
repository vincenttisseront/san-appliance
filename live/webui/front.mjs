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
import { SESSION_SECONDS, capSetCookie, createSession, readCookie, sessionCookie, touchSession } from './session.mjs'
import { newestSlot } from './status.mjs'

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
<p>La release plus récente est appliquée toute seule quand aucune session n'écrit. Le diff est là pour être lu. Le slot le plus récent est marqué. Le bouton ci-dessous annule seulement le dernier choix.</p>
<ul>${rows}</ul>
<h2>Diff</h2>
<pre id="diff">${escapeHtml(diff)}</pre>
<p><a href="/appliance-config">Télécharger la configuration</a></p>
<p><button id="undo" type="button">Revenir au slot précédent</button></p>
<pre id="out"></pre>
</main>
<script>
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
    const forwarded = { ...incoming.headers }
    const cookies = forwarded['set-cookie']
      ? (Array.isArray(forwarded['set-cookie']) ? forwarded['set-cookie'] : [forwarded['set-cookie']])
      : []
    forwarded['set-cookie'] = [...cookies.map((item) => capSetCookie(String(item))), sessionHeader]
    res.writeHead(incoming.statusCode || 502, forwarded)
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
  const ownWrite = pathOnly === '/appliance-update' || pathOnly === '/appliance-session/end'
  if (mutating && !ownWrite) {
    beginWrite()
    res.on('close', endWrite)
  }
  try {
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
        } else {
          payload = JSON.stringify({ tag: parsed.tag || '', api: parsed.api || '' })
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
