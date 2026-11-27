/**
 * Loopback front for the WebUI. Nitro stays on 127.0.0.1:3010.
 * /appliance-update is served here and forwarded to the root updater on 127.0.0.1:9753.
 */
import http from 'node:http'
import { spawn } from 'node:child_process'
import net from 'node:net'
import { auditResponse } from './audit-route.mjs'

const PAGE = `<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>Mise à jour SAN</title>
</head>
<body>
<main>
<h1>Mise à jour de l'application</h1>
<p>Release GitHub de vincenttisseront/san-appliance. L'empreinte est vérifiée avant écriture. Un échec laisse les exports en place. Le jeton n'est pas enregistré dans l'image.</p>
<form id="update">
<label>Version <input name="tag" placeholder="v1.2.3 ou vide pour latest"></label>
<button type="submit">Appliquer</button>
</form>
<p><a href="/appliance-audit"><button type="button">Télécharger l'audit</button></a></p>
<pre id="out"></pre>
</main>
<script>
document.getElementById("update").addEventListener("submit", async (event) => {
  event.preventDefault();
  const tag = new FormData(event.target).get("tag") || "";
  const out = document.getElementById("out");
  out.textContent = "en cours";
  const response = await fetch("/appliance-update", {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify({tag})
  });
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
  if (path === '/appliance-audit' && req.method === 'GET') {
    const answer = auditResponse(process.env.SAN_AUDIT_FILE || '/run/san/audit-export.json')
    res.writeHead(answer.status, answer.headers)
    res.end(answer.body)
    return
  }
  if (path === '/appliance-update' && req.method === 'GET') {
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
    res.end(PAGE)
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
