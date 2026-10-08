"""插件配置：读取与规范化。

配置本身由主程序管理（`config_schema` → `ui_data._plugins_v1.<id>.config`），
这里只做「默认值兜底 + 类型/区间收敛」，避免坏配置把作业带进不可预期状态。
"""

DEFAULT_CONFIG = {
    'default_profile_id': '',
    'max_tags': 8,
    'summary_max_chars': 300,
    'confidence_min': 0.5,
    'batch_size': 5,
    'concurrency': 1,
    'apply_mode': 'review',
    'apply_tags': True,
    'skip_unchanged': True,
    'validation_strict': False,
    'wi_excerpt_ratio': 0.3,
    'wi_keywords': '世界观,设定,背景,叙事要求,元数据,规则',
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


def normalize_config(raw) -> dict:
    """把任意配置收敛为合法结构（未知键保留，坏值回落默认）。"""
    data = raw if isinstance(raw, dict) else {}

    apply_mode = _to_text(data.get('apply_mode'), DEFAULT_CONFIG['apply_mode']).strip().lower()
    if apply_mode not in APPLY_MODES:
        apply_mode = DEFAULT_CONFIG['apply_mode']

    return {
        'default_profile_id': _to_text(data.get('default_profile_id'), '').strip(),
        'max_tags': _to_int(data.get('max_tags'), DEFAULT_CONFIG['max_tags'], minimum=1, maximum=20),
        'summary_max_chars': _to_int(data.get('summary_max_chars'),
                                     DEFAULT_CONFIG['summary_max_chars'], minimum=50, maximum=2000),
        'confidence_min': _to_float(data.get('confidence_min'),
                                    DEFAULT_CONFIG['confidence_min'], minimum=0.0, maximum=1.0),
        'batch_size': _to_int(data.get('batch_size'), DEFAULT_CONFIG['batch_size'], minimum=1, maximum=50),
        'concurrency': _to_int(data.get('concurrency'), DEFAULT_CONFIG['concurrency'], minimum=1, maximum=8),
        'apply_mode': apply_mode,
        'apply_tags': _to_bool(data.get('apply_tags'), DEFAULT_CONFIG['apply_tags']),
        'skip_unchanged': _to_bool(data.get('skip_unchanged'), DEFAULT_CONFIG['skip_unchanged']),
        'validation_strict': _to_bool(data.get('validation_strict'), DEFAULT_CONFIG['validation_strict']),
        'wi_excerpt_ratio': _to_float(data.get('wi_excerpt_ratio'),
                                      DEFAULT_CONFIG['wi_excerpt_ratio'], minimum=0.0, maximum=1.0),
        'wi_keywords': _to_text(data.get('wi_keywords'), DEFAULT_CONFIG['wi_keywords']),
        'price_per_1k_prompt': _to_float(data.get('price_per_1k_prompt'), 0.0, minimum=0.0),
        'price_per_1k_completion': _to_float(data.get('price_per_1k_completion'), 0.0, minimum=0.0),
        'currency': _to_text(data.get('currency'), DEFAULT_CONFIG['currency']).strip() or 'CNY',
    }


def load_config(api) -> dict:
    """从主程序读取并规范化配置。"""
    try:
        raw = api.get_config()
    except Exception:  # noqa: BLE001 - 配置读取失败不应让插件不可用
        raw = {}
    return normalize_config(raw)


def wi_keywords(config) -> list:
    """把逗号分隔的关键词配置解析为列表；为空时回落默认表。"""
    text = _to_text((config or {}).get('wi_keywords'), '')
    parts = [part.strip() for part in text.replace('，', ',').split(',') if part.strip()]
    return parts or list(DEFAULT_WI_KEYWORDS)
