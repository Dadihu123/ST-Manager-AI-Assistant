"""模型能力：上下文长度、输出长度、推理开关与思考等级的解析。

这一层只做纯计算，不读写配置也不发请求：

- 配置里的四个键（`context_length` / `max_output_tokens` / `reasoning_enabled` /
  `reasoning_effort`）统一收敛成一份 `caps`；
- `resolve_max_tokens()` 决定一次调用真正发出去的 `max_tokens`：档案显式值优先，
  但不超过全局「输出长度上限」；档案写 `0`（或留空）= 跟随全局；
- `resolve_extra_body()` 把推理设置并进 `extra_body`，**不覆盖档案显式声明**；
- `resolve_prompt_budget()` 保证任何提示词预算都装得进上下文；
- `resolve_agent_budget()` 在 Agent 作业里应用「按库规模自适应轮次」与上下文预算。

为什么不把默认值写在各调用点：模型能力是一个**用户可调**的全局口径，
散落在 6 个 call site 里迟早漂移（`max_tokens=2048` 截断 finalize JSON 就是这么来的）。
"""

# 协议名在这里只用来决定「要不要注入 reasoning_effort」。**故意不 import
# `backend.protocols`**：`backend/core` 会被主程序（以及测试）当作独立包加载，
# core 模块不能反向依赖 backend 根目录的模块。
_OPENAI_CHAT = 'openai_chat'
_OPENAI_RESPONSES = 'openai_responses'
_KNOWN_PROTOCOLS = (_OPENAI_CHAT, _OPENAI_RESPONSES, 'anthropic_messages')

#: 默认上下文长度：256K（用户可在设置里改）
DEFAULT_CONTEXT_LENGTH = 262144
#: 默认输出长度上限：32K
DEFAULT_MAX_OUTPUT_TOKENS = 32768

MIN_CONTEXT_LENGTH = 8192
MAX_CONTEXT_LENGTH = 4000000
MIN_OUTPUT_TOKENS = 256
MAX_OUTPUT_TOKENS = 262144

#: 思考等级：`auto` = 不显式传，用供应商默认
REASONING_EFFORTS = ('auto', 'low', 'medium', 'high')
DEFAULT_REASONING_EFFORT = 'auto'

#: 只有 OpenAI 风格的两种协议接受 `reasoning_effort`；
#: Anthropic 的思考走 `thinking` 字段（语义不同），不在这里猜。
REASONING_PROTOCOLS = (_OPENAI_CHAT, _OPENAI_RESPONSES)

DEFAULT_PROMPT_BUDGET = 8000
MIN_PROMPT_BUDGET = 1024
#: 给系统提示、用户模板、输出留的安全余量
CONTEXT_RESERVE_TOKENS = 4096


def _to_int(value, default, *, minimum, maximum):
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _to_bool(value, default):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ('1', 'true', 'yes', 'on'):
            return True
        if text in ('0', 'false', 'no', 'off', ''):
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def normalize_capabilities(raw) -> dict:
    """把配置（或任意 dict）收敛成一份模型能力。"""
    data = raw if isinstance(raw, dict) else {}

    context_length = _to_int(
        data.get('context_length'), DEFAULT_CONTEXT_LENGTH,
        minimum=MIN_CONTEXT_LENGTH, maximum=MAX_CONTEXT_LENGTH,
    )
    max_output_tokens = _to_int(
        data.get('max_output_tokens'), DEFAULT_MAX_OUTPUT_TOKENS,
        minimum=MIN_OUTPUT_TOKENS, maximum=MAX_OUTPUT_TOKENS,
    )
    # 输出不可能超过上下文：用户把上下文调到 8K 时，32K 的输出上限要跟着收
    max_output_tokens = min(max_output_tokens, context_length)

    effort = str(data.get('reasoning_effort') or DEFAULT_REASONING_EFFORT).strip().lower()
    if effort not in REASONING_EFFORTS:
        effort = DEFAULT_REASONING_EFFORT

    return {
        'context_length': context_length,
        'max_output_tokens': max_output_tokens,
        'reasoning_enabled': _to_bool(data.get('reasoning_enabled'), True),
        'reasoning_effort': effort,
    }


def _caps(value) -> dict:
    """接受「完整配置」或「已经是 caps 的 dict」，统一过一遍归一化（幂等）。"""
    return normalize_capabilities(value)


def resolve_max_tokens(profile, config_or_caps) -> int:
    """一次调用真正使用的 `max_tokens`。

    档案显式值优先，但不超过全局输出上限；`0` / 负数 / 非数字 = 跟随全局。
    """
    caps = _caps(config_or_caps)
    raw_value = profile.get('max_tokens') if isinstance(profile, dict) else None
    try:
        value = int(raw_value or 0)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        value = caps['max_output_tokens']
    return max(MIN_OUTPUT_TOKENS, min(value, caps['max_output_tokens']))


def resolve_extra_body(profile, config_or_caps, protocol='') -> dict:
    """把推理设置并进 `extra_body`。

    档案自己写了 `reasoning_effort` 就完全尊重档案（例如「关闭思考」的测试档案）；
    否则按全局设置注入：关推理 → `none`，思考等级非 auto → 对应等级。
    Anthropic 协议不注入（它的思考是 `thinking`，不是 `reasoning_effort`）。
    """
    caps = _caps(config_or_caps)
    body = dict((profile or {}).get('extra_body') or {}) if isinstance(profile, dict) else {}

    if 'reasoning_effort' in body:
        return body

    text = str(protocol or '').strip().lower()
    target = text if text in _KNOWN_PROTOCOLS else _OPENAI_CHAT
    if target not in REASONING_PROTOCOLS:
        return body

    if not caps['reasoning_enabled']:
        body['reasoning_effort'] = 'none'
    elif caps['reasoning_effort'] != 'auto':
        body['reasoning_effort'] = caps['reasoning_effort']
    return body


def context_available(caps_or_config) -> int:
    """一次请求里「提示词可用的 token」上限。"""
    caps = _caps(caps_or_config)
    return max(
        MIN_PROMPT_BUDGET,
        caps['context_length'] - caps['max_output_tokens'] - CONTEXT_RESERVE_TOKENS,
    )


def resolve_prompt_budget(config_or_caps, requested=0, *,
                          default=DEFAULT_PROMPT_BUDGET) -> int:
    """把提示词/世界书预算夹到上下文里（默认值不受影响，只有超限才收）。"""
    caps = _caps(config_or_caps)
    try:
        base = int(requested or default)
    except (TypeError, ValueError):
        base = default
    base = max(MIN_PROMPT_BUDGET, base)
    return min(base, context_available(caps))


def adaptive_iterations(card_total) -> int:
    """按库规模给 Agent 轮次：`max(4, min(12, ceil(卡数/6)))`。"""
    try:
        total = max(1, int(card_total or 1))
    except (TypeError, ValueError):
        total = 1
    return max(4, min(12, (total + 5) // 6))


def resolve_agent_budget(raw_budget, card_total, config_or_caps) -> dict:
    """Agent 作业的预算：显式值优先，缺省值按库规模与模型能力算。"""
    from . import agent as agent_core  # noqa: PLC0415 - 避免模块级循环导入

    raw = raw_budget if isinstance(raw_budget, dict) else {}
    budget = agent_core.normalize_budget(raw)
    caps = _caps(config_or_caps)

    if raw.get('max_iterations') is None:
        budget['max_iterations'] = adaptive_iterations(card_total)
    if raw.get('context_budget') is None:
        budget['context_budget'] = max(
            4000, caps['context_length'] - caps['max_output_tokens'] - 8192,
        )

    # 单次读批与目录预算也不能超过提示词可用额度（上下文调小的时候尤其重要）
    available = context_available(caps)
    if raw.get('catalog_tokens') is None:
        budget['catalog_tokens'] = min(budget['catalog_tokens'], available)
    if raw.get('read_batch_tokens') is None:
        budget['read_batch_tokens'] = min(budget['read_batch_tokens'], available)
    return budget


__all__ = [
    'DEFAULT_CONTEXT_LENGTH', 'DEFAULT_MAX_OUTPUT_TOKENS', 'REASONING_EFFORTS',
    'normalize_capabilities', 'resolve_max_tokens', 'resolve_extra_body',
    'resolve_prompt_budget', 'context_available', 'adaptive_iterations',
    'resolve_agent_budget',
]
