import test from 'node:test'
import assert from 'node:assert/strict'
import { renderChannels, renderLatency, renderScsi } from './boards.mjs'

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
