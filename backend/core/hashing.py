"""内容哈希：幂等判定的唯一依据。

设计文档 §6.5：
    content_hash = sha256(卡片关键字段 + 世界书条目名集合)
字段顺序固定、世界书条目名先排序，保证同一张卡在同一状态下永远得到同一个哈希。
"""

import hashlib

HASH_PREFIX = 'sha256:'

# 参与哈希的卡片字段（顺序即拼接顺序，不可随意调整）
CARD_HASH_FIELDS = (
    'name',
    'description',
    'first_mes',
    'personality',
    'scenario',
    'mes_example',
)

_SEPARATOR = '\n\x1f\n'


def normalize_text(value) -> str:
    """归一化文本：None → 空串、统一换行、去掉首尾空白。"""
    if value is None:
        return ''
    if not isinstance(value, str):
        value = str(value)
    return value.replace('\r\n', '\n').replace('\r', '\n').strip()


def card_data_block(card) -> dict:
    """取出卡片的数据块。

    `api.cards.read()` 的返回兼容 V2（字段在顶层）与 V3（字段在 `data` 下）
    两种结构（主程序 `core/plugins/api.py:437-442` 也是这么处理的）。
    """
    if not isinstance(card, dict):
        return {}
    block = card.get('data')
    if isinstance(block, dict):
        merged = dict(card)
        merged.update(block)
        return merged
    return dict(card)


def card_fields(card) -> dict:
    """抽取参与哈希的字段（缺失字段按空串处理）。"""
    block = card_data_block(card)
    return {key: normalize_text(block.get(key)) for key in CARD_HASH_FIELDS}


def worldbook_entry_names(card) -> list:
    """从卡片内嵌世界书里取出条目名集合（用于哈希）。"""
    if not isinstance(card, dict):
        return []

    block = card_data_block(card)
    book = block.get('character_book')
    if isinstance(book, dict):
        entries = book.get('entries')
    elif isinstance(book, list):
        entries = book
    else:
        entries = None

    if not isinstance(entries, list):
        return []

    names = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get('comment') or entry.get('name') or ''
        name = normalize_text(name)
        if name:
            names.append(name)
    return sorted(set(names))


def card_content_hash(card, wi_entry_names=()) -> str:
    """计算内容哈希。

    Args:
        card: `api.cards.read()` 的返回值。
        wi_entry_names: 额外参与哈希的世界书条目名（如内嵌世界书之外的来源）。
    """
    fields = card_fields(card)
    names = set(worldbook_entry_names(card))
    for name in wi_entry_names or ():
        normalized = normalize_text(name)
        if normalized:
            names.add(normalized)

    parts = [fields[key] for key in CARD_HASH_FIELDS]
    parts.append('|'.join(sorted(names)))

    digest = hashlib.sha256(_SEPARATOR.join(parts).encode('utf-8')).hexdigest()
    return f'{HASH_PREFIX}{digest}'


def short_hash(value, length: int = 12) -> str:
    """截断哈希，用于日志与 UI 展示。"""
    text = str(value or '')
    if text.startswith(HASH_PREFIX):
        text = text[len(HASH_PREFIX):]
    size = max(4, int(length or 12))
    return text[:size]
