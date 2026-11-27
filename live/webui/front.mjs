/**
 * Loopback front for the WebUI. Nitro stays on 127.0.0.1:3010.
 * /appliance-update is served here and forwarded to the root updater on 127.0.0.1:9753.
 */
import http from 'node:http'
import fs from 'node:fs'
import { spawn } from 'node:child_process'
import net from 'node:net'

const PAGE = `<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>Mise à jour SAN</title>
</head>
<body>
<main>
<h1>Mise à jour de l'application</h1>
<p>Le boot applique une release plus récente si aucune session iSCSI n'écrit. L'empreinte est vérifiée avant écriture. Un échec laisse les exports en place. Cette page n'applique rien : elle affiche le journal et peut annuler le slot.</p>
<pre id="audit"></pre>
<form id="undo">
<button type="submit">Revenir d'un slot</button>
</form>
<p><a href="/exports">Exports iSCSI</a> · <a href="/nvme">NVMe-oF</a></p>
<pre id="out"></pre>
</main>
<script>
fetch("/appliance/audit").then((response) => response.text()).then((text) => {
  document.getElementById("audit").textContent = text;
});
document.getElementById("undo").addEventListener("submit", async (event) => {
  event.preventDefault();
  const out = document.getElementById("out");
  const response = await fetch("/appliance-rollback", {method: "POST"});
  out.textContent = await response.text();
});
</script>
</body>
</html>
`

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

function readText(file) {
  try {
    return fs.readFileSync(file, 'utf8')
  } catch {
    return ''
  }
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

const server = http.createServer(async (req, res) => {
  const local = req.socket.localAddress
  if (local !== '127.0.0.1' && local !== '::ffff:127.0.0.1') {
    res.writeHead(403)
    res.end()
    return
  }
  const path = (req.url || '/').split('?')[0]
  if (path === '/appliance-update' && req.method === 'GET') {
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
    res.end(PAGE)
    return
  }
  if (path === '/appliance/audit' && req.method === 'GET') {
    res.writeHead(200, { 'content-type': 'text/plain; charset=utf-8' })
    res.end(publicText(readText('/var/lib/san/conf/app/audit.jsonl')) || 'aucune mise à jour\n')
    return
  }
  if (path === '/exports' && req.method === 'GET') {
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
    res.end(inspectionPage('Exports iSCSI', statusRows('exports')))
    return
  }
  if (path === '/nvme' && req.method === 'GET') {
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
    res.end(inspectionPage('NVMe-oF', statusRows('nvme')))
    return
  }
  if (path === '/appliance-rollback' && req.method === 'POST') {
    const answer = await askUpdater(JSON.stringify({ op: 'rollback' }))
    res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' })
    res.end(answer)
    return
  }
  if (path === '/appliance-update' && req.method === 'POST') {
    const chunks = []
    let size = 0
    for await (const chunk of req) {
      size += chunk.length
      if (size > 8192) {
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
        res.writeHead(400, { 'content-type': 'application/json' })
        res.end('{"applied":false,"error":"jeton refusé dans la demande"}\n')
        return
      }
      payload = JSON.stringify({ tag: parsed.tag || '', api: parsed.api || '' })
    } catch {
      res.writeHead(400, { 'content-type': 'application/json' })
      res.end('{"applied":false,"error":"demande refusée"}\n')
      return
    }
    const answer = await askUpdater(payload)
    res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' })
    res.end(answer)
    return
  }
  proxy(req, res)
})

server.listen(3000, '127.0.0.1')
