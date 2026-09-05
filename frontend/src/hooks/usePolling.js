import { useCallback, useEffect, useRef, useState } from 'react'

// 通用轮询 hook：首次立即拉取，之后每 interval 毫秒重复。
// fn 抛错时不更新 data（保留上次值），并通过 onError 回调暴露降级时机。
export function usePolling(fn, interval, onError) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const fnRef = useRef(fn)
  fnRef.current = fn

  const run = useCallback(async () => {
    try {
      const d = await fnRef.current()
      setData(d)
    } catch (e) {
      if (onError) onError(e)
    } finally {
      setLoading(false)
    }
  }, [onError])

  // 自调度取代 setInterval：本轮结束后才排下一轮，保证同一时刻只有一次请求在飞。
  // （setInterval 不等待上一轮完成，接口慢于 interval 时会持续堆积并雪崩打爆后端，
  //   2026-08-09 主面板已因此瘫痪一次，此处一并从通用 hook 层面根治。）
  useEffect(() => {
    let cancelled = false
    let timer = null
    const tick = async () => {
      await run()
      if (!cancelled) timer = setTimeout(tick, interval)
    }
    tick()
    return () => { cancelled = true; if (timer) clearTimeout(timer) }
  }, [run, interval])

  return { data, loading, rerun: run }
}
