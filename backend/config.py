"""插件配置：读取与规范化。

配置本身由主程序管理（`config_schema` → `ui_data._plugins_v1.<id>.config`），
这里只做「默认值兜底 + 类型/区间收敛」，避免坏配置把作业带进不可预期状态。
"""

import re

from .core import modelcap as modelcap_core
from .core import selector as selector_core
from .core import tagpool as tagpool_core

DEFAULT_CONFIG = {
    'default_profile_id': '',
    'max_tags': 8,
    'summary_max_chars': 300,
    # 全局「标签置信度下限」已删除：门槛改为按分类配置（见 tag_category_rules）。
    # 未单独设置的分类用 tagpool.DEFAULT_CATEGORY_CONFIDENCE（0.5）。
    'batch_size': 5,
    'concurrency': 1,
    'apply_mode': 'review',
    'apply_tags': True,
    'skip_unchanged': True,
    'validation_strict': False,
    'wi_excerpt_ratio': 0.3,
    'wi_keywords': '世界观,设定,背景,叙事要求,元数据,规则',
    # ---- 标签池的分类白/黑名单 ----
    # 存成两个**已声明**的字符串键：宿主的 `api.config.set()` 只接受
    # `config_schema` 里声明过的键，未声明的写入会被**拒绝**（实测返回 False），
    # 所以不能只写 `tag_pool` 这个 dict。
    'tag_pool_categories_include': '',
    'tag_pool_categories_exclude': '',
    # 每分类规则（JSON 字符串：{"分类": {"confidence_min": 0.6, "max_tags": 3}}）。
    # 同样必须是已声明的键；设置页负责读写，一般不用手填。
    'tag_category_rules': '',
    # 标签池 token 预算（给 AI 列多少标签）；实际值会按模型上下文夹一次。
    'tag_pool_budget_tokens': 1200,
    # ---- 模型能力（用户可调；默认按当前主流大模型给）----
    'context_length': modelcap_core.DEFAULT_CONTEXT_LENGTH,
    'max_output_tokens': modelcap_core.DEFAULT_MAX_OUTPUT_TOKENS,
    'reasoning_enabled': True,
    'reasoning_effort': modelcap_core.DEFAULT_REASONING_EFFORT,
    'price_per_1k_prompt': 0.0,
    'price_per_1k_completion': 0.0,
    'currency': 'CNY',
}

DEFAULT_WI_KEYWORDS = ('世界观', '设定', '背景', '叙事要求', '元数据', '规则')

APPLY_MODES = ('auto', 'review')


def _to_int(value, default, *, minimum=None, maximum=None):
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def _to_float(value, default, *, minimum=None, maximum=None):
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


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


def _to_text(value, default=''):
    if value is None:
        return default
    return str(value)


#: 分类名单的分隔符：中英文逗号 / 分号 / 顿号 / 换行（非程序员不会只打英文逗号）。
_CATEGORY_SPLIT_PATTERN = re.compile(r'[,，;；、\n\r]+')


def _to_category_list(value) -> list:
    """把「只取 / 排除这些分类」的文本解析成分类名列表（去空、去重、保序）。"""
    if isinstance(value, (list, tuple, set)):
        parts = [str(item or '') for item in value]
    else:
        parts = _CATEGORY_SPLIT_PATTERN.split(_to_text(value, ''))
    result = []
    for part in parts:
        name = str(part or '').strip()
        if name and name not in result and len(name) <= 64:
            result.append(name)
    return result[:50]


def normalize_config(raw) -> dict:
    """把任意配置收敛为合法结构（未知键保留，坏值回落默认）。"""
    data = raw if isinstance(raw, dict) else {}

    apply_mode = _to_text(data.get('apply_mode'), DEFAULT_CONFIG['apply_mode']).strip().lower()
    if apply_mode not in APPLY_MODES:
        apply_mode = DEFAULT_CONFIG['apply_mode']

    caps = modelcap_core.normalize_capabilities(data)

    # 标签池：分类白/黑名单来自两个**已声明**的字符串键（见 DEFAULT_CONFIG 注释），
    # 其余字段仍由 `tag_pool`（内部结构）或内置默认提供。
    tag_pool = tagpool_core.normalize_pool(data.get('tag_pool'))
    tag_pool['categories_include'] = _to_category_list(
        data.get('tag_pool_categories_include'),
    )
    tag_pool['categories_exclude'] = _to_category_list(
        data.get('tag_pool_categories_exclude'),
    )

    # 每分类规则：存成 JSON 字符串（宿主只接受已声明的键），这里折成 dict。
    tag_rules = tagpool_core.normalize_rules(data.get('tag_category_rules'))

    # 标签池 token 预算：决定「给 AI 列多少标签」。全局默认 1200；档案自己的
    # tag_pool.budget_tokens 仍可覆盖它。最后按模型上下文夹一次，避免预算超过窗口。
    budget = _to_int(
        data.get('tag_pool_budget_tokens'), tag_pool.get('budget_tokens'),
        minimum=200, maximum=20000,
    )
    tag_pool['budget_tokens'] = min(budget, max(200, modelcap_core.context_available(caps)))

    return {
        'default_profile_id': _to_text(data.get('default_profile_id'), '').strip(),
        'max_tags': _to_int(data.get('max_tags'), DEFAULT_CONFIG['max_tags'], minimum=1, maximum=20),
        'summary_max_chars': _to_int(data.get('summary_max_chars'),
                                     DEFAULT_CONFIG['summary_max_chars'], minimum=50, maximum=2000),
        'tag_category_rules': tag_rules,
        'batch_size': _to_int(data.get('batch_size'), DEFAULT_CONFIG['batch_size'], minimum=1, maximum=50),
        'concurrency': _to_int(data.get('concurrency'), DEFAULT_CONFIG['concurrency'], minimum=1, maximum=8),
        'apply_mode': apply_mode,
        'apply_tags': _to_bool(data.get('apply_tags'), DEFAULT_CONFIG['apply_tags']),
        'skip_unchanged': _to_bool(data.get('skip_unchanged'), DEFAULT_CONFIG['skip_unchanged']),
        'validation_strict': _to_bool(data.get('validation_strict'), DEFAULT_CONFIG['validation_strict']),
        'wi_excerpt_ratio': _to_float(data.get('wi_excerpt_ratio'),
                                      DEFAULT_CONFIG['wi_excerpt_ratio'], minimum=0.0, maximum=1.0),
        'wi_keywords': _to_text(data.get('wi_keywords'), DEFAULT_CONFIG['wi_keywords']),
        # ---- 模型能力：context_length / max_output_tokens / reasoning_* ----
        **caps,
        'price_per_1k_prompt': _to_float(data.get('price_per_1k_prompt'), 0.0, minimum=0.0),
        'price_per_1k_completion': _to_float(data.get('price_per_1k_completion'), 0.0, minimum=0.0),
        'currency': _to_text(data.get('currency'), DEFAULT_CONFIG['currency']).strip() or 'CNY',
        # 新档案的初值；档案自己的 content_selector / tag_pool 会覆盖它（档案优先）
        'content_selector': selector_core.normalize_selector(data.get('content_selector')),
        'tag_pool': tag_pool,
    }


def load_config(api) -> dict:
    """从主程序读取并规范化配置。"""
    try:
        raw = api.config.get()
    except Exception:  # noqa: BLE001 - 配置读取失败不应让插件不可用
        raw = {}
    return normalize_config(raw)


def wi_keywords(config) -> list:
    """把逗号分隔的关键词配置解析为列表；为空时回落默认表。"""
    text = _to_text((config or {}).get('wi_keywords'), '')
    parts = [part.strip() for part in text.replace('，', ',').split(',') if part.strip()]
    return parts or list(DEFAULT_WI_KEYWORDS)
