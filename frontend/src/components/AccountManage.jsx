import { useEffect, useState, useCallback } from 'react'
import {
  listAccounts, connectAccount, syncAccount, toggleTrading,
  getPositions, deleteAccount, addAccount, discoverMt5, setPrimary,
  updateAccount,
} from '../services/api'

const fmt = (n) => (n == null ? '—' : Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }))
const STATUS = {
  online: { t: '在线', c: 'b-on' },
  offline: { t: '离线', c: 'b-off' },
  error: { t: '错误', c: 'b-off' },
  connecting: { t: '连接中', c: 'b-tr' },
}

export default function AccountManage() {
  const [accounts, setAccounts] = useState([])
  const [busy, setBusy] = useState({})
  const [err, setErr] = useState('')
  const [positions, setPositions] = useState(null) // {id, rows}
  const [showAdd, setShowAdd] = useState(false)
  const [showEdit, setShowEdit] = useState(false)
  const [editingAccount, setEditingAccount] = useState(null) // 正在编辑的账号对象
  const [form, setForm] = useState({ name: '', account_id: '', password: '', server: 'STARTRADERFinancial-Demo', terminal_path: '', account_type: 'demo' })

  // 星迈（STARTRADER Financial）服务器预设（input+datalist 模式：框里既能选也能自由键入）
  const STARTRADER_SERVERS = [
    'STARTRADERFinancial-Demo',
    'STARTRADERFinancial-Live',
    'STARTRADERFinancial-Live 3',
    'STARTRADERFinancial-Live 4',
    'STARTRADERFinancial-Live 5',
    'STARTRADERFinancial-Live 02',
  ]
  const [discovered, setDiscovered] = useState([])       // 扫描到的 MT5 终端
  const [scanning, setScanning] = useState(false)
  const [scanErr, setScanErr] = useState('')
  const [serverOpen, setServerOpen] = useState(false)   // 服务器下拉面板开关

  const load = useCallback(async () => {
    try {
      const list = await listAccounts()
      setAccounts(Array.isArray(list) ? list : [])
      setErr('')
    } catch (e) {
      setErr(e.message || '加载失败')
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    let timer = null
    const tick = async () => {
      await load()
      if (!cancelled) timer = setTimeout(tick, 10000)
    }
    tick()
    return () => { cancelled = true; if (timer) clearTimeout(timer) }
  }, [load])

  const act = async (id, fn, label) => {
    setBusy((b) => ({ ...b, [id + label]: true }))
    setErr('')
    try { await fn(); await load() }
    catch (e) { setErr((e.message || '操作失败') + '') }
    finally { setBusy((b) => ({ ...b, [id + label]: false })) }
  }

  const viewPositions = async (id) => {
    setBusy((b) => ({ ...b, [id + 'pos']: true }))
    try {
      const rows = await getPositions(id)
      setPositions({ id, rows: Array.isArray(rows) ? rows : [] })
    } catch (e) { setErr(e.message || '持仓加载失败') }
    finally { setBusy((b) => ({ ...b, [id + 'pos']: false })) }
  }

  // 自动扫描硬盘上的 MT5 安装，填充路径下拉
  const scanMt5 = async () => {
    setScanning(true)
    setScanErr('')
    try {
      const data = await discoverMt5()
      const list = (data && data.terminals) || []
      setDiscovered(list)
      if (!list.length) setScanErr('未扫描到 MT5 终端，可手动填写路径')
    } catch (e) {
      setScanErr(e.message || '扫描失败')
    } finally {
      setScanning(false)
    }
  }

  // 关闭弹窗时清空扫描结果，避免下次打开残留
  const closeAdd = () => {
    setShowAdd(false)
    setDiscovered([])
    setScanErr('')
    setServerOpen(false)
  }

  // ── 编辑账号 ──
  const openEdit = (account) => {
    setEditingAccount(account)
    setForm({
      name: account.name || '',
      account_id: String(account.account_id || ''),
      password: '', // 安全：不回显密码，留空表示不修改
      server: account.server || 'STARTRADERFinancial-Demo',
      terminal_path: account.terminal_path || '',
      account_type: account.account_type || 'demo',
    })
    setDiscovered([])
    setScanErr('')
    setServerOpen(false)
    setShowEdit(true)
  }

  const closeEdit = () => {
    setShowEdit(false)
    setEditingAccount(null)
    setDiscovered([])
    setScanErr('')
    setServerOpen(false)
  }

  const submitEdit = async (e) => {
    e.preventDefault()
    if (!form.server.trim()) { setErr('请填写服务器名'); return }
    if (!editingAccount) return
    setBusy((b) => ({ ...b, edit: true }))
    setErr('')
    try {
      const payload = { ...form, account_id: String(form.account_id), server: form.server.trim() }
      // 密码为空时不传（保持原密码不变）
      if (!payload.password) delete payload.password
      await updateAccount(editingAccount.id, payload)
      closeEdit()
      await load()
    } catch (e) { setErr(e.message || '修改失败') }
    finally { setBusy((b) => ({ ...b, edit: false })) }
  }

  const submitAdd = async (e) => {
    e.preventDefault()
    if (!form.server.trim()) { setErr('请填写服务器名'); return }
    setBusy((b) => ({ ...b, add: true }))
    setErr('')
    try {
      await addAccount({ ...form, account_id: String(form.account_id), server: form.server.trim() })
      closeAdd()
      setForm({ name: '', account_id: '', password: '', server: STARTRADER_SERVERS[0], terminal_path: '', account_type: 'demo' })
      await load()
    } catch (e) { setErr(e.message || '添加失败') }
    finally { setBusy((b) => ({ ...b, add: false })) }
  }

  const del = async (id) => {
    if (!confirm('确认移除该账户？')) return
    setBusy((b) => ({ ...b, [id + 'del']: true }))
    try { await deleteAccount(id); await load() }
    catch (e) { setErr(e.message || '删除失败') }
    finally { setBusy((b) => ({ ...b, [id + 'del']: false })) }
  }

  return (
    <div className="wrap">
      <div className="panel">
        <div className="h">
          账户管理 · MT5 多账户
          <button className="mini-btn" style={{ marginLeft: 'auto' }} onClick={() => setShowAdd((v) => !v)}>
            {showAdd ? '收起' : '+ 添加账户'}
          </button>
        </div>

        {err && <div className="login-err" style={{ marginBottom: 10 }}>{err}</div>}

        {showAdd && (
          <div className="add-modal-backdrop" onClick={closeAdd}>
            <form className="add-modal" onSubmit={submitAdd} onClick={(e) => e.stopPropagation()}>
              <div className="add-modal-glow" />
              <div className="add-modal-h">
                <div className="add-modal-title">添加 MT5 账号</div>
                <button type="button" className="add-modal-x" onClick={closeAdd} aria-label="关闭">×</button>
              </div>

              <div className="add-modal-body">
                <div className="m-row">
                  <label>账号名称</label>
                  <input className="m-input" placeholder="例如：实盘主账户" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
                </div>
                <div className="m-row">
                  <label>MT5 账号 ID</label>
                  <input className="m-input" placeholder="登录数字" value={form.account_id} onChange={(e) => setForm({ ...form, account_id: e.target.value })} required />
                </div>
                <div className="m-row">
                  <label>MT5 密码</label>
                  <input className="m-input" type="password" placeholder="交易密码（AES 加密存储）" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} required />
                </div>
                <div className="m-row">
                  <label>服务器</label>
                  <div className="m-combo">
                    <input
                      className="m-input m-combo-input"
                      placeholder="点击选择星迈预设，或直接键入任意服务器名"
                      value={form.server}
                      onChange={(e) => setForm({ ...form, server: e.target.value })}
                      onFocus={() => setServerOpen(true)}
                      onBlur={() => setTimeout(() => setServerOpen(false), 150)}
                      onKeyDown={(e) => {
                        if (e.key === 'Escape') setServerOpen(false)
                        if (e.key === 'ArrowDown' && !serverOpen) setServerOpen(true)
                      }}
                      required
                      autoComplete="off"
                    />
                    <button
                      type="button"
                      className={`m-combo-arrow ${serverOpen ? 'on' : ''}`}
                      onClick={() => setServerOpen((v) => !v)}
                      tabIndex={-1}
                      aria-label="展开服务器列表"
                    >{serverOpen ? '▴' : '▾'}</button>
                    {serverOpen && (
                      <div className="m-combo-panel" onMouseDown={(e) => e.preventDefault()}>
                        <div className="m-combo-hint">⚡ 星迈预设 · 点选填入，或继续键入自定义服务器</div>
                        {STARTRADER_SERVERS.map((s) => (
                          <div
                            key={s}
                            className={`m-combo-opt ${form.server === s ? 'on' : ''}`}
                            onMouseDown={(e) => { e.preventDefault(); setForm({ ...form, server: s }); setServerOpen(false) }}
                          >
                            <span className="m-combo-dot" />
                            {s}
                            {form.server === s && <span className="m-combo-chk">✓</span>}
                          </div>
                        ))}
                        {form.server && !STARTRADER_SERVERS.includes(form.server) && (
                          <div className="m-combo-opt custom">
                            <span className="m-combo-dot gold" />
                            自定义：<code>{form.server}</code>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </div>
                <div className="m-row">
                  <label>MT5 终端路径</label>
                  <div className="m-row-inline">
                    <input
                      className="m-input"
                      placeholder={discovered.length ? '— 请从下方选择终端或手动输入 —' : '点击右侧「重新扫描」自动检测全部硬盘'}
                      value={form.terminal_path}
                      onChange={(e) => setForm({ ...form, terminal_path: e.target.value })}
                    />
                    <button type="button" className="scan-btn" onClick={scanMt5} disabled={scanning}>
                      {scanning ? '扫描中…' : '⟳ 重新扫描'}
                    </button>
                  </div>
                  {discovered.length > 0 ? (
                    <select
                      className="m-input"
                      style={{ marginTop: 6 }}
                      value={form.terminal_path}
                      onChange={(e) => setForm({ ...form, terminal_path: e.target.value })}
                    >
                      <option value="">— 请选择终端 —</option>
                      {discovered.map((t) => (
                        <option key={t.path} value={t.path}>{t.name}（{t.path}）</option>
                      ))}
                    </select>
                  ) : scanErr ? (
                    <div className="scan-hint">⚠ {scanErr}</div>
                  ) : null}
                </div>
                <div className="m-row">
                  <label>账户类型</label>
                  <div className="type-tabs">
                    <button
                      type="button"
                      className={`type-tab ${form.account_type === 'demo' ? 'on' : ''}`}
                      onClick={() => setForm({ ...form, account_type: 'demo' })}
                    >🧪 模拟盘</button>
                    <button
                      type="button"
                      className={`type-tab real ${form.account_type === 'real' ? 'on' : ''}`}
                      onClick={() => setForm({ ...form, account_type: 'real' })}
                    >💰 真实盘</button>
                  </div>
                </div>
              </div>

              <div className="add-modal-foot">
                <button type="button" className="m-btn" onClick={closeAdd}>取消</button>
                <button type="submit" className="m-btn primary" disabled={busy.add}>
                  {busy.add ? '添加中…' : '+ 添加账号'}
                </button>
              </div>
            </form>
          </div>
        )}

        {showEdit && editingAccount && (
          <div className="add-modal-backdrop" onClick={closeEdit}>
            <form className="add-modal" onSubmit={submitEdit} onClick={(e) => e.stopPropagation()}>
              <div className="add-modal-glow" />
              <div className="add-modal-h">
                <div className="add-modal-title">编辑账号 · {editingAccount.name}</div>
                <button type="button" className="add-modal-x" onClick={closeEdit} aria-label="关闭">×</button>
              </div>

              <div className="add-modal-body">
                <div className="m-row">
                  <label>账号名称</label>
                  <input className="m-input" placeholder="例如：实盘主账户" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
                </div>
                <div className="m-row">
                  <label>MT5 账号 ID</label>
                  <input className="m-input" placeholder="登录数字" value={form.account_id} onChange={(e) => setForm({ ...form, account_id: e.target.value })} required />
                </div>
                <div className="m-row">
                  <label>MT5 密码</label>
                  <input className="m-input" type="password" placeholder="留空保持原密码不变（AES 加密存储）" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
                </div>
                <div className="m-row">
                  <label>服务器</label>
                  <div className="m-combo">
                    <input
                      className="m-input m-combo-input"
                      placeholder="点击选择星迈预设，或直接键入任意服务器名"
                      value={form.server}
                      onChange={(e) => setForm({ ...form, server: e.target.value })}
                      onFocus={() => setServerOpen(true)}
                      onBlur={() => setTimeout(() => setServerOpen(false), 150)}
                      onKeyDown={(e) => {
                        if (e.key === 'Escape') setServerOpen(false)
                        if (e.key === 'ArrowDown' && !serverOpen) setServerOpen(true)
                      }}
                      required
                      autoComplete="off"
                    />
                    <button
                      type="button"
                      className={`m-combo-arrow ${serverOpen ? 'on' : ''}`}
                      onClick={() => setServerOpen((v) => !v)}
                      tabIndex={-1}
                      aria-label="展开服务器列表"
                    >{serverOpen ? '▴' : '▾'}</button>
                    {serverOpen && (
                      <div className="m-combo-panel" onMouseDown={(e) => e.preventDefault()}>
                        <div className="m-combo-hint">⚡ 星迈预设 · 点选填入，或继续键入自定义服务器</div>
                        {STARTRADER_SERVERS.map((s) => (
                          <div
                            key={s}
                            className={`m-combo-opt ${form.server === s ? 'on' : ''}`}
                            onMouseDown={(e) => { e.preventDefault(); setForm({ ...form, server: s }); setServerOpen(false) }}
                          >
                            <span className="m-combo-dot" />
                            {s}
                            {form.server === s && <span className="m-combo-chk">✓</span>}
                          </div>
                        ))}
                        {form.server && !STARTRADER_SERVERS.includes(form.server) && (
                          <div className="m-combo-opt custom">
                            <span className="m-combo-dot gold" />
                            自定义：<code>{form.server}</code>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </div>
                <div className="m-row">
                  <label>MT5 终端路径</label>
                  <div className="m-row-inline">
                    <input
                      className="m-input"
                      placeholder={discovered.length ? '— 请从下方选择终端或手动输入 —' : '点击右侧「重新扫描」自动检测全部硬盘'}
                      value={form.terminal_path}
                      onChange={(e) => setForm({ ...form, terminal_path: e.target.value })}
                    />
                    <button type="button" className="scan-btn" onClick={scanMt5} disabled={scanning}>
                      {scanning ? '扫描中…' : '⟳ 重新扫描'}
                    </button>
                  </div>
                  {discovered.length > 0 ? (
                    <select
                      className="m-input"
                      style={{ marginTop: 6 }}
                      value={form.terminal_path}
                      onChange={(e) => setForm({ ...form, terminal_path: e.target.value })}
                    >
                      <option value="">— 请选择终端 —</option>
                      {discovered.map((t) => (
                        <option key={t.path} value={t.path}>{t.name}（{t.path}）</option>
                      ))}
                    </select>
                  ) : scanErr ? (
                    <div className="scan-hint">⚠ {scanErr}</div>
                  ) : null}
                </div>
                <div className="m-row">
                  <label>账户类型</label>
                  <div className="type-tabs">
                    <button
                      type="button"
                      className={`type-tab ${form.account_type === 'demo' ? 'on' : ''}`}
                      onClick={() => setForm({ ...form, account_type: 'demo' })}
                    >🧪 模拟盘</button>
                    <button
                      type="button"
                      className={`type-tab real ${form.account_type === 'real' ? 'on' : ''}`}
                      onClick={() => setForm({ ...form, account_type: 'real' })}
                    >💰 真实盘</button>
                  </div>
                </div>
              </div>

              <div className="add-modal-foot">
                <button type="button" className="m-btn" onClick={closeEdit}>取消</button>
                <button type="submit" className="m-btn primary" disabled={busy.edit}>
                  {busy.edit ? '保存中…' : '✓ 保存修改'}
                </button>
              </div>
            </form>
          </div>
        )}

        <div className="grid" style={{ marginTop: 12 }}>
          {accounts.length === 0 && <div className="empty">暂无账户，点击右上角"添加账户"。</div>}
          {accounts.map((a) => {
            const st = STATUS[a.status] || STATUS.offline
            return (
              <div className="acct" key={a.id}>
                <div className="hd">
                  <span className="nm">{a.name}</span>
                  <span className={`badge ${st.c}`}>{st.t}</span>
                  <span className={`badge ${a.account_type === 'real' ? 'b-pri' : 'b-tr'}`}>{a.account_type === 'real' ? '实盘' : '模拟'}</span>
                  {a.is_market_primary && <span className="badge b-pri">⚜ 主号</span>}
                  {a.is_trading_enabled && <span className="badge b-on">交易中</span>}
                </div>
                <div className="acct-id">账号 {a.account_id} · {a.server}</div>
                <div className="bal">$ {fmt(a.balance)}</div>
                <div className="metrics">
                  <div className="mt"><div className="v">{fmt(a.equity)}</div><div className="k">净值</div></div>
                  <div className="mt"><div className={`v ${(a.profit || 0) >= 0 ? 'pos' : 'neg'}`}>{fmt(a.profit)}</div><div className="k">利润</div></div>
                  <div className="mt"><div className="v">{a.is_connected ? '✓' : '✗'}</div><div className="k">连通</div></div>
                  <div className="mt"><div className="v">{a.is_trading_enabled ? '开' : '关'}</div><div className="k">交易</div></div>
                </div>
                <div className="acct-actions">
                  <button className="mini-btn" disabled={busy[a.id + 'connect']} onClick={() => act(a.id, () => connectAccount(a.id), 'connect')}>{busy[a.id + 'connect'] ? '…' : '连接'}</button>
                  <button className="mini-btn" disabled={busy[a.id + 'sync']} onClick={() => act(a.id, () => syncAccount(a.id), 'sync')}>{busy[a.id + 'sync'] ? '…' : '同步'}</button>
                  <button className="mini-btn" disabled={busy[a.id + 'toggle']} onClick={() => act(a.id, () => toggleTrading(a.id, !a.is_trading_enabled), 'toggle')}>{a.is_trading_enabled ? '停交易' : '启交易'}</button>
                  <button className="mini-btn" disabled={busy[a.id + 'pos']} onClick={() => viewPositions(a.id)}>{busy[a.id + 'pos'] ? '…加载' : '持仓'}</button>
                  {!a.is_market_primary && (
                    <button className="mini-btn primary" disabled={busy[a.id + 'primary']} onClick={() => act(a.id, () => setPrimary(a.id), 'primary')}>{busy[a.id + 'primary'] ? '…' : '设为主号'}</button>
                  )}
                  <button className="mini-btn danger" disabled={busy[a.id + 'del']} onClick={() => del(a.id)}>删除</button>
                  <button className="mini-btn" disabled={busy[a.id + 'edit']} onClick={() => openEdit(a)} title="修改账号名称、密码、服务器、终端路径等">{busy[a.id + 'edit'] ? '…' : '编辑'}</button>
                </div>
              </div>
            )
          })}
        </div>
      </div>

      {positions && (
        <div className="panel" style={{ marginTop: 14 }}>
          <div className="h">
            持仓明细
            <span style={{ marginLeft: 10, color: 'var(--muted)', fontWeight: 400, fontSize: 12 }}>
              {(() => {
                const a = (accounts || []).find((x) => x.id === positions.id)
                if (!a) return ''
                const loading = busy[positions.id + 'pos']
                return `${a.name} · 余额 ${fmt(a.balance)} · ${loading ? '刷新中…' : '实时'}`
              })()}
            </span>
            <button className="mini-btn" style={{ marginLeft: 'auto', marginRight: 8 }} disabled={busy[positions.id + 'pos']} onClick={() => viewPositions(positions.id)}>{busy[positions.id + 'pos'] ? '…' : '刷新'}</button>
            <button className="mini-btn" onClick={() => setPositions(null)}>关闭</button>
          </div>
          {positions.rows.length === 0
            ? (
              <div className="empty" style={{ padding: '24px 12px', textAlign: 'center' }}>
                <div style={{ fontSize: 32, opacity: 0.4, marginBottom: 6 }}>∅</div>
                <div style={{ color: 'var(--txt)', marginBottom: 4 }}>该账户当前无未平仓头寸</div>
                <div style={{ color: 'var(--muted)', fontSize: 12 }}>
                  {(() => {
                    const a = (accounts || []).find((x) => x.id === positions.id)
                    if (!a) return ''
                    const now = new Date()
                    const d = now.getDay()
                    const h = now.getHours()
                    if (d === 0 || d === 6) return '提示：周末 XAUUSD 休市，下周一开盘后 AI 决策循环会自动建仓。'
                    if (h < 6) return '提示：XAUUSD 早盘 06:00 开盘前暂无新行情，开盘后自动建仓。'
                    return '提示：账户已就绪，AI 决策循环建仓后此处会实时显示。'
                  })()}
                </div>
              </div>
            )
            : (
              <table className="tbl">
                <thead><tr><th>品种</th><th>方向</th><th>手数</th><th>开仓价</th><th>现价</th><th>浮盈</th></tr></thead>
                <tbody>
                  {positions.rows.map((p, i) => (
                    <tr key={i}>
                      <td>{p.symbol}</td>
                      <td className={p.type === 0 ? 'pos' : 'neg'}>{p.type === 0 ? '买' : '卖'}</td>
                      <td>{p.volume}</td>
                      <td>{fmt(p.price_open)}</td>
                      <td>{fmt(p.price_current)}</td>
                      <td className={(p.profit || 0) >= 0 ? 'pos' : 'neg'}>{fmt(p.profit)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
        </div>
      )}
    </div>
  )
}
