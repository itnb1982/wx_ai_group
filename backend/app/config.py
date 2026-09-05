"""
XAU/USD万象Ai自动量化交易系统 — 全局配置管理
所有配置项可通过环境变量覆盖，支持 .env 文件
"""
import os
from pathlib import Path
from pydantic_settings import BaseSettings

from app.version import get_version as _get_version
from runtime_paths import data_dir

# 后端根目录（config.py 位于 backend/app/ 下），用于锁定 .env 的绝对路径，
# 避免 uvicorn 从不同工作目录启动时读不到 .env 而回退到被 Defender 锁定的旧库。
BASE_DIR = Path(__file__).resolve().parent.parent
# 项目根（BASE_DIR 是 backend/，上一级才是项目根）。
PROJECT_ROOT = BASE_DIR.parent


class Settings(BaseSettings):
    # ========== 应用基础 ==========
    APP_NAME: str = "XAU/USD万象Ai自动量化交易系统"
    # ★ 版本号唯一权威源 = 项目根 VERSION 文件（V6 Phase 7.1）。
    #   严禁在此写死字面量——历史上后端 1.0.0 / package.json 0.1.0 / 登录页 v1.0.0
    #   三处互相矛盾，客户报障时说不清跑的是哪一版。
    APP_VERSION: str = _get_version()
    APP_DESCRIPTION: str = "DeepSeek V4 + 混元 Hy3 双模型AI自动交易系统 — 专注XAUUSD"
    DEBUG: bool = False

    # ========== 数据目录与数据库（可移植性关键）==========
    # ★ 铁律：默认值必须是「相对于我自己所在的位置」，绝不能是任何绝对盘符。
    #
    #   历史教训：这里曾默认 Path.home()/".wanxiangai"，而 runtime_paths.data_dir()
    #   默认 <项目根>/data —— 同一套程序里两个「数据目录」指向两个地方，
    #   于是日志写 A 处、状态文件写 B 处，排障时看到的永远只是一半真相。
    #   更糟的是 .env 里又用绝对路径 F:/... 把两边强行拉回同一处，
    #   掩盖了分叉，直到项目被拷到没有 F 盘的客户机上才集中爆发。
    #
    #   现在两处默认值统一为 <项目根>/data，且 .env 中不再出现任何绝对路径。
    DATA_DIR: str = str(data_dir())
    DATABASE_URL: str = ""

    def get_database_url(self) -> str:
        """数据库连接串。空 DATABASE_URL 时按项目自身位置推导。

        默认落点 = <项目根>/backend/data/wx_prod.dat，
        与本机既有生产库同一路径 —— 这样删掉 .env 里的绝对路径后
        本机行为零变化，不会突然连到一个新建的空库上（那等同于「清库」）。

        注意必须用 as_posix()：database.py 是靠字符串
        replace("sqlite:///","") 还原文件路径的，Windows 反斜杠在
        某些 SQLAlchemy/URL 解析路径上会被当作转义字符吞掉。
        """
        if self.DATABASE_URL:
            return self.DATABASE_URL
        db_path = BASE_DIR / "data" / "wx_prod.dat"
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            # 只读盘/权限不足时不在配置层炸掉；让 database.py 的
            # 连接重试与自愈逻辑去报可读的错。
            pass
        return f"sqlite:///{db_path.as_posix()}"

    # ========== 安全 ==========
    SECRET_KEY: str = ""  # 安装时自动生成
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 1440  # 24小时

    # ========== 服务器 ==========
    HOST: str = "127.0.0.1"
    PORT: int = 8080
    CORS_ORIGINS: str = "http://localhost:8080,http://127.0.0.1:8080"

    # ========== DeepSeek V4 ==========
    # 2026-08-11 用户硬性要求：全程强制 deepseek-v4-flash，彻底禁用 pro（pro 费用过高）。
    # 原「复杂/分歧场景升级 pro」逻辑已移除——flash 对 XAUUSD 技术分析已足够，降本优先。
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"
    DEEPSEEK_MODEL: str = "deepseek-v4-flash"
    DEEPSEEK_THINK_MODEL: str = "deepseek-v4-flash"

    # ========== 腾讯混元 Hy3（TokenHub 平台） ==========
    HUNYUAN_API_KEY: str = ""
    HUNYUAN_BASE_URL: str = "https://tokenhub.tencentmaas.com/v1"
    HUNYUAN_MODEL: str = "hy3"

    # ========== 云模型总开关（2026-08-11 新增·降本切换）==========
    # ★★ 2026-08-19 用户决策：云端 DS/HY 方案【永久弃用】，全本地化运行。
    #    默认值改 False 是防回退保险（DB runtime_config 仍为主，此处兜底）。
    #    True : 正常调用 DeepSeek + 混元云端双脑。
    #    False: 完全禁用云端 LLM 调用，决策链只使用本地模型（视觉+时序锚+校对员）。
    ENABLE_CLOUD_MODELS: bool = False
    # ── 大脑审计：统一审计每个模型的接入/输出/消费（见 services/brain_audit.py）──
    BRAIN_AUDIT_ENABLED: bool = True

    # ========== AI 决策参数 ==========
    AI_DECISION_INTERVAL: int = 30        # 决策周期(秒)·2026-08-14 提速：开新仓基础间隔 60→30
    AI_DEBATE_ROUNDS: int = 2             # 多空辩论轮次
    AI_MAX_TOKENS_ANALYSIS: int = 4096    # 分析阶段最大token（2026-08-12 修复：恢复4096；2048导致deepseek-v4-flash输出被截断、云端双脑故障降级。仍禁止length时翻倍到8192，避免费用失控）
    AI_MAX_TOKENS_DEBATE: int = 1024      # 辩论阶段最大token
    AI_TEMPERATURE: float = 0.3           # 推理温度

    # ========== 新闻 / 舆情层（2026-08-13 新增·结构性补短板）==========
    # 调研支撑（≥3 源交叉验证）：goldprice.com(AI 情绪掘金)、dataconomy.com(2026 AI 黄金 bots)、
    # gainsium.com(Best AI Trading Tools 2026)、TradingAgents(GitHub 32K+★ 多智能体·独立 News/Sentiment Analyst)
    # 一致结论：黄金对 Fed/地缘极敏感、新闻情绪是 SOTA 框架的独立分析层，且与宏观信号「互补不替代」。
    # 设计：定时拉取 XAUUSD 相关 RSS（多源）→ 词典情绪分 → 聚合 gold_sentiment_score[-1,1] +
    # 高影响事件标记(FOMC/CPI/NFP/地缘)。"Blank beats wrong"：无新鲜新闻→has_news=False 不注入。
    # 舆情只做「提准」（高影响事件下逆新闻方向需更高置信才放行），绝不 blanket 拦截。
    NEWS_ENABLED: bool = True
    NEWS_REFRESH_SEC: int = 300            # 后台刷新间隔(秒)
    NEWS_WINDOW_HOURS: float = 6.0         # 情绪聚合回看窗口(小时)
    NEWS_HIGH_IMPACT_HOURS: float = 12.0   # 高影响事件有效窗口(小时)
    NEWS_SENTIMENT_BIAS_THRESHOLD: float = 0.30   # |score|≥此值才认为有明确多/空偏向
    NEWS_CONFLICT_MIN_CONF: float = 0.80   # 高影响事件下逆新闻方向开单所需的最低置信(提准非拦截)
    NEWS_MAX_ITEMS: int = 30               # 内存保留最新条目数

    # ========== 逆共识高置信闸门（2026-08-13 新增·基于大脑审计「发现1」）==========
    # 审计闭环(227笔/146平仓): META 逆三脑(DS/HY/Chronos)共识单胜率 51% < 共识单 56%,
    # 元智能体独立 override 拖累信号准度。提准非拦截: 仅当 META 终裁逆三脑共识
    # 且 置信<此阈值时,**降级采用共识方向**(保留交易、不腰斩笔数);高置信(≥阈值)才放行逆共识。
    # 详见 app/core/decision_gates.py。
    CONSENSUS_OVERRIDE_ENABLED: bool = True
    CONSENSUS_OVERRIDE_MIN_CONF: float = 0.80

    # ========== 置信校准层（2026-08-13 新增·基于调研「LLM 过自信」实证）==========
    # 调研实证（≥3 源交叉验证）：LLM 集成输出的「自报置信度」系统性过自信
    # （PredictEngine / Polymarket 研究 / paperswithbacktest）。
    # 本系统用 final_confidence 作「逆共识闸门 / 新闻闸门」阈值——虚高置信会让弱逆共识单
    # 被放行，拖累信号准度（上轮审计「发现1」）。故加「后处理校准层」：
    #   把 raw meta_agent_confidence 映射为历史观测命中率（Isotonic/Platt 按 Brier 选优），
    #   让 0.80 阈值真正表示 80% 命中。详见 app/core/confidence_calibrator.py。
    # 运行期仅加载离线生成的 data/confidence_calibration.json 查表，零 DB 访问、零推理成本。
    # 「提准非拦截」：只改阈值语义、不改开仓笔数（降级仍保留交易只改方向）、默认不改仓位大小。
    CONFIDENCE_CALIBRATION_ENABLED: bool = True
    # ★ 安全守门员：校准值是否介入「闸门阈值」(新闻/逆共识)。默认 False → 闸门仍用 raw 置信，
    #   零行为变化、保护「交易笔数不腰斩」及格线。须先 walk-forward 验证净盈利提升后方可置 True。
    CONFIDENCE_CALIBRATION_AFFECTS_GATES: bool = False
    CONFIDENCE_CALIBRATION_AFFECTS_SIZING: bool = False  # 默认不改手数，避免净利意外波动，守住及格线

    # ========== 风控参数（全局默认，可逐账号覆盖） ==========
    RISK_MAX_POSITION_LOTS: float = 1.0   # 最大持仓手数
    RISK_MAX_DAILY_LOSS_PCT: float = 5.0  # 日亏损上限(%)
    RISK_MAX_DRAWDOWN_PCT: float = 20.0   # 回撤熔断阈值(%)
    RISK_MAX_SPREAD_POINTS: int = 50      # 最大点差
    RISK_MAX_RISK_PER_TRADE_PCT: float = 2.0  # 单笔最大风险%
    RISK_MIN_CONFIDENCE: float = 0.58     # AI决策最低置信度（2026-08-06 下调：原0.60/策略0.65门槛过严，
                                           # 在强趋势中 DS 给出 0.60-0.62 的合理方向判断却被 HY 的 HOLD 拖到阈值下，
                                           # 导致 0 成交、违背"多交易多赚钱"铁律；0.58 仍过滤弱信号、保留全部风控栈）

    # ========== Chronos 本地时序大脑投票权 ==========
    # 2026-08-07 修复：Chronos 不再只是"质量陪审团"，而是与 DeepSeek/混元并列的第三辩论角色。
    CHRONOS_VOTE_WEIGHT: float = 0.25      # Chronos 方向在 Meta-Agent 加权投票中的权重
    CHRONOS_AGREE_BONUS: float = 1.05      # Chronos 与云模型同向时置信奖励（封顶 0.98）
    CHRONOS_OPPOSE_PENALTY: float = 0.85   # Chronos 与云模型反向时置信惩罚（提准非拦截）

    # ========== SMC 订单流方向锚（2026-08-11 新增·Fix A）==========
    # meta_agent 决策层把「机构订单流大脑」(smc_features.global_bias) 作为方向锚：
    #   旧逻辑只读 regime/extension_z，从不读 SMC global_bias → 16:20 cycle 在 SMC=bullish
    #   (机构订单流看涨) 时仍逆势开了 SELL @4362，价格随后涨到 4371+ 浮亏扩大。
    #   - hard ：逆机构订单流方向直接拦截，并翻向订单流方向（行情对就多赚）
    #   - soft ：逆机构订单流方向重罚置信×0.5（提准非拦截）
    #   - off  ：不干预（维持旧行为）
    # 豁免：反转哨兵确认真反转(REVERSE_* 且哨兵置信≥0.6) → 订单流已被真实推翻，放行。
    SMC_FLOW_GUARD: str = "soft"
    SMC_FLOW_FLIP_CONF: float = 0.66   # hard 模式翻向订单流方向时的置信（≥开仓门槛 0.58/策略0.65）

    # ========== 亚盘/清淡时段方向确认增强（2026-08-14 新增·提准非拦截·治三单错方向根因）==========
    # 三单错方向全部落在 session_info=亚盘(moderate)，根因=弱共识方向在弱流动性时段被市价追开。
    # 调研(United Kings / fxroboteasy AI Gold Asia / Golden Goose Scalper / ALGOGENE)一致：
    #   亚盘假突破是 XAUUSD 最大亏损源，需收盘突破确认；亚盘信号频率本就低。
    # 原则：仅对「弱流动性(moderate/poor) + 弱共识(无三脑共振且 conf<0.72) + 无明确入场zone」
    #   的方向单降权(×PENALTY)，使其低于开仓门槛(≈0.5)→自然不市价追；强共识/AI已给zone照常→不腰斩。
    ASIAN_SESSION_DIR_GATE_ENABLED: bool = True
    ASIAN_SESSION_DIR_GATE_PENALTY: float = 0.70   # ★ 2026-08-18 上调 0.45→0.70：原 0.45 对弱共识单压到 0.30(<0.58门槛)致永远不开；0.70 仍降权但趋势明确单可达门槛

    # ========== 支撑/压力位置质量门（2026-08-19 新增·提准非拦截）==========
    # 模型已能识别关键位（structure_anchors / key_levels），裁决层用它修正入场质量：
    #   SELL 贴支撑 / BUY 贴阻力 时降权，避免"卖到支撑底、买到压力顶"。
    # 默认 threshold=1.0×ATR：0.5×ATR 内强降权(×0.55)，0.5~1.0×ATR 中降权(×0.75)，
    #   已突破关键位时不惩罚（顺势突破单照常）。
    SR_LOCATION_GATE_ENABLED: bool = True
    SR_LOCATION_THRESHOLD_ATR: float = 1.0      # 多远算"贴近"关键位（ATR 倍数）
    SR_LOCATION_PENALTY_NEAR: float = 0.55       # 强贴关键位时的置信乘数
    SR_LOCATION_PENALTY_MID: float = 0.75        # 中距离时的置信乘数

    # ========== 视觉模型第四票（2026-08-14 新增·加法增强·提准非拦截）==========
    # 让 AI 大脑实时看 H4/M15 蜡烛图识别市场结构（趋势/供给需求区/流动性扫荡），
    # 作为与 DS/HY/Chronos/融合票并列的加法增强信号，专治「亚盘震荡方向判断失效」。
    # 铁律：① 绝不做 GO/NO-GO 闸门（拦截=砍信号=利润腰斩）；② 与决策链同源(OHLC 一致)；
    #   ③ 视觉独立 Ollama 实例绑 gpu1(端口11435, CUDA_VISIBLE_DEVICES=1) GPU 推理，
    #      与主实例(gpu0/qwen3:8b)物理隔离，不与 qwen3:8b 抢显存；CPU 仍跑 Chronos-2+时序竞技场；
    #   ④ 后台低频生产者线程渲染+推理，决策链只读缓存票（零延迟）。
    # ★★ 2026-08-16 GPU 编号口径（用户纠正·防混淆）：
    #   本机 Windows 任务管理器视角：GPU0=核显(接显示器) / GPU1=第一张3060Ti / GPU2=第二张3060Ti。
    #   CUDA/nvidia-smi 视角只有 NVIDIA 卡：CUDA0=第一张3060Ti(Windows GPU1) / CUDA1=第二张3060Ti(Windows GPU2)。
    #   代码内 "gpu0/gpu1" 一律指 **CUDA 视角**：主实例 qwen3:8b=CUDA0=Windows GPU1；
    #   视觉实例 qwen2.5vl:7b=CUDA1=Windows GPU2。前端可视化文字已按 Windows 视角标注 GPU1/GPU2。
    VISION_VOTE_ENABLED: bool = True
    VISION_MODEL: str = "qwen2.5vl:7b"       # Ollama 视觉模型（CUDA gpu1 = Windows GPU2 第二张3060Ti）
    # ★ 2026-08-16 由 qwen2.5vl:3b 升级到 qwen2.5vl:7b（用户批准「开搞」·GPU1 显存充分利用）：
    #   3b 稳定但能力受限（图表/结构推理弱、常 HOLD）；7b 是**同源非-thinking 系**——
    #   保留 3b 的稳定优势（JSON 遵循好、无空 content 坑、不触发 qwen3-vl 的 thinking 预算陷阱），
    #   能力升一代（ChartQA 87.3 / 图表结构推理显著更强，willitrunai 实测 RTX3060Ti 8GB tight fit
    #   7.1GB、CSDN RTX4060 8GB 实测 5.2GB 稳定）。显存纪律：视觉实例绑 gpu1 独享 8GB，
    #   num_gpu=999 全量卸载；图片已 resize 最长边≤672 保持视觉 token 可控。
    #   仍铁律：视觉仅加法增强，绝不做 GO/NO-GO 闸门。
    VISION_VOTE_WEIGHT: float = 0.30        # 视觉票基础权重（2026-08-16 0.20→0.30：7b 能力升级后
    # ★ 2026-08-19 定稿P2-3 预置（默认关闭）：权重微调 视觉0.30→0.32 / 时序0.22→0.26。
    #   云端弃用后本地票是唯一方向来源，理论应聚焦加权；但属行为变更，
    #   须先积累 P1-1 观测数据 walk-forward（笔数不腰斩/净利不降/PF>1）验证后置 True 开启。
    LOCAL_WEIGHT_TUNING_ENABLED: bool = False
    VISION_VOTE_WEIGHT_TUNED: float = 0.32
    TS_FUSION_VOTE_WEIGHT_TUNED: float = 0.26
    # ★ 2026-08-19 定稿P2-2 预置（默认关闭）：本地共识置信折价。
    #   实测 meta_agent 本地加权共识 final_confidence=胜者得分/总得分（份额归一化），票少同向时虚高
    #   （视觉0.18+体制0.20 两票同向即 100%）。开启后 _local_sum<阈值 → 置信×折价因子。
    #   须 P1-1 观测数据积累后 walk-forward 验证再置 True。
    LOCAL_CONF_DISCOUNT_ENABLED: bool = False
    LOCAL_CONF_DISCOUNT_SUM: float = 0.35
    LOCAL_CONF_DISCOUNT_FACTOR: float = 0.85
                                            #   提高其在融合中分量，让视觉能真正影响开仓方向；仍低于双脑主导权重）
    VISION_H4_WEIGHT: float = 0.60          # H4(结构/趋势)在视觉聚合内权重
    VISION_M15_WEIGHT: float = 0.40         # M15(即时)在视觉聚合内权重
    VISION_M5_WEIGHT: float = 0.20          # M5(实时管理微结构·保本/收紧/部分平仓触发)在视觉聚合内权重（2026-08-14 三帧升级·仅自身清晰时发声）
    # ★ 2026-08-16 门控放宽（配合 7b 能力升级）：
    #   旧门控在「视觉与共识冲突」时 ×0.35 近乎压制，7b 的高质量独立判断被浪费。
    #   新值：一致→1.0 / 部分→0.80 / 冲突→0.55（仍守"提准非拦截"，但让视觉在"别人错它对"时能翻盘）。
    VISION_GATE_ENABLED: bool = True
    VISION_GATE_MID: float = 0.80           # 与共识或内部一致时的门控值
    VISION_GATE_MIN: float = 0.55           # 与共识冲突时的门控下限（旧 0.35，放宽让大模型能发声）
    VISION_GATE_CHOP_MULT: float = 0.85     # 震荡体制额外折扣（保持谨慎）
    # ★ 2026-08-19 审计P1落地：视觉置信度幻觉修正封顶。
    #   实证：H4/M15/M5 三帧内部不一致（仅一帧给方向）时模型仍报 95% 置信——
    #   LLM 自报置信系统性失准（FinBench/ECE），95% 直接乘权重=幻觉放大。
    #   三帧不一致时置信封顶至此值（诚实化打分，非拦截；方向/权重不变）。
    VISION_CONF_DISAGREE_CAP: float = 0.60
    # ★ 2026-08-14 双卡规划：视觉独立 Ollama 实例绑 gpu1（端口 11435）。
    VISION_OLLAMA_URL: str = "http://127.0.0.1:11435"   # 视觉专用实例（CUDA_VISIBLE_DEVICES=1 → gpu1）
    VISION_NUM_GPU: int = 999               # 视觉模型卸载层数：999=全量卸载到 gpu1；0=回退 CPU 推理
    VISION_REFRESH_SEC: float = 90.0        # 生产者刷新周期·2026-08-15 修正：旧值 15 基于「推理仅~2-4s」的错误假设，
                                            #   实测 qwen3-vl:4b 每次推理 ~18s，15s 间隔=GPU 从不休息→gpu1 长期 87°C 高温。
                                            #   H4 结构信号无需 15s 刷新，90s 仍远快于 H4 周期且把 gpu1 占空比降到 ~20%（留足冷却）。
                                            #   注：每轮推理耗时由模型/图复杂度决定，刷新周期必须 > 单次推理耗时才有冷却间隙。
    VISION_STALE_SEC: float = 900.0       # 缓存票僵死阈值（>15min 作废，防用过期图表指挥当下）

    # ★ 2026-08-15：视觉持仓看护(平仓巡检)刷新周期。
    #   ★★ 2026-08-16 铁律更正（用户纠正）：**视觉/AI 模型调用次数不随账号数 N 线性增长**——
    #   信号跟随主号、跟单复制主号。跟单账号(follow_leader=True)在 _manage_positions 内
    #   走 _mirror_leader_exits 镜像主号出场即 return，**根本不启动 VisionExitService**；
    #   只有主号/独立号(follow_leader=False)才启动一个 VisionExitService 推理（结果按
    #   publish_leader_exit 广播给跟单）。故 6 账号（将来 100 账号）永远只有 ~1 次看护推理，
    #   不存在「6 账号共享 GPU1 占空比上升」的担忧——旧注释该句作废。
    #   刷新周期仅由主号单实例推理耗时决定：7b 推理约 8~15s，30s 周期留足冷却间隙。
    VISION_EXIT_REFRESH_SEC: float = 30.0
    # ★ 2026-08-19 定稿P0-2：L2 反向平仓本地化。
    #   云端弃用后 ai_exit 原绑 deepseek_client 持续 error → L2 AI 反向平仓静默退化规则引擎。
    #   EXIT_LOCAL_BACKEND_ENABLED=True：关云时切本地 qwen3:8b 出场评估（evaluate_exits_local）。
    #   EXIT_REVERSE_STREAK_REQUIRED=2：本地 8B reverse_signal 需置信≥0.60 且连续 N 轮才 full_close。
    EXIT_LOCAL_BACKEND_ENABLED: bool = True
    EXIT_REVERSE_STREAK_REQUIRED: int = 2
    # ★ 2026-08-19 毫秒级跟单：主号 place_order 前早信号广播 → 挂号并行发单。
    #   实测旧串行（等主号成交再复制）开仓延迟 +0.55~1.3s；早信号并行后挂号与
    #   主号同时发单，成交时差收敛到网络/撮合差异（亚秒级）。False 完全回退旧路径。
    EARLY_COPY_ENABLED: bool = True

    # ========== 智能出场理念（2026-08-17·用户铁律：发现不对果断全平走人，不要锁50%）==========
    # 用户原话："我要求的是发现不对果断全部平仓走人，不要锁50%，宁愿等下一次机会也不要
    # 亏损离场。如果守护仓位AI模型推理出跌不下来，就利润最大化早点平仓等待下一次机会。"
    # 落地：①浮盈回吐锁利门槛绝对化（利润区 min(0.5×ATR,2.0点)、回吐≥max(峰值15%,0.25点)
    #       全平）——"涨不动就走"机械兜底，不依赖 AI 方向判断；
    #       ②禁用"浮盈达标锁50%"（留一半继续扛会在回吐时把利润搭进去）——要么持有要么全走。
    SMART_EXIT_LOCK50_ENABLED: bool = False   # 默认关：不锁50%，趋势健康持有、回吐全平
    # ★ 2026-08-17 用户理念（盈利即护盘·回撤一点就跑）：
    #   "只要仓位盈利了，AI就时刻盯着，回撤一点就要跑；赚到10+就准备着，几美金也可以，绝不等到亏损"
    #   ① 利润区 0.5 点（大仓0.5手≈$28、主号0.01手≈$5）即进入护盘——浮盈小目标即触发保护资格
    #   ② 回撤下限 0.30 点（≥点差+缓冲，0.01手≈$3）：回撤一点点就跑，但防 M5 纯噪音亏点差
    #   ③ 回吐比例 5%（峰值越高越早锁）：10 点峰值回撤 0.5 点即走——"10回到9就跑"的进一步收紧
    SMART_EXIT_PROFIT_ZONE_PT: float = 0.5    # 利润区点数地板（浮盈小目标即进护盘）
    SMART_EXIT_RETRACE_PT: float = 0.30       # 回吐下限点数（≥点差+缓冲，防噪音）
    SMART_EXIT_RETRACE_PCT: float = 0.05      # 回吐比例（峰值5%即锁利快走）

    # ★★ 2026-08-18 用户铁律·开仓即亏认错（补「盈利即护盘」盲区）★★
    # 实盘根因（昨晚三笔大亏 -779/-155/-118）：现有「浮盈回吐锁利」只覆盖「先盈后回吐」，
    #   对「开仓即逆方向、从未进盈利区」完全失效 → 扛到 SL/AI 认错才平，单笔亏 800/300/280 点。
    #   用户："绝不等到亏损、回撤一点就跑"。本参数让「开仓即亏」也触发认错平仓，是护盘完整镜像。
    #   阈值 = max(硬地板, ATR比例)：硬地板 8 点 > M5 噪音带(~6点) 防误杀；0.3×ATR 顺势调整。
    #   正常波动不会触发，仅「持续/快速反向突破」触发（方向真错即跑，远在 1.5×ATR 的 SL 之前）。
    CUT_LOSS_WRONG_DIR_PT: float = 3.0         # 开仓即亏认错硬地板（价格单位≈300点；>噪音带上限~0.29×ATR防误杀）
    CUT_LOSS_WRONG_DIR_ATR_MULT: float = 0.30 # 开仓即亏认错 ATR 比例（0.3×ATR，顺势调整）

    # ★★ 2026-08-18 打破 HY 恒 HOLD 死锁（用户问"为什么不开单"根因）★★
    #   混元权重封顶 0.65 且 HOLD 永远不被罚 → recent_accuracy 维持高位 → 权重锁 0.65 →
    #   每单落入 R2(一方向一观望) 高门槛 + 亚盘乘子 0.45 双重压 → final_conf<0.58 → 永远不开。
    #   修复：① 亚盘乘子 0.45→0.70（清淡时段降权不过度）；② 混元连续 HOLD 超阈值后权重衰减到地板，
    #   让方向模型(DS)主导，趋势明确时能开单；混元一旦重新给方向立即恢复竞争。
    HY_HOLD_DECAY_ROUNDS: int = 8             # 混元连续 HOLD 多少轮后权重衰减（打破死锁）

    # ★★ 2026-08-18 第三处修复：趋势明确时降权反向"提准器"（提准非拦截·多开顺势单）★★
    #   症状：在强跌趋势里 DS=SELL+HY=SELL 双云共识，但 Chronos/融合票反向 BUY(w=0.19) 被计入
    #   active_weight 分母 → 顺势 SELL 归一化置信被压到 41% < 0.58 → 不开单（结构性死区 cycle#4）。
    #   同时 SMC 软信号(bullish) 在强跌趋势里持续降权 SELL。两"提准器"在趋势明确时反向压制顺势单，
    #   违背用户"开平仓看同一盘面、多开顺势单赚钱"铁律。修复：趋势明确时这两个反向信号降权/豁免。
    SMC_TREND_EXEMPT: bool = True              # 强趋势时 SMC 软信号反向不压顺势单（趋势本身即短周期背书）
    CHRONOS_TREND_OPPOSE_MULT: float = 0.25   # 强趋势且 Chronos/融合票反向时，其权重乘子（降权避免撑大分母压死顺势单）


    # ========== 篮子级 AI 持仓管理（2026-08-17·用户铁律：开完仓核心任务=维护持仓）==========
    # 用户实盘观察：三单 SELL 合计 +10 美金回吐到 0 全程无 AI 干预——"智能平仓不智能"。
    # 根因：AI 大脑看得到持仓但输出协议无持仓处置字段；L3 篮子锁利是纯静态阈值。
    # 本组参数驱动双脑 position_action(hold/trim/close_all) 融合 + 篮子回吐保护：
    #   · AI 层：DS/HY 每轮输出持仓处置建议 → meta_agent 加权融合 → 连续 2 轮确认 +
    #     置信≥MIN_CONF → 执行（close_all 全平 / trim 每笔减半），120s 冷却。
    #   · 规则层兜底：篮子浮盈从峰值回吐 ≥ max(峰值×PCT, ABS) 且浮盈 ≥ MIN_FLOOR → 锁利。
    #   · 纯加法：不砍开仓、不删 L1/L2/L3/M1 任何保护；解析失败一律 hold。
    BASKET_AI_MGMT_ENABLED: bool = True               # 总开关
    BASKET_AI_MIN_CONF: float = 0.60                  # AI 处置置信门槛
    BASKET_AI_CONFIRM_CYCLES: int = 2                 # 连续确认轮数（防抖，meta_agent 模块级）
    BASKET_AI_TRIM_PCT: float = 0.5                   # trim 减仓比例
    BASKET_PULLBACK_ENABLED: bool = True              # 篮子回吐保护开关（规则兜底）
    BASKET_PULLBACK_MIN_FLOOR: float = 6.0            # 最低浮盈（$）才看回吐
    BASKET_PULLBACK_PCT: float = 0.5                  # 峰值回吐 50% 触发
    BASKET_PULLBACK_ABS: float = 8.0                  # 或绝对回吐 $8 触发
    BASKET_PULLBACK_TRIM_ONLY: bool = False           # True=回吐只减半不全会（默认全平锁利）

    # ========== 辩论环（TradingAgents 式·加法增强·提准非拦截·2026-08-15 新增）==========
    # 消费裁决阶段【已有的】多路信号(DS/HY/Chronos/融合票/视觉/副驾/SMC订单流/体制/风险)作
    # 「牛熊研究员 + 风控多视角审议团」，对抗式综合：仅当多视角明显分歧或风险偏高时，
    # 对 final_confidence 做乘性缩权(∈[FLOOR, 1.0])。绝不改方向、绝不硬HOLD、绝不砍笔数。
    # 零新增 LLM 调用 / 零延迟 / 完全可复现 A/B；异常即降级无影响。默认关闭，须显式开启做实验。
    DEBATE_RING_ENABLED: bool = False                 # 总开关（周一实盘 walk-forward A/B 前保持 False）
    DEBATE_RING_FLOOR: float = 0.80                  # 缩放下限（最坏情况置信也只降 20%，保护笔数）
    DEBATE_RING_DISAGREEMENT_PENALTY: float = 0.15   # 多视角全反对时的满额分歧惩罚
    DEBATE_RING_RISK_PENALTY: float = 0.05           # 风险偏高(high/高波动)的额外谨慎惩罚
    DEBATE_RING_MAX_PENALTY: float = 0.20            # 总惩罚封顶（避免过杀）

    # ========== Qwen3-8B 常态确认型副驾第五票（2026-08-14 升级·加法增强·提准非拦截）==========
    # 把主号 gpu0 上的 qwen3:8b 从「仅 L2 降级副驾」升级为「常态确认型副驾」，
    # 作为与 DS/HY/Chronos/融合票/视觉并列的第5路加权票进入 meta_agent 融合。
    # 定位（Fin-Bias ACL2026 实证·7~8B 金融方向近随机）：
    #   ★ 只做「确认型」、绝不做「生成型」——仅当与有效时序方向(Chronos/融合票)同向时才计入，
    #     绝不自创方向、绝不翻盘。三道安全锁常态化：
    #       ① 仅当 chrono_is_dir（时序有明确方向）才调 copilot → 无方向可确认就不动
    #       ② 置信 ≥ LOCAL_COPILOT_MIN_CONFIDENCE（与云端 ai_reverse_close 对齐）
    #       ③ 降权（基础权重 × 置信）加法并入 decision_scores，权重量级与视觉(0.20)同档
    #   调用经济性：copilot 仅当有时序方向时调，且按刷新周期缓存（仿视觉，避免 6 账号各调一次）。
    # ★ 2026-08-19 审计P0落地：副驾移出投票链。brain_audit 实证 2979 次调用中
    #   91% 输出 HOLD、置信度从未 ≥0.60 → 0 次过三道锁，0.15 权重永久空转且每次
    #   白耗 ~60s 推理时延。qwen3:8b 保留校对员(proofread)与仓位管理(position_manage)
    #   两个职责（独立方法，不走投票），仅关闭"第5路加权票"。可随时改回 True。
    LOCAL_COPILOT_VOTE_ENABLED: bool = False
    LOCAL_COPILOT_VOTE_WEIGHT: float = 0.15       # 基础权重（确认型，量级低于单云模型，与视觉0.20同档加法）
    LOCAL_COPILOT_MIN_CONFIDENCE: float = 0.60    # 放行最低置信（与 copilot_gate 默认一致）
    LOCAL_COPILOT_REFRESH_SEC: float = 15.0       # 副驾票缓存刷新周期（仿视觉·gpu0 推理仅~2-4s，15s实时占余量极小）

    # ========== 真进化·在线学习置信修正（2026-08-11 新增·闭环）==========
    # 把 EvolutionEngine 的「情境→期望盈亏」映射从"软提示文本"升级为"硬置信修正器"：
    #   外网 SOTA(ATLAS/ACL2026, LinUCB 折扣老虎机, 非平稳连续适应)一致结论——
    #   只读文本反思无法系统提升且制造偏置；正确做法是每笔盈亏持续更新特征权重+漂移折扣。
    #   本实现 = 折扣上下文老虎机：context=tag组合, arm=BUY/SELL, reward=pnl,
    #   策略=对 final_confidence 乘一个数据驱动乘子（提准非拦截·硬约束）。
    #   安全护栏：指数衰减(防陈旧)+收缩(防小样本过拟合)+乘子上下限(防"永不交易"/爆量)。
    EVOLUTION_MODIFIER_ENABLED: bool = True
    EVOLUTION_GAMMA: float = 0.97          # 指数衰减系数（非平稳折扣，≈周级半衰期）
    EVOLUTION_LEARN_RATE: float = 0.5      # 期望盈亏→乘子的学习率
    EVOLUTION_SCALE: float = 30.0          # 盈亏归一化尺度($)，典型单笔风险
    EVOLUTION_MIN_SAMPLE: float = 5.0      # 衰减样本数下限（未达→不干预，乘子≈1.0）
    EVOLUTION_MAX_PENALTY: float = 0.5     # 最大惩罚→乘子下限 0.5（不能一刀切到0）
    EVOLUTION_MAX_BONUS: float = 0.5       # 最大奖励→乘子上限 1.5

    # ========== 决策模式（fusion_v2 升级开关，2026-08-10）==========
    # legacy    ：旧架构，第三票方向由单个 Chronos（meta_quality.chronos_dir）提供。
    # fusion_v2 ：第四票架构，把"第三票方向"升级为 4 时序模型融合票
    #            （Chronos-2/TimesFM/Time-MoE/Moirai 聚合），其余裁决逻辑全复用。
    #            ★ 全账号统一生效（多客户并行，不写死账号数）。一键回退改此值即可。
    DECISION_MODE: str = "fusion_v2"
    # 时序融合票作为第四票的权重（低于单云模型、与 Chronos 同级，避免喧宾夺主但提供方向锚定）
    TS_FUSION_VOTE_WEIGHT: float = 0.22
    # ★ 2026-08-18 第四处修复C：融合票命中率地板。命中率低于此值的融合票不具备方向投票资格
    # ★ 2026-08-18 第五处修复B：趋势背书加成。趋势明确(强跌/跌/强涨/涨)且云方向模型
    #   与趋势同向、另一模型沉默(HOLD)时，R2 给顺势单额外加成——趋势本身即方向权威，
    #   短周期反弹噪音不应把顺势单压到死锁(用户铁律:趋势明确多开顺势单赚钱)。仅顺势生效。
    TREND_BACKING_BONUS: float = 0.10
    #   （以 86% 置信投 BUY 却历史命中≈0% 是毒信号，会反向压死顺势单）→ 降级 NEUTRAL 回退单 Chronos。
    TS_FUSION_HIT_FLOOR: float = 0.45
    # ★ 2026-08-19 审计P1落地：同源冗余票降权（相关性去冗余）。
    #   4 时序模型吃同一 M15 收盘价序列→方向强相关，弱副本票与锚 Chronos 同向但命中率更低时，
    #   质量权重 × 此折扣，防"4 票"实为"1 票的 4 个近似副本"、重复暴露同一因子。
    TS_FUSION_REDUNDANCY_DISCOUNT: float = 0.50
    # ★ 2026-08-19 定稿P0-1：Chronos 单锚化（竞技场实证集成 10.6 净点 < 单 Chronos 319.4，
    #   弱信号叠加无互补效应）。True=非锚模型(TimesFM/Time-MoE/Moirai) qw=0 完全观测化，
    #   融合票方向 100% 由 Chronos 决定，参考面板保留 4 模型展示；False=回退 4 模型加权融合。
    TS_FUSION_SINGLE_ANCHOR: bool = True

    # ========== 交易参数 ==========
    SYMBOL: str = "XAUUSD"
    ALLOWED_SYMBOLS: str = "XAUUSD"

    # ========== 进场价位对齐（根治「AI 想在 4329 开空、执行却在 4315 市价开」）==========
    # 设计（提准非拦截·加法·对齐用户 2026-08-14 复盘铁证）：
    #   云端双脑本就会在 JSON 里回传 entry_price（DeepSeek 一直有；混元已补），且常在
    #   reasoning 里写「待反弹至 4322-4329 做空」。但执行层从前只读方向+置信，用市价单
    #   在「当前 bid/ask」直接开，把 AI 的价位指引彻底丢弃 → 山底/山顶追单。
    #   本开关：当 AI 给出的目标入场价明显优于当前价（SELL 想更高、BUY 想更低）且落在
    #   「可达区间」内时，执行层【推迟市价开仓】，等价格回到 AI 想要的 zone 再点火
    #   （复用现有 100% 可靠的市价单路径，不引入任何新订单类型，零新增 MT5 风险）。
    #   若价格迟迟不到 zone → TTL 到期自动放弃该笔（宁可不追，不追在山底/山顶）。
    #   这是「提准」不是「拦截」：交易仍在条件满足时发生，只是进场更聪明、盈亏比更好。
    #   默认开启；任意参数都可一键关回（全账号统一生效）。
    ENTRY_ZONE_DEFER_ENABLED: bool = True
    # ★ 2026-08-17 P0 修复（海外调研：nof1.ai Core Policy / MQL5 用 ATR 相对阈值而非固定点数）：
    #   MIN_DIST 原固定 3.0 点 → 实盘事故：AI 目标 4396（等回踩再空）vs 市价 4393.63 差 2.37 点
    #   < 3.0 → 判定「不值得等」→ 市价追空被套 -228，而市场随后反弹到 AI 目标位（AI 判断正确）。
    #   改为 ATR 自适应：min_dist = max(绝对下限, MIN_DIST_ATR_MULT × ATR)。
    #   XAUUSD ATR14≈14 → min_dist≈2.1 点，2.37>2.1 → 该笔会正确等待（不再机械否决 AI 价位意愿）；
    #   低波动时下限 2.0 点仍过滤噪声（防目标太近频繁等待）。这是「提准」：更尊重 AI 的入场时机，
    #   不减交易笔数（差距真的太小仍市价开，MAX_ATR_MULT 上限仍防追不到）。
    ENTRY_ZONE_MIN_DIST: float = 3.0        # （保留字段名，实际生效值=自适应，见 _maybe_defer_entry）
    ENTRY_ZONE_MIN_DIST_ATR_MULT: float = 0.15  # 最小等待距离 = 0.15×ATR（ATR=14→2.1点）
    ENTRY_ZONE_MIN_DIST_ABS: float = 2.0    # 绝对下限（点）：低于此视为噪声不值得等
    ENTRY_ZONE_MAX_ATR_MULT: float = 1.5    # 目标入场价距当前价超过此×ATR 视为「太远追不到」→ 退回市价（防漏单腰斩交易数）
    ENTRY_ZONE_CHASE_SCALE_ENABLED: bool = True   # 当价格已跑出「值得等」区间时，是否按追价程度缩仓（而非全仓市价追单）
    ENTRY_ZONE_CHASE_SCALE_FLOOR: float = 0.2       # 追价缩仓下限（最小保留 20% 手数），避免单笔追成重仓
    ENTRY_ZONE_TTL_MIN: int = 30            # 推迟入场意图存活时长（分钟），到期未触发则放弃重判
    ENTRY_ZONE_FILL_TOL: float = 7.0        # 价格回到 zone 的触发容差（点）：SELL 当前买价≥目标-容差即点火（覆盖 AI 所给区间的下沿，进 zone 即点火）
    ENTRY_ZONE_PARSE_REASONING: bool = True # 当结构化 entry_price 缺失/≈当前价时，回退解析 reasoning 文本里的「反弹/回踩至 X(-Y)」价位

    # ========== AI 自主仓位管理（Position Manager · 纯加法增强层 · 2026-08-14）==========
    # 用户授权（「可以，开干」）：让 AI 大脑「按行情自主管理仓位」——
    #   ① 确定性「利润走不动」机械平仓层（亚秒/每周期抓利润停滞·不依赖大模型·最快最稳）
    #   ② 本地 qwen3:8b 高频管仓（零 token·多周期 M5/M3 微观特征，判断「开错单最小亏损平」「追踪锁利」）
    # 设计铁律：提准非拦截、零新 MT5 订单类型、不砍交易笔数、硬 SL 永不在 AI 手里移除。
    #   复用现有红线：亏损单保护(_with_trend 闸门)、硬地板(_merge_hard_floor_sl)、浮盈回吐锁利(peak_move)、
    #   L2 反转防抖(_REVERSAL_STATE)、防重复减半(_PARTIAL_DONE)。
    #   一键回退：POSITION_MANAGER_ENABLED=False 即整层失效（原有 M1 云端 + 规则引擎完全不动）。
    # ★ 全账号统一生效（多客户并行，不写死账号数）。
    POSITION_MANAGER_ENABLED: bool = True
    # 本地 8B 管仓调用开关（确定性「利润走不动」层始终生效；此开关仅控制是否叠加本地大模型判断）
    POSITION_MANAGER_LOCAL_ENABLED: bool = True
    # 管理调用节流：持有时每笔持仓最多每 N 秒请求一次本地 8B（防刷屏 + 省显存）
    POSITION_MANAGER_CALL_INTERVAL: float = 15.0
    # ── ① 确定性停滞平仓：盈利单在 M5 窄幅震荡带内「利润走不动」≥ N 根 → 机械全平 ──
    #   逻辑：利润与行情成正比，行情走不动(窄幅+未创新高)就立刻平仓，腾笼等下一个开仓信号。
    PM_STALL_ATR_MULT: float = 0.6          # 利润停滞判定带：M5 连续 N 根高低波幅 < ATR×此值 = 行情走不动
    PM_STALL_BARS: int = 3                  # 连续停滞 K 线根数（M5）
    PM_STALL_MIN_HOLD_SEC: int = 90         # 持仓至少这么久才允许停滞平仓（避免刚开就平）
    PM_STALL_PEAK_DROP: float = 0.95        # 当前利润 < 近期峰值×此值才视为「未创新高在耗着」
    # ── ② 最小亏损平仓（开错单）：须 M5 反转确认 + 浮亏超过硬 SL 的此比例 才允许提前平（防误砍顺势回调）──
    PM_MIN_LOSS_HARD_SL_PCT: float = 0.40   # 浮亏 > 硬 SL×此比例 才达「亏得明显」门槛
    PM_MIN_LOSS_M5_RSI: float = 45.0         # M5 RSI 跌破此值(BUY)/升破(100-此值)(SELL) = 反转动能确认
    PM_MIN_LOSS_M5_EMA_BREAK: bool = True    # 要求价格跌破(BUY)/升破(SELL) M5 EMA20 = 结构破位
    PM_MIN_LOSS_LOCAL_CONF: float = 0.45     # 本地 8B 也须判 FULL_MIN_LOSS 且置信≥此值（双确认；本地不可用则仅确定性门槛）
    PM_MIN_LOSS_MIN_HOLD_SEC: int = 60        # 持仓至少这么久才允许最小亏损平（避免瞬时抖动误杀）
    # ── 追踪锁利（本地 8B 给 TRAIL_TIGHTEN 时）：新 SL 距市价至少 ATR×此值，保证留呼吸空间 ──
    PM_TRAIL_MIN_ATR_MULT: float = 0.3

    # ========== 前端 ==========
    FRONTEND_DIST_DIR: str = "dist"  # 前端构建产物目录名（dist / dist_v9 等）

    # ========== 授权与激活（V6 Phase 8）==========
    # 是否强制授权校验。开发机/内部演示机可置 False 完全旁路。
    # ★ 客户打包必须为 True，否则整套商业授权形同虚设。
    LICENSE_ENFORCE: bool = True
    # 心跳服务端。留空 = 纯离线模式（令牌本地验签即可，不联网也能跑）。
    # 心跳只用于「续期下发」和「吊销通知」，绝不作为开仓前置条件——
    # 否则我们的服务器一挂，全体客户停摆。
    LICENSE_SERVER_URL: str = ""
    LICENSE_HEARTBEAT_MINUTES: int = 360  # 6 小时一次，足够及时且不打扰

    # ========== 商业功能（旧订阅位，逐步由上面的授权体系接管）==========
    SUBSCRIPTION_ENABLED: bool = False
    FREE_MAX_ACCOUNTS: int = 1
    PRO_MAX_ACCOUNTS: int = 6
    ENTERPRISE_MAX_ACCOUNTS: int = 999

    class Config:
        env_file = str(BASE_DIR / ".env")
        env_file_encoding = "utf-8"
        extra = "allow"


settings = Settings()
