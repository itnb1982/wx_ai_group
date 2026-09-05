/**
 * 品牌 LOGO ——「神经元金环」
 *
 * V6 第十章品牌铁律（2026-08-07 定稿）：
 *   · 标志须突出 AI 金融属性，各板块风格统一。
 *   · 采纳方案 A「神经元金环」：六节点神经网络环 + 中心 Au。
 *     理由 = 可缩放性硬指标（16px favicon 退化最优雅）。
 *   · 全站只用本文件导出组件（mark / horizontal / stacked），
 *     禁止再内联 SVG 品牌标志（存量内联 SVG 一律收编到此）。
 *   · 配色令牌：金(--gold 主/稀缺) + AI蓝(--blue) / AI紫(--purple)(智能层)，
 *     底(--bg)。金额/手数/价格走 --font-num（见 global.css）。
 *
 * 颜色统一从 global.css 令牌取，不在组件内硬编码十六进制（除 SVG 必需的
 * 渐变 stop 兜底，已与令牌对齐）。
 */
import React from 'react'
import { BRAND, TAGLINE } from '../../brand/identity'
import { useModelReadiness } from '../../brand/modelReadiness.js'

// 六节点神经环几何（viewBox 120×120，中心 60,60，环半径 42）
const CENTER = 60
const RING_R = 42
const NODES = Array.from({ length: 6 }, (_, i) => {
  const ang = (-90 + i * 60) * (Math.PI / 180)
  return {
    x: CENTER + RING_R * Math.cos(ang),
    y: CENTER + RING_R * Math.sin(ang),
    // 颜色走令牌：金 / 蓝 / 紫 轮换，体现「金融×AI」双属性
    tone: ['gold', 'blue', 'purple', 'gold', 'blue', 'purple'][i],
  }
})

const TONE_VAR = { gold: 'var(--gold)', blue: 'var(--blue)', purple: 'var(--purple)' }

function RingMark({ size = 38, withSpokes = true }) {
  const ringEdges = NODES.map((n, i) => {
    const next = NODES[(i + 1) % NODES.length]
    return (
      <line
        key={`e${i}`}
        x1={n.x} y1={n.y} x2={next.x} y2={next.y}
        stroke="var(--gold)" strokeOpacity="0.35" strokeWidth="1.4"
      />
    )
  })
  const spokes = withSpokes
    ? NODES.map((n, i) => (
        <line
          key={`s${i}`}
          x1={CENTER} y1={CENTER} x2={n.x} y2={n.y}
          stroke={TONE_VAR[n.tone]} strokeOpacity="0.22" strokeWidth="1.1"
        />
      ))
    : null
  const nodes = NODES.map((n, i) => (
    <g key={`n${i}`}>
      <circle cx={n.x} cy={n.y} r="5.4" fill={TONE_VAR[n.tone]} fillOpacity="0.18" />
      <circle cx={n.x} cy={n.y} r="2.6" fill={TONE_VAR[n.tone]} />
    </g>
  ))
  return (
    <svg
      viewBox="0 0 120 120"
      width={size} height={size}
      role="img" aria-label={`${BRAND.shortName} 标志`}
      style={{ display: 'block', flexShrink: 0 }}
    >
      <defs>
        <radialGradient id="logo-halo" cx="50%" cy="50%" r="50%">
          <stop offset="55%" stopColor="rgba(255,207,77,0)" />
          <stop offset="100%" stopColor="rgba(255,207,77,0.30)" />
        </radialGradient>
      </defs>
      <circle cx={CENTER} cy={CENTER} r={RING_R + 6} fill="url(#logo-halo)" />
      {ringEdges}
      {spokes}
      {nodes}
      {/* 中心 Au —— 黄金符号，金主色 */}
      <circle cx={CENTER} cy={CENTER} r="13" fill="rgba(255,207,77,0.10)"
              stroke="var(--gold)" strokeOpacity="0.5" strokeWidth="1" />
      <text
        x={CENTER} y={CENTER + 5.5} textAnchor="middle"
        fontSize="15" fontWeight="900" fill="var(--gold)"
        fontFamily="'JetBrains Mono', ui-monospace, monospace"
        letterSpacing="0.5"
      >Au</text>
    </svg>
  )
}

/** 仅标志（侧栏折叠态 / favicon 占位 / 角标） */
export function LogoMark({ size = 38 }) {
  return <RingMark size={size} />
}

/** 标志 + 品牌名（侧栏展开态 / 顶栏） */
export function LogoHorizontal({ size = 38, showSub = false }) {
  // 副文取运行时实测就绪度，未登录/取不到时自动回落静态常量。
  // 详见 brand/modelReadiness.js 里的「虚标红线」说明。
  const rd = useModelReadiness()
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
      <RingMark size={size} />
      <div style={{ display: 'flex', flexDirection: 'column', lineHeight: 1.15 }}>
        <span style={{
          fontSize: 15, fontWeight: 800, color: 'var(--txt)',
          letterSpacing: 0.5, whiteSpace: 'nowrap',
        }}>{BRAND.fullName}</span>
        {showSub && (
          <span style={{
            fontSize: 11, color: 'var(--sub)', whiteSpace: 'nowrap', marginTop: 2,
          }}>{rd.main || TAGLINE.main}</span>
        )}
      </div>
    </div>
  )
}

/** 标志在上 + 品牌名 + 副文（登录页 / 关于弹窗 / 安装器） */
export function LogoStacked({ size = 64, sub = true }) {
  const rd = useModelReadiness()
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 12 }}>
      <RingMark size={size} />
      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', lineHeight: 1.2 }}>
        <span style={{
          fontSize: 22, fontWeight: 900, color: 'var(--txt)', letterSpacing: 1,
          textAlign: 'center',
        }}>{BRAND.fullName}</span>
        {sub && (
          <span style={{
            fontSize: 12.5, color: 'var(--sub)', marginTop: 6, textAlign: 'center',
          }}>{rd.main || TAGLINE.main}</span>
        )}
      </div>
    </div>
  )
}

/** 默认导出 = 横向（最常用） */
export default LogoHorizontal
