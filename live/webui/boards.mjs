/** Pages loopback. Seuls les champs listés sont affichés. */

const SCSI_FIELDS = ['time', 'iqn', 'lun', 'opcode', 'status', 'sense_key', 'asc', 'ascq']

export function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[ch]))
}

function page(title, body) {
  return `<!DOCTYPE html>
<html lang="fr">
<head><meta charset="utf-8"><title>${escapeHtml(title)}</title></head>
<body><main>${body}</main></body>
</html>
`
}

export function renderScsi(records) {
  const rows = (Array.isArray(records) ? records : []).map((item) => {
    const cells = SCSI_FIELDS.map((key) => `<td>${escapeHtml(item?.[key])}</td>`).join('')
    return `<tr>${cells}</tr>`
  }).join('')
  const head = SCSI_FIELDS.map((key) => `<th>${escapeHtml(key)}</th>`).join('')
  return page('Erreurs SCSI', `
<h1>Erreurs SCSI</h1>
<p>Opcode, état et sense. La charge du LUN n'est pas enregistrée.</p>
<table><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>
<form method="post" action="/scsi-errors"><button type="submit">Effacer</button></form>
`)
}

export function renderLatency(board) {
  const global = board?.global_p99_us
  const rows = (Array.isArray(board?.luns) ? board.luns : []).map((item) => {
    const value = item?.ok ? item.p99_us : item?.error
    return `<tr><td>${escapeHtml(item?.iqn)}</td><td>${escapeHtml(item?.lun)}</td><td>${escapeHtml(value)}</td></tr>`
  }).join('')
  return page('Latence par LUN', `
<h1>Latence p99</h1>
<p>Global : ${escapeHtml(global)}</p>
<table><thead><tr><th>iqn</th><th>lun</th><th>p99_us</th></tr></thead><tbody>${rows}</tbody></table>
<form method="post" action="/latency"><button type="submit">Oublier la mesure</button></form>
`)
}

export function renderPerf(board) {
  const lines = String(board?.table || '').trim().split('\n').filter((line) => line.length > 0)
  const rows = lines.map((line) => {
    const cells = line.split('\t').map((cell) => `<td>${escapeHtml(cell)}</td>`).join('')
    return `<tr>${cells}</tr>`
  }).join('')
  return page('IOPS et p99', `
<h1>IOPS et p99</h1>
<p>Le tableau est écrit au démarrage.</p>
<table>${rows}</table>
`)
}

export function renderChannels(status) {
  const channels = status?.channels || {}
  const blocks = ['app', 'kernel'].map((name) => {
    const item = channels[name] || {}
    const undo = item.previous && item.previous !== item.active
      ? `<form method="post" action="/appliance-update"><input type="hidden" name="undo" value="${escapeHtml(name)}"><button type="submit">Annuler ${escapeHtml(name)}</button></form>`
      : ''
    return `<section><h2>${escapeHtml(name)}</h2><p>slot ${escapeHtml(item.active)} p99 ${escapeHtml(item.p99_us)} noyau ${escapeHtml(item.kernel)} signé ${escapeHtml(item.signed)}</p>${undo}</section>`
  }).join('')
  return page('Canaux', `
<h1>Mises à jour</h1>
<p>Les canaux application et noyau s'appliquent tout seuls. Le bouton annule la dernière promotion.</p>
${blocks}
`)
}
