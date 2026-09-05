/**
 * 授权激活页（V6 Phase 8.4）
 *
 * 设计要点（都是被真实客服场景倒逼出来的）：
 *  1. 机器码一键复制 —— 客户要把它发给我们才能签发授权。让他手抄 32 位十六进制
 *     必错，错一位我们就签出一个永远激活不了的令牌，来回三轮客服。
 *  2. 令牌用 textarea 不用 input —— 令牌很长且客户常从微信/邮件复制，
 *     单行框看不到全貌，粘错了自己都不知道。
 *  3. 激活失败必须显示**错误码**（TOKEN_BAD_SIGNATURE 之类）——
 *     客户截图发过来，我们一眼定位是"粘漏了"还是"签给别人的"。
 *     只显示"激活失败"等于让客服重新问一遍。
 *  4. 全程离线可用，不需要联网。客户机常在隔离网络里。
 */
import { useEffect, useState } from 'react'
import {
  fetchMachineCode,
  activateLicense,
  licenseHeartbeat,
} from '../services/license.js'
import { useLicense, refreshLicense } from '../hooks/useLicense.js'
import { badgeTone } from '../services/license.js'
import { BRAND, RISK_DISCLAIMER } from '../brand/identity.js'

/** 硬件标识项的中文名（后端返回的是 board/cpu/mac 三个技术 key） */
const FACTOR_CN = { board: '主板', cpu: '处理器', mac: '网卡' }

/**
 * 后端 /license/machine 的 factors_present 是**字典** {board:true, cpu:false, mac:true}，
 * 不是数组。这里统一收敛成"已采集项的中文名列表"。
 * （一次真实契约错配：前端原先直接 .join() 会静默不渲染，什么都不显示也不报错——
 *   这种"静默失效"最难查，所以顺手把两种形态都兼容掉。）
 */
function presentFactorNames(fp) {
  if (!fp) return []
  if (Array.isArray(fp)) return fp.map((k) => FACTOR_CN[k] || k)
  return Object.entries(fp)
    .filter(([, ok]) => !!ok)
    .map(([k]) => FACTOR_CN[k] || k)
}

function Row({ label, children, mono }) {
  return (
    <div className="lic-row">
      <span className="lic-row-l">{label}</span>
      <span className={`lic-row-v ${mono ? 'num' : ''}`}>{children}</span>
    </div>
  )
}

export default function LicenseActivate() {
  const st = useLicense()
  const [machine, setMachine] = useState(null)
  const [token, setToken] = useState('')
  const [customer, setCustomer] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null) // {ok, message, code}
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    let alive = true
    fetchMachineCode()
      .then((r) => { if (alive && r?.success) setMachine(r.data) })
      .catch(() => {})
    return () => { alive = false }
  }, [])

  const doCopy = async () => {
    const code = machine?.machine_code
    if (!code) return
    try {
      await navigator.clipboard.writeText(code)
    } catch (e) {
      // 非 https / 老浏览器没有 clipboard API → 退回选中文本让客户手动 Ctrl+C，
      // 而不是静默失败让他以为已经复制了
      const el = document.getElementById('lic-machine-code')
      if (el) {
        const range = document.createRange()
        range.selectNodeContents(el)
        const sel = window.getSelection()
        sel.removeAllRanges()
        sel.addRange(range)
      }
    }
    setCopied(true)
    setTimeout(() => setCopied(false), 1800)
  }

  const doActivate = async () => {
    if (!token.trim()) {
      setResult({ ok: false, message: '请先粘贴授权令牌' })
      return
    }
    setBusy(true)
    setResult(null)
    try {
      const r = await activateLicense(token, customer)
      setResult({ ok: !!r.success, message: r.message || '', code: r.code || '' })
      if (r.success) {
        setToken('')
        await refreshLicense() // 三处 UI 立即同步，不用等 60s 轮询
      }
    } catch (e) {
      setResult({ ok: false, message: e.message || '激活请求失败' })
    } finally {
      setBusy(false)
    }
  }

  const doHeartbeat = async () => {
    setBusy(true)
    try {
      const r = await licenseHeartbeat()
      const d = r?.data || {}
      setResult({
        ok: true,
        message:
          d.mode === 'offline'
            ? '当前为离线授权模式，无需联网校验（不影响交易）'
            : d.message || '心跳完成',
      })
      await refreshLicense()
    } catch (e) {
      // ★ 心跳失败绝不能渲染成红色告警——它跟交易毫无关系。
      setResult({ ok: true, message: '心跳未能完成（不影响交易，可稍后再试）' })
    } finally {
      setBusy(false)
    }
  }

  const tone = st ? badgeTone(st) : 'ok'

  return (
    <div className="lic-page">
      {/* ───── 当前状态 ───── */}
      <div className="panel lic-card">
        <div className="h">授权状态</div>
        {!st ? (
          <div className="lic-empty">正在读取授权状态…</div>
        ) : (
          <>
            <div className={`lic-state-hero lic-hero-${tone}`}>
              <div className="lic-state-name">{st.state_label}</div>
              <div className="lic-state-msg">
                {st.message || '授权正常，系统按策略开仓'}
              </div>
              {!st.allow_open && (
                <div className="lic-state-safe">
                  已停止开新仓；<b>已有持仓不受影响</b>，止损、止盈与智能平仓照常运行
                </div>
              )}
            </div>
            <div className="lic-rows">
              <Row label="授权码" mono>{st.license_key || '—'}</Row>
              <Row label="版本">{st.edition_label || st.edition || '—'}</Row>
              <Row label="客户">{st.customer || '—'}</Row>
              <Row label="账号配额" mono>
                {st.max_accounts > 0
                  ? `${st.used_accounts ?? 0} / ${st.max_accounts}`
                  : '—'}
              </Row>
              <Row label="有效期至" mono>
                {st.valid_until ? st.valid_until.slice(0, 10) : '永久'}
              </Row>
              {st.grace_until && (
                <Row label="宽限截止" mono>{st.grace_until.slice(0, 19).replace('T', ' ')}</Row>
              )}
              <Row label="最近校验" mono>
                {st.evaluated_at ? st.evaluated_at.slice(11, 19) : '—'}
              </Row>
            </div>
          </>
        )}
      </div>

      {/* ───── 机器码 ───── */}
      <div className="panel lic-card">
        <div className="h">本机机器码</div>
        <div className="lic-tip">
          申请或迁移授权时，请把下面这串机器码提供给服务商。
          它由主板、CPU、网卡三项硬件标识哈希而成，<b>不含任何可还原的硬件信息</b>，
          可以放心截图发送。
        </div>
        <div className="lic-machine">
          <code id="lic-machine-code" className="lic-machine-code">
            {machine?.machine_code || '读取中…'}
          </code>
          <button className="lic-btn ghost" onClick={doCopy} disabled={!machine}>
            {copied ? '已复制' : '复制'}
          </button>
        </div>
        {machine && (
          <div className="lic-machine-meta">
            硬件标识采集 {machine.factors_count}/3
            {(() => {
              const names = presentFactorNames(machine.factors_present)
              return names.length ? `（${names.join(' · ')}）` : ''
            })()}
            {' · '}三项中匹配两项即视为同一台机器，换网卡不影响授权
            {machine.factors_count < 2 && (
              <span className="lic-warn-inline">
                　⚠ 采集到的标识不足 2 项，虚拟机/容器环境可能无法稳定绑定，请联系服务商
              </span>
            )}
          </div>
        )}
      </div>

      {/* ───── 激活 ───── */}
      <div className="panel lic-card">
        <div className="h">激活 / 续期</div>
        <div className="lic-tip">
          把服务商签发的授权令牌完整粘贴到下方（以 <code>WXAI1.</code> 开头的一长串），
          点击激活即可。<b>整个过程无需联网。</b>
        </div>

        <textarea
          className="lic-token"
          rows={5}
          spellCheck={false}
          placeholder="WXAI1.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.xxxxxxxxxxxxxxxx"
          value={token}
          onChange={(e) => setToken(e.target.value)}
        />

        <div className="lic-form-line">
          <input
            className="lic-input"
            placeholder="客户名称（可选，便于对账）"
            value={customer}
            onChange={(e) => setCustomer(e.target.value)}
          />
          <button className="lic-btn primary" onClick={doActivate} disabled={busy}>
            {busy ? '处理中…' : '激活'}
          </button>
          <button className="lic-btn ghost" onClick={doHeartbeat} disabled={busy}>
            检查续期
          </button>
        </div>

        {result && (
          <div className={`lic-result ${result.ok ? 'ok' : 'bad'}`}>
            <span className="lic-result-msg">{result.message}</span>
            {result.code ? (
              <code className="lic-result-code" title="报障时请连同此代码一起提供">
                {result.code}
              </code>
            ) : null}
          </div>
        )}
      </div>

      {/* ───── 合规 ───── */}
      <div className="panel lic-card lic-legal">
        <div className="h">风险提示</div>
        <p className="lic-disclaimer">{RISK_DISCLAIMER}</p>
        <p className="lic-legal-foot">
          {BRAND.fullName} · 授权为商业许可，不构成任何收益承诺。
        </p>
      </div>
    </div>
  )
}
