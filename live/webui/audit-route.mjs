/**
 * Répond avec le fichier d'audit déjà préparé. N'en crée pas.
 */
import fs from 'node:fs'

export function auditResponse(file) {
  try {
    const body = fs.readFileSync(file)
    return {
      status: 200,
      body,
      headers: {
        'content-type': 'application/json; charset=utf-8',
        'content-disposition': 'attachment; filename="audit-export.json"',
      },
    }
  } catch {
    return {
      status: 404,
      body: Buffer.from('audit non préparé'),
      headers: { 'content-type': 'text/plain; charset=utf-8' },
    }
  }
}
