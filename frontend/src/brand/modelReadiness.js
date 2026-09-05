/**
 * 模型就绪度运行时store —— 品牌副文的「虚标红线」执行器
 *
 * ── 为什么需要它 ────────────────────────────────────────────
 * identity.js 里的 QUAD_MODEL_READY 是构建期常量。构建期我们无法知道
 * 客户那台机器上到底装没装 Ollama、拉没拉 qwen3:8b。
 * 如果把它写死成 true，等于对所有客户宣称「四模型协同」，
 * 而其中一部分人的第四个模型根本没装 —— 这就是虚标。
 * 反过来写死 false，装了的客户又看不到自己该有的东西。
 *
 * 所以就绪度必须是运行时的、来自后端实测的。
 *
 * ── 三条约束 ────────────────────────────────────────────────
 * 1. 判定收口在后端。前端不许自己数模型个数，也不许自己拼副文——
 *    一旦两处逻辑分叉，迟早出现「侧栏说四模型、系统管理页说三模型」。
 *    后端 /api/local-model/status 直接下发 tagline，前端照抄。
 * 2. 全局只轮询一份。Logo 在侧栏和登录页可能同时出现好几个实例，
 *    每个实例各自 fetch 就成了 DDoS 自己。这里用模块级单例 + 订阅。
 * 3. 拿不到数据时回落静态常量，绝不显示空白或"加载中"。
 *    品牌名下面挂一行"加载中…"，比显示保守副文难看得多。
 */
import { useEffect, useState } from 'react'
import { TAGLINE, QUAD_MODEL_READY } from './identity.js'
import { localModelStatus, getToken } from '../services/api.js'

const FALLBACK = {
  ready: QUAD_MODEL_READY,
  readyCount: null,
  total: 4,
  main: TAGLINE.main,
  compact: TAGLINE.compact,
  loaded: false,
  // ★ 2026-08-19 云端永久弃用：运行模式与角色文案一律由后端下发（约束 1「判定收口在后端」）。
  //   qwen3:8b 已移出投票专职校验层（校对/仓管/L2平仓），视觉 0.30、Chronos 锚 0.22、体制 0.20。
  //   角色若在前端各组件里写死，改一次架构就要满仓库改文案，且必然漏掉几处变成虚假陈述。
  //   拿不到时给 null，消费方 `roles?.qwen || '本地语义模型'` 保守兜底，不猜角色。
  mode: null,          // 'cloud_hybrid' | 'local_only'
  modeLabel: '',
  cloudEnabled: null,  // null = 还不知道（区别于明确的 false，避免误报"云已关"）
  localReady: null,
  localTotal: null,
  roles: null,
  // 本地双核各自的在岗状态。除了品牌副文，降级车道也要用它——
  // L2 档位的文案写着「本地副驾接管」，那是一句**承诺**。
  // 若 Qwen 其实没上线，这句话就变成了骗客户："你以为有人接管，其实没有。"
  // 所以就绪度必须能被降级面板读到，让文案随实况变化。
  qwen: false,
  chronos: false,
  // 视觉模型(qwen2.5vl:7b)以 0.30 权重实际参与决策，却一直没进这个就绪度 store，
  // 导致"本地阵容亮灯"永远少一盏 —— 关云后它是三脑之一，必须透出。
  vision: false,
  // 云端双脑逐个的就绪明细（{deepseek:{ready,down,...}, hunyuan:{...}}）。
  // 「关于」弹窗的阵容表要逐行亮灯，只有汇总数字是不够的。
  cloud: {},
}

let _state = { ...FALLBACK }
let _subs = new Set()
let _timer = null
let _refs = 0

function _emit() {
  // 拷贝一份再发，避免订阅者拿到同一个对象引用导致 React 判定"没变"
  const snap = { ..._state }
  _subs.forEach((fn) => {
    try { fn(snap) } catch { /* 单个订阅者出错不影响其他人 */ }
  })
}

async function _poll() {
  // 未登录时不发请求。登录页也会渲染 Logo，若照发请求会拿到 401，
  // 而 getJSON 收到 401 会广播 auth:logout —— 在登录页广播登出属于噪音，
  // 更糟的是万一将来有人在那个事件上挂了副作用，就成了自踩。
  if (!getToken()) {
    _state = { ...FALLBACK }
    _emit()
    return
  }
  try {
    const d = await localModelStatus()
    const s = d?.summary || {}
    const n = s.model_ready_count
    const total = s.model_total ?? 4
    const quad = !!s.quad_ready
    const cloudOn = typeof s.cloud_enabled === 'boolean' ? s.cloud_enabled : null
    // ★ 虚标红线（2026-08-18）：旧兜底文案写死「云端双脑 × 本地时序」。用户已关云，
    //   一旦后端漏发 tagline，界面就会对着一个零云成本的系统宣称有云端双脑 —— 属反向虚标。
    //   兜底文案必须跟着运行模式走；模式未知时只说客观事实（几模型协同），不提云。
    const fallbackMain = cloudOn === false
      ? '本地多脑协同 · 全本地推理零云成本'
      : (cloudOn === true ? '云端双脑 × 本地多模型 · 协同决策' : TAGLINE.compact)
    const readyN = typeof n === 'number' ? n : null
    _state = {
      ready: quad,
      readyCount: readyN,
      total,
      // 后端给了就用后端的；没给则按运行模式退回保守文案（绝不写死云端字样）。
      main: s.tagline || fallbackMain,
      // compact 旧实现只有"四/三模型"两档，关云后本地三脑就绪时会被叫成"四模型协同"。
      // 改为直接按实际就绪数表述，数字对不上就不给数字。
      compact: readyN != null ? `${readyN}/${total} 模型协同决策` : (s.tagline || fallbackMain),
      loaded: true,
      qwen: !!(d?.qwen?.available),
      chronos: !!(d?.chronos?.available),
      vision: !!(d?.vision?.available),
      cloud: (d?.cloud && typeof d.cloud === 'object') ? d.cloud : {},
      mode: s.mode || null,
      modeLabel: s.mode_label || '',
      cloudEnabled: cloudOn,
      localReady: typeof s.local_ready === 'number' ? s.local_ready : null,
      localTotal: typeof s.local_total === 'number' ? s.local_total : null,
      roles: (s.roles && typeof s.roles === 'object') ? s.roles : null,
    }
  } catch {
    // 401 / 网络不可达 / 后端没起 —— 一律静默回落，不打扰用户。
    // 品牌区域出现报错信息是很掉价的。
    if (!_state.loaded) _state = { ...FALLBACK }
  }
  _emit()
}

function _start() {
  if (_timer) return
  _poll()
  // ★★ 2026-08-18 P0 修复：这里原本写的是 `_listeners.size`，而本模块从来
  //   没有 `_listeners` 这个变量（订阅集合叫 `_subs`，见 L45）。
  //   后果非常隐蔽：首次 _start() 里的 setTimeout 能正常排上，但那一次 tick
  //   执行到 `_listeners.size` 就抛 ReferenceError —— 异常发生在 setTimeout
  //   回调内部，既不会让页面崩溃，也不会被任何人捕获，而 `_timer = setTimeout(...)`
  //   那一行永远执行不到。于是**轮询链在第 30 秒断掉，此后永不恢复**。
  //   实际影响：后端 /api/local-model/status 下发的 tagline / 就绪度是活的，
  //   前端却永久停在第一帧。用户切云开关、模型上下线、Ollama 挂掉，
  //   侧栏与 Logo 副文全都不变 —— 正是本轮"按云开关动态下发副文"要治的病，
  //   若不修这一行，后端所有动态口径都传不到界面上（最后一公里断路）。
  //   顺手把整个 tick 包进 try/finally：轮询链的续期绝不能被任何异常打断，
  //   否则又会退化成同一类"静默停摆"。
  const tick = async () => {
    try {
      await _poll()
    } finally {
      if (_subs.size > 0) _timer = setTimeout(tick, 30000)
      else _timer = null
    }
  }
  _timer = setTimeout(tick, 30000)
}

function _stop() {
  if (_timer) { clearTimeout(_timer); _timer = null }
}

/**
 * 订阅模型就绪度。返回 { ready, readyCount, total, main, compact, loaded }。
 * 组件卸载时自动退订；最后一个订阅者走掉时停止轮询。
 */
export function useModelReadiness() {
  const [st, setSt] = useState(_state)
  useEffect(() => {
    const fn = (s) => setSt(s)
    _subs.add(fn)
    _refs += 1
    _start()
    return () => {
      _subs.delete(fn)
      _refs -= 1
      if (_refs <= 0) _stop()
    }
  }, [])
  return st
}

/** 供非 React 场景（例如 document.title）取当前快照 */
export function currentReadiness() {
  return { ..._state }
}

/** 测试用：重置内部状态 */
export function __resetReadiness() {
  _stop()
  _state = { ...FALLBACK }
  _subs = new Set()
  _refs = 0
}
