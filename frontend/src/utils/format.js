export function fmtNum(n, d = 2) {
  if (n == null || isNaN(n)) return '—'
  return Number(n).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d })
}

export function fmtMoney(n) {
  if (n == null || isNaN(n)) return '—'
  const s = n < 0 ? '-' : '+'
  return s + '$' + Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

export function holding(ht) {
  if (ht == null) return '—'
  const hh = Math.floor(ht / 60)
  const mm = ht % 60
  return hh > 0 ? `${hh}h${mm}m` : `${mm}m`
}
