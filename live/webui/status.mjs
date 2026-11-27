/** Which release slot was installed last. Missing stamps sort as oldest. */

export function newestSlot(state) {
  let best = null
  let stamp = ''
  const slots = (state && state.slots) || {}
  for (const name of ['a', 'b']) {
    const installed = (slots[name] && slots[name].installed) || ''
    if (installed > stamp) {
      best = name
      stamp = installed
    }
  }
  return best
}
