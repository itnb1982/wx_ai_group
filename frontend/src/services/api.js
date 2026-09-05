// 万象AI 仪表盘 API 层
// 开发时通过 Vite 代理 /api -> http://127.0.0.1:8081 转发，规避 CORS。
// 生产由后端在 8081 同源托管，前端用相对路径 /api 直连，无需代理。
const BASE = '/api'
const TOKEN_KEY = 'wx_token'

// ---------- Token 管理（localStorage 持久化） ----------
export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || ''
}
export function setToken(t) {
  if (t) localStorage.setItem(TOKEN_KEY, t)
  else localStorage.removeItem(TOKEN_KEY)
}

// 401 专用错误：触发全局退回登录，不走 mock 降级
export class AuthError extends Error {
  constructor(msg) {
    super(msg)
    this.name = 'AuthError'
  }
}

// ---------- 登录 ----------
export async function login(email, password) {
  const r = await fetch(BASE + '/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  if (!r.ok) {
    let msg = '登录失败'
    try { msg = (await r.json()).detail || msg } catch (e) {}
    throw new Error(msg)
  }
  const data = await r.json()
  setToken(data.access_token)
  return data.user
}

// ---------- 注册 ----------
export async function register(email, username, password) {
  const r = await fetch(BASE + '/auth/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, username, password }),
  })
  if (!r.ok) {
    let msg = '注册失败'
    try { msg = (await r.json()).detail || msg } catch (e) {}
    throw new Error(msg)
  }
  const data = await r.json()
  setToken(data.access_token)
  return data.user
}

// ---------- 通用请求（支持 method/body，自动带 token，401 触发登出） ----------
// opts.timeout: 请求超时毫秒，默认 12000。fetch 原生无超时，需用 AbortController。
export async function getJSON(path, opts = {}) {
  const { timeout = 12000, ...rest } = opts
  const headers = { 'Content-Type': 'application/json' }
  const tk = getToken()
  if (tk) headers['Authorization'] = 'Bearer ' + tk

  const controller = new AbortController()
  const to = setTimeout(() => controller.abort(), timeout)
  let r
  try {
    r = await fetch(BASE + path, { ...rest, headers, signal: controller.signal })
    clearTimeout(to)
  } catch (e) {
    clearTimeout(to)
    if (e.name === 'AbortError') {
      throw new Error('请求超时')
    }
    throw e // 网络不可达 → 交给调用方降级（mock）
  }
  if (r.status === 401) {
    setToken('')
    if (typeof window !== 'undefined') window.dispatchEvent(new Event('auth:logout'))
    throw new AuthError('登录已失效，请重新登录')
  }
  if (!r.ok) {
    let msg = 'HTTP ' + r.status
    try { msg = (await r.json()).detail || msg } catch (e) {}
    throw new Error(msg)
  }
  if (r.status === 204) return {}
  return r.json()
}

// ---------- 仪表盘 ----------
export function fetchSession() {
  return getJSON('/dashboard/market-session')
}
export function fetchChart(tf) {
  return getJSON('/dashboard/market-chart?tf=' + tf)
}
export function fetchAccounts() {
  return getJSON('/dashboard/accounts')
}
// AI 工作剧场：辩论 / 进化 / 交易执行流（三类真实数据）
export function fetchAIFlow(opts = {}) {
  return getJSON('/dashboard/ai-flow', opts)
}
// 系统健康检查（故障报警用）
export function fetchSystemHealth() {
  return getJSON('/dashboard/system-health')
}
// 风控事件流：为什么没开单（拦截/熔断记录 + 按原因聚合排行）
export function fetchRiskEvents(accountId = '', limit = 40) {
  const q = new URLSearchParams()
  if (accountId) q.set('account_id', accountId)
  q.set('limit', String(limit))
  return getJSON('/dashboard/risk-events?' + q.toString())
}
// 组合盈利/净值曲线（按日累计净盈亏，P0-2 可视化数据源）
export function fetchEquityCurve(days = 30) {
  return getJSON('/dashboard/equity-curve?days=' + days)
}
// AI Key 用量统计（多 Key 聚合 + token / 费用）
export function fetchAIUsage() {
  return getJSON('/dashboard/ai-usage')
}
// 3 个模型工作原理（自洽性解释）
export function fetchModelWorkflow() {
  return getJSON('/dashboard/model-workflow')
}

// ---------- 信号源参考（多模型时序预测 → fusion_v2 融合票接入决策链，权重0.22） ----------
export function fetchTsReference(opts = {}) {
  return getJSON('/ts-reference/snapshot', opts)
}

export function selftestTsReferenceModel(name) {
  return getJSON(`/ts-reference/${encodeURIComponent(name)}/selftest`, { timeout: 45000 })
}

// ---------- 账户管理 ----------
export function listAccounts() {
  return getJSON('/accounts/')
}
export function connectAccount(id) {
  return getJSON('/accounts/' + id + '/connect', { method: 'POST' })
}
export function syncAccount(id) {
  return getJSON('/accounts/' + id + '/sync', { method: 'POST' })
}
export function toggleTrading(id, enabled) {
  return getJSON('/accounts/' + id + '/toggle-trading?enabled=' + (enabled ? 'true' : 'false'), { method: 'POST' })
}
export function getPositions(id) {
  return getJSON('/accounts/' + id + '/positions')
}
export function addAccount(payload) {
  return getJSON('/accounts/', { method: 'POST', body: JSON.stringify(payload) })
}
export function deleteAccount(id) {
  return getJSON('/accounts/' + id, { method: 'DELETE' })
}
export function updateAccount(id, payload) {
  return getJSON('/accounts/' + id, { method: 'PUT', body: JSON.stringify(payload) })
}
export function setPrimary(id) {
  return getJSON('/accounts/' + id + '/set-primary', { method: 'POST' })
}
export function allAccountStatus() {
  return getJSON('/accounts/status')
}

// ---------- MT5 终端发现（自动扫描硬盘上的 MT5 安装） ----------
export function discoverMt5() {
  return getJSON('/mt5/discover')
}

// ---------- 策略风控 ----------
export function getStrategy(accountId) {
  return getJSON('/strategy/' + accountId)
}
export function updateStrategy(accountId, payload) {
  return getJSON('/strategy/' + accountId, { method: 'PUT', body: JSON.stringify(payload) })
}

// ---------- AI Key 管理 ----------
export function listKeys() {
  return getJSON('/keys/')
}
export function addKey(payload) {
  return getJSON('/keys/', { method: 'POST', body: JSON.stringify(payload) })
}
export function deleteKey(id) {
  return getJSON('/keys/' + id, { method: 'DELETE' })
}
export function toggleKey(id, active) {
  return getJSON('/keys/' + id + '/toggle?active=' + (active ? 'true' : 'false'), { method: 'POST' })
}
export function cloudStatus() {
  return getJSON('/keys/cloud-status')
}
export function cloudToggle(enabled) {
  return getJSON('/keys/cloud-toggle', { method: 'POST', body: JSON.stringify({ enabled }) })
}

// ---------- 本地双核（Qwen3-8B 校对员 / Chronos-2 时序） ----------
// 说明：本地模型是【增强项】不是【依赖项】。这些接口在 Ollama 完全没装的
// 机器上也会正常返回结构化的「未启用」，前端据此渲染安装指引即可，
// 不要按「请求失败」处理。
export function localModelStatus(opts = {}) {
  return getJSON('/local-model/status', opts)
}
export function localModelWarm() {
  return getJSON('/local-model/warm', { method: 'POST' })
}
export function localModelSelftest() {
  return getJSON('/local-model/selftest', { method: 'POST' })
}

// ---------- 视觉第四票（知觉模型 qwen2.5vl:3b） ----------
// 说明：视觉模型是决策链的【加法增强】(第5路)，后台低频渲染 H4/M15 图表→
// 送视觉模型(CPU)识别结构→缓存 VisionVote，meta_agent 同步读取融合提方向准确率。
// 这些接口与本地双核同理：模型未装/未启动时也会正常返回结构化「未启用/读图中」，
// 前端据此渲染状态，不要按「请求失败」处理。
export function visionStatus(opts = {}) {
  return getJSON('/dashboard/vision-status', opts)
}
