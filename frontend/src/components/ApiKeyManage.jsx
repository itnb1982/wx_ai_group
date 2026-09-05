import { useState, useEffect, useCallback } from 'react'
import { listKeys, addKey, deleteKey, toggleKey, cloudStatus, cloudToggle, fetchAIUsage, fetchModelWorkflow } from '../services/api'

// AI Key 管理：DeepSeek / 混元 多 Key 管理 + token 用量 + 模型工作流
const PROVIDERS = [
  { v: 'deepseek', label: 'DeepSeek V4', color: '#4f8cff', desc: '技术分析 · 趋势/动量' },
  { v: 'hunyuan',  label: '腾讯混元 Hy3', color: '#28c0a0', desc: '金融建模 · 风险量化' },
]
const provOf = (v) => PROVIDERS.find((p) => p.v === v) || { label: v, color: '#888', desc: '' }

const fmtNum = (n) => {
  if (n === null || n === undefined) return '0'
  return Number(n).toLocaleString('zh-CN', { maximumFractionDigits: 0 })
}
const fmtCost = (n) => {
  if (!n) return '$0.0000'
  return '$' + Number(n).toFixed(4)
}

export default function ApiKeyManage() {
  const [keys, setKeys] = useState([])
  const [envFallbacks, setEnvFallbacks] = useState([])
  const [usage, setUsage] = useState(null)
  const [workflow, setWorkflow] = useState(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [showAdd, setShowAdd] = useState(false)
  const [form, setForm] = useState({ provider: 'deepseek', key_name: '', api_key: '', secret_key: '' })
  const [busy, setBusy] = useState(false)
  const [cloud, setCloud] = useState(null)
  // 【2026-08-08 修复】原本这里自己从 localStorage 的 'wxai_user' 里刨 token。
  // 但 api.js 存的键是 'wx_token'、存的是裸字符串——'wxai_user' 这个键
  // 全站从来没有被写入过，取出来恒为 ''，于是下面的「启用/停用」请求发的是
  // `Bearer `（空 token），后端返 401，按钮点了没反应、也不弹错。
  // 教训：鉴权只允许有一个出口。组件一律走 services/api.js 的封装，
  // 任何地方都不许自己拼 Authorization 头。

  const load = useCallback(async () => {
    try {
      const [data, u, w, c] = await Promise.all([listKeys(), fetchAIUsage().catch(() => null), fetchModelWorkflow().catch(() => null), cloudStatus().catch(() => null)])
      // 后端 list_keys() 现在返回 {keys:[], env_fallbacks:[]}
      // 兼容：旧版直接返回数组
      let dbKeys = []
      let envKeys = []
      if (Array.isArray(data)) {
        dbKeys = data
      } else if (data && typeof data === 'object') {
        dbKeys = data.keys || []
        envKeys = data.env_fallbacks || []
      }
      setKeys([...dbKeys, ...envKeys])
      setEnvFallbacks(envKeys)
      setUsage(u)
      setWorkflow(w)
      setCloud(c)
      setErr('')
    } catch (e) {
      if (e.name !== 'AuthError') setErr(e.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])
  // 用量每 10s 轮询（轻量，仅 /ai-usage）
  useEffect(() => {
    let alive = true
    let timer = null
    const tick = async () => {
      try {
        const [u, w] = await Promise.all([fetchAIUsage().catch(() => null), fetchModelWorkflow().catch(() => null)])
        if (!alive) return
        if (u) setUsage(u)
        if (w) setWorkflow(w)
      } catch {}
      if (alive) timer = setTimeout(tick, 10000)
    }
    tick()
    return () => { alive = false; if (timer) clearTimeout(timer) }
  }, [])

  const submit = async (e) => {
    e.preventDefault()
    if (!form.key_name.trim() || !form.api_key.trim()) {
      setErr('请填写密钥名称与 API Key')
      return
    }
    setBusy(true); setErr('')
    try {
      await addKey(form)
      setForm({ provider: 'deepseek', key_name: '', api_key: '', secret_key: '' })
      setShowAdd(false)
      load()
    } catch (e) {
      setErr(e.message || '添加失败')
    } finally {
      setBusy(false)
    }
  }

  const onToggle = async (k) => {
    setBusy(true); setErr('')
    try {
      // 走统一封装：自动带 token、自动处理 401 登出
      const data = await toggleKey(k.id, !k.is_active)
      if (data.is_active) {
        // 启用成功（已通过验证）
        setErr('')
      } else if (data.error) {
        // 启用失败（验证未通过）
        setErr(`启用失败：${data.error}`)
      } else {
        // 禁用
        setErr('')
      }
      load()
    } catch (e) {
      setErr(e.message || '操作失败')
    } finally {
      setBusy(false)
    }
  }
  const onDelete = async (k) => {
    if (!window.confirm('确认删除密钥「' + k.key_name + '」？此操作不可恢复。')) return
    try { await deleteKey(k.id); load() } catch (e) { setErr(e.message || '删除失败') }
  }

  const onCloudToggle = async () => {
    if (!cloud) return
    setBusy(true); setErr('')
    try {
      const c = await cloudToggle(!cloud.master_enabled)
      setCloud(c)
      setErr('')
    } catch (e) {
      setErr(e.message || '云模型开关切换失败')
    } finally {
      setBusy(false)
    }
  }

  const statusBadge = (k) => {
    if (k.is_env_fallback) return { t: '.env 回退 · 工作中', c: '#ffd56b' }
    if (!k.is_active) return { t: '已禁用', c: 'var(--dim)' }
    return { t: '正常', c: 'var(--green)' }
  }

  // 用量聚合（按 provider 分组）
  const usageByProvider = (provider) => {
    if (!usage?.pools?.[provider]) {
      return { items: [], agg: { calls_total: 0, calls_today: 0, total_tokens: 0, total_cost_usd: 0 } }
    }
    return usage.pools[provider]
  }

  return (
    <div className="wrap">
      {/* ── 云模型总开关（客户自选） ── */}
      {cloud && (
        <div className="panel" style={{
          padding: 16, marginBottom: 14,
          borderColor: cloud.effective_enabled ? 'rgba(79,140,255,.4)' : 'rgba(46,230,160,.4)',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
            <div style={{ flex: '1 1 auto', minWidth: 240 }}>
              <div className="h" style={{ marginBottom: 4 }}>
                ☁️ 云模型总开关（已冻结）
                <span style={{ marginLeft: 8, fontSize: 10, color: 'var(--dim)', fontWeight: 400 }}>
                  云端方案已永久弃用 · 纯本地决策
                </span>
              </div>
              <div style={{ fontSize: 13, color: '#2ee6a0', fontWeight: 700 }}>
                纯本地运行 · 云端已弃用
              </div>
              <div style={{ fontSize: 11, color: 'var(--dim)', marginTop: 4, lineHeight: 1.6 }}>
                2026-08-19 用户决策：云端 DS/HY 永久弃用。决策链 100% 本地（视觉 + Chronos 锚 + qwen3:8b 校验 + L2 本地反向平仓）。此开关已冻结。
              </div>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 6 }}>
              <button
                className={'btn'}
                disabled
                style={{ minWidth: 130, opacity: .6, cursor: 'not-allowed' }}
                title="云端方案已永久弃用，此开关已冻结"
              >
                🔒 已冻结 · 纯本地
              </button>
              <span style={{ fontSize: 10, color: 'var(--dim)' }}>
                决策链：视觉 + Chronos 锚 + 本地加权
              </span>
            </div>
          </div>
          {/* provider 可用性 */}
          <div style={{ display: 'flex', gap: 10, marginTop: 12, flexWrap: 'wrap' }}>
            {Object.entries(cloud.providers || {}).map(([p, info]) => (
              <span key={p} style={{
                fontSize: 11, padding: '3px 10px', borderRadius: 20,
                color: info.available ? 'var(--blue)' : 'var(--dim)',
                background: info.available ? 'rgba(79,140,255,.1)' : 'rgba(255,255,255,.04)',
                border: '1px solid ' + (info.available ? 'rgba(79,140,255,.3)' : 'var(--line)'),
              }}>
                {p === 'deepseek' ? 'DeepSeek' : '混元'} · {info.available ? info.source : '无可用 Key'}
              </span>
            ))}
          </div>
          {cloud.master_enabled && !cloud.effective_enabled && (
            <div style={{
              marginTop: 10, fontSize: 11, color: '#ffcf4d', padding: '8px 10px', borderRadius: 8,
              background: 'rgba(255,207,77,.08)', border: '1px solid rgba(255,207,77,.3)', lineHeight: 1.6,
            }}>
              ℹ 云端方案已永久弃用（2026-08-19 决策），系统以<b>全本地模式</b>运行，决策链 100% 本地模型，零云成本。
            </div>
          )}
        </div>
      )}

      {/* ── 模型工作原理卡片（自洽性解释） ── */}
      {workflow && (
        <div className="panel" style={{ padding: 16, marginBottom: 14 }}>
          <div className="h">
            <span style={{ color: 'var(--gold)', fontWeight: 900 }}>🤖</span> 3 个模型如何工作 · 自洽机制
            <span style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--dim)' }}>
              上次共识：<b style={{ color: workflow.last_consensus === 'strong' ? '#4caf50' : workflow.last_consensus === 'disagreement' ? '#f44336' : '#ffd56b' }}>
                {workflow.last_consensus || 'unknown'}
              </b>
            </span>
          </div>
          {/* 三角色卡 */}
          <div className="wf-roles">
            {workflow.roles?.map((r, i) => (
              <div key={i} className={`wf-role wf-role-${i}`}>
                <div className="wf-role-head">
                  <div className="wf-role-name">{r.name}</div>
                  <div className="wf-role-alias">{r.alias}</div>
                </div>
                <div className="wf-role-desc">{r.role_desc}</div>
                <div className="wf-role-stats">
                  <div className="wf-stat">
                    <div className="wf-stat-k">权重</div>
                    <div className="wf-stat-v" style={{ color: '#ffd56b' }}>{(r.weight * 100).toFixed(0)}%</div>
                  </div>
                  <div className="wf-stat">
                    <div className="wf-stat-k">近期准确率</div>
                    <div className="wf-stat-v">{(r.recent_accuracy * 100).toFixed(0)}%</div>
                  </div>
                  <div className="wf-stat">
                    <div className="wf-stat-k">最近</div>
                    <div className="wf-stat-v" style={{
                      color: r.last_decision === 'BUY' ? '#4caf50' : r.last_decision === 'SELL' ? '#f44336' : 'var(--dim)',
                    }}>
                      {r.last_decision === '-' ? '-' : r.last_decision}
                      {r.last_confidence > 0 && <span style={{ color: 'var(--dim)', fontSize: 9 }}> · {(r.last_confidence * 100).toFixed(0)}%</span>}
                    </div>
                  </div>
                </div>
              </div>
            ))}
          </div>
          {/* 流程 + 自洽机制 */}
          <div className="wf-flow">
            <div className="wf-flow-col">
              <div className="wf-flow-h">🔁 决策流程</div>
              {workflow.flow?.map((s, i) => (
                <div key={i} className="wf-flow-step">{s}</div>
              ))}
            </div>
            <div className="wf-flow-col">
              <div className="wf-flow-h">⚖️ 自洽机制</div>
              {workflow.self_consistency && Object.entries(workflow.self_consistency).map(([k, v]) => (
                <div key={k} className="wf-cons-row">
                  <span className="wf-cons-k">{({
                    model: '调度',
                    decision_alignment: '对齐',
                    risk_override: '风险',
                    evolution_loop: '进化',
                  })[k]}</span>
                  <span className="wf-cons-v">{v}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* ── Token 用量总览 ── */}
      {usage && (
        <div className="panel" style={{ padding: 16, marginBottom: 14 }}>
          <div className="h">
            💰 Token 用量与成本 · 实时聚合
            <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--dim)' }}>
              调用 <b style={{ color: 'var(--txt)' }}>{fmtNum(usage.aggregate?.calls_total)}</b> 次 ·
              今日 <b style={{ color: 'var(--txt)' }}>{fmtNum(usage.aggregate?.calls_today)}</b> 次 ·
              Tokens <b style={{ color: 'var(--txt)' }}>{fmtNum(usage.aggregate?.total_tokens)}</b> ·
              成本 <b style={{ color: 'var(--gold)' }}>{fmtCost(usage.aggregate?.total_cost_usd)}</b>
            </span>
          </div>
          <div className="usage-grid">
            {PROVIDERS.map((p) => {
              const u = usageByProvider(p.v)
              const items = u.items || []
              const agg = u.aggregate || {}
              return (
                <div key={p.v} className="usage-card" style={{ borderColor: p.color + '44' }}>
                  <div className="usage-card-h" style={{ color: p.color }}>
                    <span>{p.label}</span>
                    <span style={{ fontSize: 10, color: 'var(--dim)', fontWeight: 400 }}>{p.desc}</span>
                  </div>
                  <div className="usage-card-meta">
                    池大小 <b>{u.pool_size}</b> · 调用 <b>{fmtNum(agg.calls_total)}</b> · 今日 <b>{fmtNum(agg.calls_today)}</b>
                  </div>
                  {items.length === 0 ? (
                    <div className="usage-empty">尚未配置 Key · 添加后将自动轮询</div>
                  ) : (
                    <div className="usage-items">
                      {items.map((it) => (
                        <div key={it.key_id} className="usage-item">
                          <div className="usage-item-top">
                            <span className="usage-key-name" title={it.masked_key}>{it.key_name}</span>
                            <span className="usage-key-mask">{it.masked_key}</span>
                          </div>
                          <div className="usage-bar-wrap">
                            <div
                              className="usage-bar"
                              style={{
                                width: Math.min(100, (it.calls_total / Math.max(1, agg.calls_total)) * 100) + '%',
                                background: `linear-gradient(90deg, ${p.color}, ${p.color}88)`,
                              }}
                            />
                          </div>
                          <div className="usage-stats">
                            <span>调用 <b>{fmtNum(it.calls_total)}</b></span>
                            <span>今日 <b>{fmtNum(it.calls_today)}</b></span>
                            <span>Tokens <b>{fmtNum(it.total_tokens)}</b></span>
                            <span style={{ color: p.color, fontWeight: 800 }}>{fmtCost(it.total_cost_usd)}</span>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
          <div style={{ fontSize: 10, color: 'var(--dim)', marginTop: 8 }}>
            定价参考：DeepSeek V4 input $0.14/M · output $0.28/M ｜ 腾讯混元 Hy3 input ¥0.004/1K · output ¥0.008/1K
          </div>
        </div>
      )}

      {/* ── AI Key 列表 ── */}
      <div className="panel" style={{ padding: 16 }}>
        <div className="h" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span>
            🔑 AI Key 管理 · 双模型密钥
            <span style={{ marginLeft: 10, fontSize: 10, color: 'var(--dim)', fontWeight: 400 }}>
              {keys.filter(k => !k.is_env_fallback).length} 条 DB 密钥（可管理） · {envFallbacks.length} 条 .env 兜底（只读）
            </span>
          </span>
          <button className="btn" onClick={() => setShowAdd((s) => !s)}>
            {showAdd ? '收起' : '+ 添加密钥'}
          </button>
        </div>

        {err && <div className="err-box">{err}</div>}

        {showAdd && (
          <form className="key-form" onSubmit={submit}>
            <div className="row">
              <label>提供商</label>
              <select value={form.provider} onChange={(e) => setForm({ ...form, provider: e.target.value })}>
                {PROVIDERS.map((p) => <option key={p.v} value={p.v}>{p.label}</option>)}
              </select>
            </div>
            <div className="row">
              <label>密钥名称</label>
              <input value={form.key_name} placeholder="如：生产-DeepSeek" onChange={(e) => setForm({ ...form, key_name: e.target.value })} />
            </div>
            <div className="row">
              <label>API Key</label>
              <input value={form.api_key} placeholder="sk-..." onChange={(e) => setForm({ ...form, api_key: e.target.value })} />
            </div>
            <div className="row">
              <label>Secret（可选）</label>
              <input value={form.secret_key} placeholder="部分平台需要" onChange={(e) => setForm({ ...form, secret_key: e.target.value })} />
            </div>
            <div className="row" style={{ justifyContent: 'flex-end' }}>
              <button className="btn" type="submit" disabled={busy}>{busy ? '提交中…' : '保存密钥'}</button>
            </div>
          </form>
        )}

        {loading ? (
          <div className="dim" style={{ padding: 20 }}>加载中…</div>
        ) : (
          <>
            {/* ━━━ 段 1：DB 密钥（可管理） ━━━ */}
            {(() => {
              const dbKeys = keys.filter(k => !k.is_env_fallback)
              return (
                <div style={{ marginBottom: 14 }}>
                  <div style={{
                    fontSize: 11, fontWeight: 700, color: 'var(--blue)',
                    padding: '6px 0', borderBottom: '1px dashed var(--border)',
                    marginBottom: 8, display: 'flex', alignItems: 'center', gap: 8,
                  }}>
                    📋 我的密钥（DB 存储 · 可启用/禁用/删除）
                    <span style={{ fontSize: 10, color: 'var(--dim)', fontWeight: 400 }}>
                      添加后自动加入轮询池，与 .env 兜底互为热备
                    </span>
                  </div>
                  {dbKeys.length === 0 ? (
                    <div className="dim" style={{ padding: 14, fontSize: 12 }}>
                      暂无 DB 密钥。点击右上「+ 添加密钥」创建你的第一个 API Key，启用后将与下方 .env 兜底共同构成 KeyPool。
                    </div>
                  ) : (
                    <div className="keys-grid">
                      {dbKeys.map((k) => {
                        const b = statusBadge(k)
                        const p = provOf(k.provider)
                        return (
                          <div key={k.id} className="key-row">
                            <span className="provider-badge" style={{ background: p.color + '22', color: p.color, borderColor: p.color + '55' }}>
                              {p.label}
                            </span>
                            <div className="key-mid">
                              <div className="key-name">{k.key_name}</div>
                              <div className="key-meta">
                                <span style={{ color: b.c }}>● {b.t}</span>
                                <span>总 Token {k.total_tokens ?? 0}</span>
                                <span>本月 {k.monthly_tokens ?? 0}</span>
                                <span>本月成本 ${k.monthly_cost ?? 0}</span>
                              </div>
                            </div>
                            <div className="key-ops">
                              <button className="btn-sm" onClick={() => onToggle(k)} disabled={busy}>
                                {busy ? '验证中…' : (k.is_active ? '禁用' : '启用')}
                              </button>
                              <button className="btn-sm danger" onClick={() => onDelete(k)}>删除</button>
                            </div>
                          </div>
                        )
                      })}
                    </div>
                  )}
                </div>
              )
            })()}

            {/* ━━━ 段 2：.env 兜底（只读状态展示） ━━━ */}
            {envFallbacks.length > 0 && (
              <div>
                <div style={{
                  fontSize: 11, fontWeight: 700, color: '#ffd56b',
                  padding: '6px 0', borderBottom: '1px dashed var(--border)',
                  marginBottom: 8, display: 'flex', alignItems: 'center', gap: 8,
                }}>
                  🛡️ 系统兜底（.env 启动时加载 · 运维级只读）
                  <span style={{ fontSize: 10, color: 'var(--dim)', fontWeight: 400 }}>
                    部署期在 .env 配置 · 防止误删 · 始终参与调用
                  </span>
                </div>
                <div className="keys-grid">
                  {envFallbacks.map((k) => {
                    const b = statusBadge(k)
                    const p = provOf(k.provider)
                    return (
                      <div key={k.id} className="key-row" style={{
                        borderColor: '#ffd56b55',
                        background: 'rgba(255, 213, 107, 0.05)',
                      }}>
                        <span className="provider-badge" style={{ background: p.color + '22', color: p.color, borderColor: p.color + '55' }}>
                          {p.label}
                        </span>
                        <div className="key-mid">
                          <div className="key-name">
                            {k.key_name}
                            <span style={{ marginLeft: 6, fontSize: 9, padding: '1px 5px', background: '#ffd56b22', color: '#ffd56b', borderRadius: 3, fontWeight: 700 }}>系统内置</span>
                          </div>
                          <div className="key-meta">
                            <span style={{ color: b.c }}>● {b.t}</span>
                            {k.masked_key && <span style={{ color: 'var(--dim)' }}>{k.masked_key}</span>}
                            <span>总 Token {k.total_tokens ?? 0}</span>
                            <span>本月 {k.monthly_tokens ?? 0}</span>
                            <span>本月成本 ${k.monthly_cost ?? 0}</span>
                          </div>
                        </div>
                        <div className="key-ops">
                          <span style={{ fontSize: 10, color: 'var(--dim)', padding: '0 6px' }}>不可手动管理</span>
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}