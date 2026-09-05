/**
 * 授权状态单一订阅源（V6 Phase 8.4）
 *
 * 为什么要做成模块级单例而不是每个组件各 useEffect 拉一遍：
 *   侧栏徽章、顶部提示条、激活页三处都要状态。若各拉各的，
 *   ①三倍请求量；②三处刷新时机不同 → 徽章说"已授权"而横幅还挂着红条，
 *   客户当场就不信任这个系统了。状态不一致比状态错误更伤信任。
 *
 * 后端 /license/status 自带 30s 缓存，前端 60s 轮询即可，不会打穿。
 */
import { useEffect, useState } from 'react'
import { fetchLicenseStatus } from '../services/license.js'

const POLL_MS = 60_000

let _state = null          // 最近一次成功的快照
let _timer = null
let _inflight = null
const _subs = new Set()

function _emit() {
  _subs.forEach((fn) => {
    try { fn(_state) } catch (e) { /* 单个订阅者抛错不得连累其它订阅者 */ }
  })
}

async function _refresh() {
  // 并发去重：多个组件同时挂载时只发一个请求
  if (_inflight) return _inflight
  _inflight = (async () => {
    try {
      const r = await fetchLicenseStatus()
      if (r && r.success && r.data) {
        _state = r.data
        _emit()
      }
    } catch (e) {
      // ★ 拉不到状态时**保持上一次快照不变**，绝不退化成"未授权"。
      //   网络抖动一下就给客户弹红条"授权已失效"是灾难级误报。
    } finally {
      _inflight = null
    }
  })()
  return _inflight
}

function _ensureTimer() {
  if (_timer) return
  const tick = async () => {
    await _refresh()
    if (_subs.size > 0) _timer = setTimeout(tick, POLL_MS)
    else _timer = null
  }
  _timer = setTimeout(tick, POLL_MS)
}

function _maybeStopTimer() {
  if (_subs.size === 0 && _timer) {
    clearTimeout(_timer)
    _timer = null
  }
}

/** 激活成功后主动打通知，三处 UI 同时更新，不用等下一轮询 */
export function refreshLicense() {
  return _refresh()
}

export function useLicense() {
  const [st, setSt] = useState(_state)

  useEffect(() => {
    _subs.add(setSt)
    _ensureTimer()
    if (_state === null) _refresh()
    else setSt(_state)
    return () => {
      _subs.delete(setSt)
      _maybeStopTimer()
    }
  }, [])

  return st
}

export default useLicense
