/**
 * 品牌与版本单一权威源（V6 第十章 / Phase 7.1 + 7.2）
 *
 * 铁律：
 *   1. 全站任何位置需要品牌名 / 副文 / 版权 / 版本号，一律从这里 import，
 *      严禁手写字符串。历史教训：登录页页脚、后端 config、package.json 三处
 *      各写各的版本号且互相矛盾，客户报障时说不清跑的是哪一版。
 *   2. 「Ai」是固定写法。禁止 万象AI / 万象ai / 万象A.I.。
 *   3. ★ 虚标红线：副文里的「四模型」必须等 Phase 6（本地 Qwen3-8B 降级车道）
 *      交付且混沌演练通过后才可对外。当前 Qwen3-8B 未落地 = 实际只有 3/4，
 *      所以 SUBTITLE 走过渡版「三模型协同决策」。切换开关见下方 QUAD_MODEL_READY。
 *      不得称「四个大模型」——Chronos-2 是时序模型不是 LLM。
 */

// ─────────────────────────────────────────────────────────────
// 构建期注入常量（vite.config.js define）。
// 用 typeof 守卫是为了让单元测试/SSR 等非 Vite 环境不至于 ReferenceError。
// ─────────────────────────────────────────────────────────────
export const APP_VERSION =
  typeof __APP_VERSION__ !== 'undefined' ? __APP_VERSION__ : '0.0.0-unknown'
export const BUILD_TIME =
  typeof __BUILD_TIME__ !== 'undefined' ? __BUILD_TIME__ : ''
export const GIT_COMMIT =
  typeof __GIT_COMMIT__ !== 'undefined' ? __GIT_COMMIT__ : 'unknown'

// ─────────────────────────────────────────────────────────────
// 品牌命名（2026-08-07 定稿，全站唯一权威）
// ─────────────────────────────────────────────────────────────
export const BRAND = {
  /** 全称，用于登录页、关于弹窗、安装器 */
  fullName: '万象Ai 智能交易系统',
  /** 短名，用于侧栏、标题栏、狭窄容器 */
  shortName: '万象Ai',
  /** 英文名，用于 favicon alt / 海外物料 */
  nameEn: 'WanxiangAi',
  /** 主打品类 */
  category: 'XAU/USD 智能量化交易',
}

/**
 * ★ 四模型副文闸门。
 * Phase 6 交付 + 混沌演练通过前恒为 false，届时改 true 即全站切换，
 * 不需要再去翻十几个组件改文案。
 */
export const QUAD_MODEL_READY = false

// ★ 2026-08-19 云端 DS/HY 永久弃用：副文不再出现「云端双脑」。
const SUBTITLE_QUAD = '全本地三脑协同 · 四模型决策'
const SUBTITLE_TRI = '全本地三脑协同 · 纯本地决策'

export const TAGLINE = {
  /** 主副文（登录页、关于弹窗） */
  main: QUAD_MODEL_READY ? SUBTITLE_QUAD : SUBTITLE_TRI,
  /** 紧凑副文（侧栏、窄栏） */
  compact: QUAD_MODEL_READY ? '四模型协同决策' : '全本地三脑协同',
  /** 英文副文 */
  en: QUAD_MODEL_READY ? 'Quad-model consensus engine' : 'All-local tri-brain consensus engine',
}

/**
 * 模型阵容说明（关于弹窗展开用）。
 * ready=false 的条目在 UI 上标灰并注「即将上线」，绝不冒充已交付。
 */
// ★ 2026-08-19 云端永久弃用：qwen3:8b 定位校验层（校对/仓管/L2平仓），已移出方向投票。
//   这里是构建期常量，运行期一律被后端 summary.roles 覆盖，
//   所以写成不含"云/降级"预设的中性描述，避免关云时兜底文案自己变成虚标。
//   同时补上视觉模型：它以 0.30 权重实打实参与方向裁决，阵容常量里却一直缺席。
// ★ 2026-08-19 云端永久弃用：移除 DeepSeek/混元条目；qwen3:8b 定位校验层（校对+仓管+L2平仓）；
//   视觉 7b 实际在线 ready=true；Chronos-2 为时序方向锚。
export const MODEL_LINEUP = [
  { key: 'vision', label: 'Qwen2.5-VL 7B', role: '本地视觉 · 图表结构票 (0.30)', tier: '感知层', ready: true },
  { key: 'chronos', label: 'Chronos-2 120M', role: '本地时序 · 方向锚 (0.22)', tier: '感知层', ready: true },
  { key: 'qwen', label: 'Qwen3-8B', role: '本地校验 · 校对/仓管/L2平仓', tier: '校验层', ready: true },
  { key: 'tsf', label: 'TimesFM/Time-MoE/Moirai', role: '本地时序 · 参考观测（不投票）', tier: '观测层', ready: true },
]

// ─────────────────────────────────────────────────────────────
// 版权与合规
// ─────────────────────────────────────────────────────────────
/** 起始年份写死，结束年份动态取，避免每年手改漏改 */
const COPYRIGHT_SINCE = 2026

export function copyrightText() {
  const y = new Date().getFullYear()
  const span = y > COPYRIGHT_SINCE ? `${COPYRIGHT_SINCE}-${y}` : `${y}`
  return `© ${span} ${BRAND.shortName}`
}

/** 登录页页脚：版权 + 版本，版本永远来自构建期注入 */
export function footerText() {
  return `${copyrightText()} · 商业版 v${APP_VERSION}`
}

/**
 * 风险提示（合规必备，「关于」弹窗必须展示）。
 * 品牌文案禁任何收益承诺——这条是红线，不得为营销弱化。
 */
export const RISK_DISCLAIMER =
  '外汇及贵金属保证金交易具有高风险，可能导致本金全部损失。本系统提供的分析与自动执行' +
  '功能不构成投资建议，历史表现不代表未来收益。请在充分理解风险并具备相应承受能力后使用。'

/** 第三方模型声明：不得暗示与模型厂商存在合作关系 */
export const THIRD_PARTY_NOTICE =
  '本系统基于 DeepSeek、腾讯混元、Amazon Chronos 等第三方公开 API / 开源模型构建，' +
  '与上述厂商无合作或背书关系。'

export default BRAND
