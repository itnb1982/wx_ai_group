import { useEffect, useState } from 'react'

// 本地秒级倒计时：传入目标绝对毫秒时刻，返回剩余秒数。
export function useCountdown(targetMs) {
  const [sec, setSec] = useState(() =>
    targetMs == null ? 0 : Math.max(0, Math.floor((targetMs - Date.now()) / 1000))
  )
  useEffect(() => {
    if (targetMs == null) return
    const tick = () => setSec(Math.max(0, Math.floor((targetMs - Date.now()) / 1000)))
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [targetMs])
  return sec
}

export function fmtCountdown(sec) {
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = sec % 60
  const p = (n) => String(n).padStart(2, '0')
  return `${p(h)}:${p(m)}:${p(s)}`
}
