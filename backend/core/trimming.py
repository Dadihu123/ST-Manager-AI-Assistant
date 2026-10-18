"""世界书条目的分层取样与文本裁剪。

纯函数层。对应设计文档 §5.8（分层取样）与 §6.3（P2 的缩减校验预留）。

取样策略：
    第 1 层（全量低成本）：所有启用条目的「名称 + 关键词」组成索引；
    第 2 层（按预算）：按高价值判据排序后装入 content，超出预算即停止并标注「节选」。

条目字段兼容 ST 的多种写法（`comment`/`name`、`keys`/`key`、`disable`/`enabled`），
与主程序 `core/utils/text.py:18-35` 的兼容口径保持一致。
"""

import re

from .hashing import normalize_text
from .tokens import estimate

HTML_TAG_PATTERN = re.compile(r'<[^>]{1,400}>')

DEFAULT_KEYWORDS = (
    '世界观', '设定', '背景', '叙事要求', '元数据', '规则', '设定集',
)

MIN_CONTENT_CHARS = 200
MAX_CONTENT_CHARS = 4000


# ------------------------------------------------------------------ 条目访问


def entry_uid(entry) -> str:
    """条目稳定标识：优先 `uid`，退回 `id`，都没有则空串。"""
    if not isinstance(entry, dict):
        return ''
    uid = entry.get('uid')
    if uid is None:
        uid = entry.get('id')
    return '' if uid is None else str(uid)


def entry_name(entry) -> str:
    if not isinstance(entry, dict):
        return ''
    return normalize_text(entry.get('comment') or entry.get('name'))


def entry_keys(entry) -> list:
    """条目关键词列表（`keys` 数组或 `key` 单值/数组）。"""
    if not isinstance(entry, dict):
        return []
    raw = entry.get('keys')
    if raw in (None, '', []):
        raw = entry.get('key')

    if isinstance(raw, str):
        parts = re.split(r'[,\n]', raw)
    elif isinstance(raw, (list, tuple)):
        parts = list(raw)
    else:
        return []

    result = []
    for item in parts:
        text = normalize_text(item)
        if text and text not in result:
            result.append(text)
    return result


def entry_content(entry) -> str:
    if not isinstance(entry, dict):
        return ''
    return '' if entry.get('content') is None else str(entry.get('content'))


def is_enabled(entry) -> bool:
    """条目是否启用：`disable=True` 表示停用，否则看 `enabled`（默认 True）。"""
    if not isinstance(entry, dict):
        return False
    if entry.get('disable') is True:
        return False
    return bool(entry.get('enabled', True))


def is_mostly_html(text, threshold: float = 0.5) -> bool:
    """判断文本是否主要由 HTML 标签构成（P2 缩减时用于跳过改写）。"""
    source = '' if text is None else str(text)
    if not source:
        return False
    tag_chars = sum(len(match.group(0)) for match in HTML_TAG_PATTERN.finditer(source))
    return (tag_chars / len(source)) >= float(threshold)


def normalize_entry(entry) -> dict:
    """把原始条目归一化为取样用结构。"""
    content = entry_content(entry)
    return {
        'uid': entry_uid(entry),
        'name': entry_name(entry),
        'keys': entry_keys(entry),
        'content': content,
        'constant': bool(entry.get('constant')) if isinstance(entry, dict) else False,
        'enabled': is_enabled(entry),
        'tokens': estimate(content),
    }


def truncate_to_tokens(text, max_tokens: int) -> str:
    """按 token 预算截断文本（按字符比例近似，末尾补省略号）。"""
    source = '' if text is None else str(text)
    limit = int(max_tokens or 0)
    if limit <= 0 or not source:
        return ''
    if estimate(source) <= limit:
        return source

    ratio = limit / max(1, estimate(source))
    cut = max(1, int(len(source) * ratio * 0.95))
    return source[:cut].rstrip() + '…'


# ------------------------------------------------------------------ 分层取样


def _outline_line(entry: dict) -> str:
    keys = '、'.join(entry.get('keys') or [])
    suffix = f'（关键词：{keys}）' if keys else '（无关键词）'
    return f'- {entry["name"] or "(未命名)"}{suffix}'


def _value_score(entry: dict, keywords) -> tuple:
    """高价值判据：常驻 > 名称命中关键词 > 长度落在有效区间 > token 降序。"""
    name = entry.get('name') or ''
    hit = any(word and word in name for word in keywords)
    length = len(entry.get('content') or '')
    in_range = MIN_CONTENT_CHARS <= length <= MAX_CONTENT_CHARS
    return (
        0 if entry.get('constant') else 1,
        0 if hit else 1,
        0 if in_range else 1,
        -int(entry.get('tokens') or 0),
        name,
    )


def select_wi_excerpt(entries, budget_tokens: int, *, keywords=None,
                      outline_ratio: float = 0.2, pinned_uids=None,
                      only_uids=None) -> dict:
    """按 token 预算做分层取样。

    Args:
        entries: 世界书条目列表（原始结构）。
        budget_tokens: 世界书部分的总预算（≤0 表示不出内容）。
        keywords: 高价值关键词表；默认 `DEFAULT_KEYWORDS`。
        outline_ratio: 索引层最多占用的预算比例。
        pinned_uids: **优先装入**的条目 uid（用户显式勾选的）。它们排在价值排序
            之前，因此不会因为「名字没命中关键词」而被挤掉；但仍受预算限制
            （超出预算是唯一能压过用户意愿的东西，此时标注 `truncated`）。
        only_uids: **只允许**这些 uid（非空时其余条目完全不参与，连索引也不出现）。
            用于「世界书：仅选定条目」模式。

    Returns:
        `{'outline', 'excerpt', 'count', 'total_tokens', 'excerpt_tokens',
           'truncated', 'selected', 'dropped', 'pinned', 'pinned_missing'}`
    """
    word_list = list(keywords) if keywords else list(DEFAULT_KEYWORDS)

    normalized = [normalize_entry(entry) for entry in (entries or []) if is_enabled(entry)]

    # 只保留指定条目（选择器「仅选定」模式）
    allowed = [str(item) for item in (only_uids or []) if str(item or '').strip()]
    pinned = [str(item) for item in (pinned_uids or []) if str(item or '').strip()]

    if allowed:
        allowed_set = set(allowed)
        normalized = [item for item in normalized if item['uid'] in allowed_set]

    total_tokens = sum(item['tokens'] for item in normalized)
    result = {
        'outline': '',
        'excerpt': '',
        'count': len(normalized),
        'total_tokens': total_tokens,
        'excerpt_tokens': 0,
        'truncated': False,
        'selected': [],
        'dropped': [],
        'pinned': [],
        'pinned_missing': [],
    }

    budget = max(0, int(budget_tokens or 0))
    if not normalized or budget <= 0:
        result['dropped'] = [item['name'] for item in normalized if item['name']]
        result['truncated'] = bool(normalized)
        # 勾选了但根本不在（或已停用）的条目要报出来，否则用户以为发出去了
        present = {item['uid'] for item in normalized}
        result['pinned_missing'] = [uid for uid in pinned if uid not in present]
        return result

    ratio = min(max(float(outline_ratio or 0.0), 0.0), 0.8)
    outline_budget = int(budget * ratio)

    # ---- 第 1 层：条目索引（按 token 降序，越重要的越靠前）----
    outline_lines = []
    outline_tokens = 0
    outline_truncated = False

    for item in sorted(normalized, key=lambda entry: -entry['tokens']):
        line = _outline_line(item)
        line_tokens = estimate(line) + 1
        if outline_tokens + line_tokens > outline_budget and outline_lines:
            outline_truncated = True
            break
        outline_lines.append(line)
        outline_tokens += line_tokens

    result['outline'] = '\n'.join(outline_lines)
    if outline_truncated:
        result['truncated'] = True
        result['outline'] += f'\n（索引已截断，共 {len(normalized)} 条）'

    # ---- 第 2 层：按价值排序装入内容（pinned 优先）----
    remaining = budget - outline_tokens
    selected = []
    dropped = []
    excerpt_blocks = []
    excerpt_tokens = 0

    if pinned:
        pinned_set = set(pinned)
        pinned_items = [item for item in normalized if item['uid'] in pinned_set]
        rest_items = [item for item in normalized if item['uid'] not in pinned_set]
        result['pinned'] = [item['name'] for item in pinned_items if item['name']]
        present = {item['uid'] for item in normalized}
        result['pinned_missing'] = [uid for uid in pinned if uid not in present]
        ordered = pinned_items + sorted(rest_items, key=lambda entry: _value_score(entry, word_list))
    else:
        ordered = sorted(normalized, key=lambda entry: _value_score(entry, word_list))

    for item in ordered:
        if remaining <= 0:
            dropped.append(item['name'])
            continue

        header = f'### {item["name"] or "(未命名)"}'
        keys = '、'.join(item['keys'])
        if keys:
            header += f'（关键词：{keys}）'

        header_tokens = estimate(header) + 2
        if header_tokens >= remaining:
            dropped.append(item['name'])
            continue

        content = item['content']
        content_budget = remaining - header_tokens
        content_tokens = estimate(content)

        if content_tokens > content_budget:
            content = truncate_to_tokens(content, content_budget)
            content_tokens = estimate(content)
            result['truncated'] = True

        if not content.strip():
            dropped.append(item['name'])
            continue

        excerpt_blocks.append(f'{header}\n{content}')
        excerpt_tokens += header_tokens + content_tokens
        remaining -= header_tokens + content_tokens
        selected.append(item['name'])

    if excerpt_blocks:
        result['excerpt'] = '（以下为节选）\n\n' + '\n\n'.join(excerpt_blocks)
        excerpt_tokens += estimate('（以下为节选）')

    result['excerpt_tokens'] = excerpt_tokens
    result['selected'] = selected
    result['dropped'] = dropped
    return result
