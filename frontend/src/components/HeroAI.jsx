// 首屏 Hero：系统身份传达（纯AI定位）
// 命中铁律：① 纯AI系统身份（抛弃传统EA） ② 高端酷炫
export default function HeroAI() {
  return (
    <div className="hero">
      {/* 系统身份 */}
      <div className="hero-id">
        <NeuralSVG />
        <div className="hero-txt">
          <div className="hero-title">
            万象 <span className="ai-grad">AI</span> 智能交易系统
          </div>
          <div className="hero-sub">抛弃传统 EA · 前沿 AI 纯驱动 · 持续盈利</div>
          <div className="hero-tags">
            <span className="ht">DeepSeek V4</span>
            <span className="ht">混元 Hy3</span>
            <span className="ht">多模型协同进化</span>
          </div>
        </div>
      </div>
    </div>
  )
}

// 神经网络装饰 SVG
function NeuralSVG() {
  const L = [[34,34],[34,92],[34,150],[92,18],[92,58],[92,98],[92,138],[150,44],[150,112]]
  const layers = [[0,1,2],[3,4,5,6],[7,8]]
  const edges = []
  for (let li = 0; li < layers.length - 1; li++)
    for (const a of layers[li]) for (const b of layers[li + 1]) edges.push([a, b])
  return (
    <svg className="nn-svg" viewBox="0 0 184 184" width="92" height="92">
      <defs>
        <linearGradient id="nnGrad" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#ffcf4d" /><stop offset="1" stopColor="#7aa8ff" />
        </linearGradient>
      </defs>
      {edges.map(([a,b],i) => {
        const [x1,y1]=L[a],[x2,y2]=L[b]
        return <line key={'e'+i} className="nn-line" x1={x1} y1={y1} x2={x2} y2={y2} stroke="url(#nnGrad)" strokeWidth="1" style={{animationDelay:(i%7)*.18+'s'}}/>
      })}
      {L.map(([x,y],i) => (
        <circle key={'n'+i} className="nn-node" cx={x} cy={y} r="4.5" fill="url(#nnGrad)" style={{animationDelay:(i%5)*.22+'s'}}/>
      ))}
    </svg>
  )
}
