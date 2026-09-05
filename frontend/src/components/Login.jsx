import { useState } from 'react'
import { login, register } from '../services/api'
import { BRAND, footerText } from '../brand/identity'
import { LogoStacked } from './brand/Logo.jsx'

// 商业版登录门禁：支持「登录 / 注册」切换。
// 注意：已移除 dev 预填账号密码，正式发布不应硬编码任何凭据。
export default function Login({ onOk }) {
  const [mode, setMode] = useState('login') // login | register
  const [email, setEmail] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (e) => {
    e.preventDefault()
    setErr('')
    setBusy(true)
    try {
      const user = mode === 'login'
        ? await login(email.trim(), password)
        : await register(email.trim(), username.trim(), password)
      onOk && onOk(user)
    } catch (e) {
      setErr(e.message || '操作失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-bg">
      <form className="login-card" onSubmit={submit}>
        <LogoStacked size={72} />
        <div className="login-sub">{BRAND.category}</div>

        {mode === 'register' && (
          <>
            <label>用户名</label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="登录用户名"
              autoComplete="username"
              required
            />
          </>
        )}
        <label>邮箱</label>
        <input
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="邮箱"
          autoComplete="username"
          required
        />
        <label>密码</label>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="密码（至少 6 位）"
          autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
          required
        />

        {err && <div className="login-err">{err}</div>}
        <button type="submit" disabled={busy}>
          {busy ? '处理中…' : mode === 'login' ? '进入交易大脑' : '注册并进入'}
        </button>

        <div className="login-switch">
          {mode === 'login' ? (
            <span>还没有账号？<a onClick={() => { setMode('register'); setErr('') }}>立即注册</a></span>
          ) : (
            <span>已有账号？<a onClick={() => { setMode('login'); setErr('') }}>前往登录</a></span>
          )}
        </div>
        <div className="login-foot">{footerText()}</div>
      </form>
    </div>
  )
}
