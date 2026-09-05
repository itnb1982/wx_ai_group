// 实时状态条：AI 自动交易循环运行状态 + Key 来源
// 独立成组件，方便 Dashboard 在作战图顶部直接调用，避免在 AI 工作剧场里占大块空间
import React from 'react'

const srcLabel = (s) => {
  if (s === 'db') return { t: 'DB 池', c: 'var(--blue)' }
  if (s === 'env_fallback_only') return { t: '.env 回退', c: '#ffd56b' }
  if (s === 'db+env') return { t: 'DB + .env', c: 'var(--blue)' }
  if (s === 'missing') return { t: '未配置', c: 'var(--red)' }
  // undefined/null：请求尚未返回或丢失，显示「读取中」而非红色「未配置」，避免切换路由时误报掉线
  return { t: '读取中', c: 'var(--dim)' }
}

// 横条样式：贴作战图标题栏下方，自动循环状态 + 已跑轮数 + 上轮时间 + Key 来源
// 设计目标：高度 ~28px，一行显示，不占用正文空间
export default function LiveStatusBar({ liveStatus, compact = false }) {
  const al = liveStatus?.auto_loop || {}
  const ks = liveStatus?.key_sources || {}
  const loading = liveStatus?.loading
  const running = !!al.running
  const cloudEnabled = liveStatus?.cloud_enabled !== false
  // 主开关状态：用于区分"客户主动关"与"Key 全禁用自动降级"两种纯本地情形
  const cloudMaster = liveStatus?.cloud_master_enabled !== false
  const cloudLocalLabel = (m) =>
    m ? '本地融合决策（云端Key停用·自动降级）' : '本地融合决策（已关闭云端双脑）'
  const dsSrc = ks.deepseek // 故意不默认 'missing'，让 undefined 走「读取中」
  const hySrc = ks.hunyuan
  const dsL = srcLabel(dsSrc)
  const hyL = srcLabel(hySrc)
  const lastCycle = al.last_cycle ? al.last_cycle.slice(11, 19) : '--:--:--'

  if (compact) {
    // 紧凑横条：单行，最小高度
    return (
      <div
        style={{
          display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'nowrap',
          padding: '5px 10px', margin: '6px 0 8px 0',
          background: loading ? 'rgba(255,255,255,0.03)' : (running ? 'rgba(76,175,80,0.06)' : 'rgba(244,67,54,0.08)'),
          border: '1px solid ' + (loading ? 'rgba(255,255,255,0.08)' : (running ? 'rgba(76,175,80,0.3)' : 'rgba(244,67,54,0.35)')),
          borderRadius: 4, fontSize: 11, lineHeight: 1.2,
          overflow: 'hidden', whiteSpace: 'nowrap',
        }}
      >
        <span
          style={{
            color: running ? '#4caf50' : '#f44336', fontWeight: 900, flex: '0 0 auto',
            animation: running ? 'live-pulse 2s infinite' : 'none',
          }}
          title={loading ? '正在读取 AI 实时状态…' : (running ? 'AI 自动交易循环运行中' : 'AI 自动交易循环已停止')}
        >
          ● {loading ? '读取中…' : (running ? 'AI 运行中' : 'AI 已停止')}
        </span>
        <span style={{ color: 'var(--dim)', flex: '0 0 auto' }}>
          已跑 <b style={{ color: 'var(--txt)' }}>{al.cycles || 0}</b> 轮
        </span>
        <span style={{ color: 'var(--dim)', flex: '0 0 auto' }}>
          上轮 <b style={{ color: 'var(--txt)' }}>{lastCycle}</b>
        </span>
        <span style={{ color: 'var(--dim)', flex: '0 0 auto' }}>|</span>
        {cloudEnabled ? (
          <>
            <span style={{ color: dsL.c, fontWeight: 700, flex: '0 0 auto' }}>
              DeepSeek · {dsL.t}
            </span>
            <span style={{ color: 'var(--dim)', flex: '0 0 auto' }}>|</span>
            <span style={{ color: hyL.c, fontWeight: 700, flex: '0 0 auto' }}>
              混元 · {hyL.t}
            </span>
          </>
        ) : (
          <span style={{ color: '#2ee6a0', fontWeight: 700, flex: '0 0 auto' }}>
            {cloudLocalLabel(cloudMaster)}
          </span>
        )}
        {(!running && !loading) && (
          <span style={{ marginLeft: 'auto', color: '#f44336', fontSize: 10, flex: '0 0 auto' }}>
            调 <code style={{ fontSize: 10 }}>POST /api/trade/auto/start</code> 启动
          </span>
        )}
      </div>
    )
  }

  // 默认模式（保留旧长条样式，给其它场景用）
  return (
    <div
      style={{
        display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap',
        padding: '8px 12px', margin: '0 0 10px 0',
        background: loading ? 'rgba(255,255,255,0.03)' : (running ? 'rgba(76,175,80,0.06)' : 'rgba(244,67,54,0.08)'),
        border: '1px solid ' + (loading ? 'rgba(255,255,255,0.08)' : (running ? 'rgba(76,175,80,0.3)' : 'rgba(244,67,54,0.35)')),
        borderRadius: 6, fontSize: 12,
      }}
    >
      <span style={{
        color: loading ? 'var(--dim)' : (running ? '#4caf50' : '#f44336'), fontWeight: 900,
        animation: (running && !loading) ? 'live-pulse 2s infinite' : 'none',
      }}>
        ● {loading ? '读取 AI 状态中…' : (running ? 'AI 自动交易循环 运行中' : 'AI 自动交易循环 已停止')}
      </span>
      <span style={{ color: 'var(--dim)' }}>
        已跑 <b style={{ color: 'var(--txt)' }}>{al.cycles || 0}</b> 轮 ·
        上轮 <b style={{ color: 'var(--txt)' }}>{lastCycle}</b>
      </span>
      <span style={{ marginLeft: 'auto', color: 'var(--dim)' }}>Key 来源：</span>
      {cloudEnabled ? (
        <>
          <span style={{ color: dsL.c, fontWeight: 700 }}>DeepSeek · {dsL.t}</span>
          <span style={{ color: 'var(--dim)' }}>|</span>
          <span style={{ color: hyL.c, fontWeight: 700 }}>混元 · {hyL.t}</span>
        </>
      ) : (
        <span style={{ color: '#2ee6a0', fontWeight: 700 }}>{cloudLocalLabel(cloudMaster)}</span>
      )}
      {(!running && !loading) && (
        <span style={{ marginLeft: 8, color: '#f44336', fontSize: 11 }}>
          （后端启动时若未自动开启，请调用 <code>POST /api/trade/auto/start</code>）
        </span>
      )}
    </div>
  )
}
