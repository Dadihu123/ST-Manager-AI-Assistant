"""内容选择器：决定「哪些卡片信息真的进提示词」。

纯函数层，无 flask、无主程序依赖。

**为什么需要独立的选择器**（而不是靠模板写不写变量）：

- 模板负责**位置与组织**（怎么摆、放哪段）；
- 选择器负责**取值**（给不给、给几条）。

两者分离后，同一份模板可以配不同选择器：分析概览时不要正则脚本（太脏太长），
分析「这张卡能不能跑」时才要。也避免了「用户以为没写变量就没发出去，
其实模板里某段早就引用了」。

选择器只**收敛**取值，从不新增：未勾选的字段在变量表里是**空串**，
因此模板里写了 `{{ card.description }}` 也不会报 missing（键存在即视为已提供）。
唯一的例外是 `card.name` —— 它是卡片身份，无论如何都发。
"""

import copy

#: 卡片正文字段（可在选择器里勾选）
SELECTABLE_CARD_FIELDS = (
    'description',
    'personality',
    'scenario',
    'first_mes',
    'mes_example',
    'creator',
    'creator_notes',
    'system_prompt',
    'post_history_instructions',
    'tags',
)

#: 永远发送、不可关闭的字段（卡片身份）
ALWAYS_INCLUDED_FIELDS = ('name',)

#: 永远可用、不受选择器控制的「元数据」变量
#: —— 它们不是卡片正文，也不产生 token 负担，因此不该被算作「白勾」。
ALWAYS_AVAILABLE_VARIABLES = (
    'card.alternate_greetings_count',
    'worldbook.count',
    'worldbook.total_tokens',
    'worldbook.excerpt_tokens',
    'worldbook.truncated',
)

#: 附加内容（默认全关：它们通常很长或很脏）
EXTRA_REGEX = 'regex'
EXTRA_ST_SCRIPTS = 'st_scripts'
EXTRA_LOCAL_NOTE = 'local_note'
EXTRAS = (EXTRA_REGEX, EXTRA_ST_SCRIPTS, EXTRA_LOCAL_NOTE)

#: 备用开场白的取法
GREETINGS_NONE = 'none'
GREETINGS_ALL = 'all'
GREETINGS_SELECTED = 'selected'
GREETINGS_MODES = (GREETINGS_NONE, GREETINGS_ALL, GREETINGS_SELECTED)

#: 世界书的取法
WI_NONE = 'none'
WI_AUTO = 'auto'          # 按 token 预算 + 价值排序自动取样（历史行为）
WI_SELECTED = 'selected'  # 只发勾选的条目（预算内仍可自动补）
WI_MODES = (WI_NONE, WI_AUTO, WI_SELECTED)

MAX_GREETING_INDEXES = 50


def default_selector() -> dict:
    """默认选择器：等同改造前的行为（全部正文字段、世界书自动、附加全关）。"""
    return {
        'card_fields': list(SELECTABLE_CARD_FIELDS),
        'alternate_greetings': {'mode': GREETINGS_NONE, 'indexes': []},
        'extras': [],
        'worldbook': {'mode': WI_AUTO, 'entry_uids': [], 'budget_tokens': 0},
    }


def _to_bool_list(values, allowed, default):
    """保序去重地筛出 allowed 内的值。"""
    if not isinstance(values, (list, tuple, set)):
        return list(default)
    allowed_set = set(allowed)
    result = []
    for item in values:
        text = str(item or '').strip()
        if text and text in allowed_set and text not in result:
            result.append(text)
    return result


def _to_indexes(values) -> list:
    if not isinstance(values, (list, tuple, set)):
        return []
    result = []
    for item in values:
        if isinstance(item, bool):
            continue
        try:
            index = int(item)
        except (TypeError, ValueError):
            continue
        if index < 0 or index in result:
            continue
        result.append(index)
    return sorted(result)[:MAX_GREETING_INDEXES]


def _to_uids(values) -> list:
    if not isinstance(values, (list, tuple, set)):
        return []
    result = []
    for item in values:
        text = str(item or '').strip()
        if text and text not in result:
            result.append(text)
    return result


def normalize_selector(raw) -> dict:
    """把任意输入收敛为合法选择器（坏值回落默认，不抛错）。"""
    data = raw if isinstance(raw, dict) else {}
    base = default_selector()

    fields = data.get('card_fields')
    if isinstance(fields, (list, tuple, set)):
        # 显式给空列表 = 只要 name（合法用法，不回落默认）
        selected = _to_bool_list(fields, SELECTABLE_CARD_FIELDS, [])
        card_fields = [name for name in SELECTABLE_CARD_FIELDS if name in selected]
    else:
        card_fields = base['card_fields']

    greetings_raw = data.get('alternate_greetings')
    greetings = greetings_raw if isinstance(greetings_raw, dict) else {}
    greetings_mode = str(greetings.get('mode') or GREETINGS_NONE).strip().lower()
    if greetings_mode not in GREETINGS_MODES:
        greetings_mode = GREETINGS_NONE

    worldbook_raw = data.get('worldbook')
    worldbook = worldbook_raw if isinstance(worldbook_raw, dict) else {}
    wi_mode = str(worldbook.get('mode') or WI_AUTO).strip().lower()
    if wi_mode not in WI_MODES:
        wi_mode = WI_AUTO

    budget = worldbook.get('budget_tokens')
    try:
        budget_tokens = max(0, int(budget or 0))
    except (TypeError, ValueError):
        budget_tokens = 0

    return {
        'card_fields': card_fields,
        'alternate_greetings': {
            'mode': greetings_mode,
            'indexes': _to_indexes(greetings.get('indexes')),
        },
        'extras': _to_bool_list(data.get('extras'), EXTRAS, []),
        'worldbook': {
            'mode': wi_mode,
            'entry_uids': _to_uids(worldbook.get('entry_uids')),
            'budget_tokens': budget_tokens,
        },
    }


def merge_selector(defaults, override) -> dict:
    """把档案里的选择器合并到全局默认上（档案优先，但只覆盖它显式给的键）。"""
    base = copy.deepcopy(normalize_selector(defaults))
    layer = override if isinstance(override, dict) else {}
    if not layer:
        return base
    merged = dict(base)
    merged.update(layer)
    return normalize_selector(merged)


def field_enabled(selector, field_name: str) -> bool:
    """该字段是否被选择器放行。"""
    if field_name in ALWAYS_INCLUDED_FIELDS:
        return True
    spec = normalize_selector(selector)
    return field_name in spec['card_fields']


def wants_extra(selector, name: str) -> bool:
    return name in normalize_selector(selector)['extras']


def worldbook_mode(selector) -> str:
    return normalize_selector(selector)['worldbook']['mode']


def worldbook_entry_uids(selector) -> list:
    return list(normalize_selector(selector)['worldbook']['entry_uids'])


def describe_selector(selector) -> dict:
    """给 UI 与预览用的摘要（人类可读，不参与渲染）。"""
    spec = normalize_selector(selector)

    disabled = [name for name in SELECTABLE_CARD_FIELDS if name not in spec['card_fields']]

    greetings = spec['alternate_greetings']
    if greetings['mode'] == GREETINGS_ALL:
        greetings_text = '全部'
    elif greetings['mode'] == GREETINGS_SELECTED:
        greetings_text = f'第 {greetings["indexes"]} 条' if greetings['indexes'] else '（未选）'
    else:
        greetings_text = '不发'

    label = {
        WI_NONE: '不发',
        WI_AUTO: '按预算自动取样',
        WI_SELECTED: f'指定 {len(spec["worldbook"]["entry_uids"])} 条',
    }[spec['worldbook']['mode']]

    return {
        'disabled_fields': disabled,
        'alternate_greetings': greetings_text,
        'extras': list(spec['extras']),
        'worldbook': label,
    }
