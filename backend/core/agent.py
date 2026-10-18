"""只读工具循环（Agent 阶段二）：分批读取卡片库，最后产出结构提议。

**为什么需要它**：卡片数量太多，一次读不完。阶段一（`core.proposal`）
只采样 30 张就下结论，样本之外的部分完全没看过。这里让模型**自己决定看哪些**：
先看全库目录（便宜），再按需分批读细节（贵），最后才提议。

**为什么不照搬 shujuku 的 Agent 实现**（那是 33 个文件 / 73 万字符，
含子代理、派工、账本、工作流、事务）。对照之后，它真正的机制只有四条，
其余都是「世界推演」这个业务域特有的：

| 机制 | shujuku 的做法 | 本模块的做法 |
|---|---|---|
| 动作协议 | 6 种动作 + 严格封闭对象校验 | **3 种**（`read` / `search` / `finalize`） |
| 轮次上限 | `maxIterations=4`、`maxReads=24` | 同量级（6 轮、40 张） |
| 读取门禁 | 累计授权 token + 超限拒绝并回灌原因 | **同机制**，代码从 39 行起 |
| 上下文压缩 | 丢弃整轮 → 生成「交接报告」 | **丢弃旧读取结果** → 一行提示「可重新 read」 |

最后一条是本模块最大的简化，也是**正确的简化**：shujuku 的交接报告之所以
要精心构造，是因为它丢弃的是「已经发生的剧情推演过程」，丢了就真的没了；
而这里丢弃的是**可重放的读取结果** —— 卡片内容随时能再读一次。
所以不需要报告，只需要告诉模型「这些你可以重新读」。
"""

import json
import re

from .tokens import estimate

# ------------------------------------------------------------------ 预算默认值


def default_budget() -> dict:
    """一轮 Agent 运行的预算。数值参照 shujuku 的量级，按卡片库场景调整。"""
    return {
        'max_iterations': 6,        # 最多 6 轮动作
        'max_reads': 40,            # 全run累计最多读 40 张卡
        'read_batch_tokens': 6000,  # 单次 read 的 token 上限
        'context_budget': 40000,    # 单次请求的上下文上限
        'catalog_tokens': 8000,     # 首轮目录的 token 上限
        'max_protocol_retries': 2,  # 协议错误的重试次数
    }


def normalize_budget(raw) -> dict:
    data = raw if isinstance(raw, dict) else {}
    base = default_budget()

    def _int(key, minimum, maximum):
        value = data.get(key)
        if isinstance(value, bool) or value is None:
            return base[key]
        try:
            number = int(value)
        except (TypeError, ValueError):
            return base[key]
        return max(minimum, min(maximum, number))

    return {
        'max_iterations': _int('max_iterations', 1, 20),
        'max_reads': _int('max_reads', 1, 200),
        'read_batch_tokens': _int('read_batch_tokens', 500, 60000),
        'context_budget': _int('context_budget', 4000, 400000),
        'catalog_tokens': _int('catalog_tokens', 1000, 100000),
        'max_protocol_retries': _int('max_protocol_retries', 0, 5),
    }


# ------------------------------------------------------------------ 动作协议

ACTION_READ = 'read'
ACTION_SEARCH = 'search'
ACTION_FINALIZE = 'finalize'
ACTIONS = (ACTION_READ, ACTION_SEARCH, ACTION_FINALIZE)

#: 详情层级：`summary` 只给摘要，`full` 给更完整的字段
DETAIL_SUMMARY = 'summary'
DETAIL_FULL = 'full'
DETAILS = (DETAIL_SUMMARY, DETAIL_FULL)

#: 单次 read 最多几张（防止模型一次要 500 张）
MAX_IDS_PER_READ = 20

#: 单次 search 最多返回几条
MAX_SEARCH_RESULTS = 30

#: 回灌给模型的工具结果上限
FEEDBACK_MAX_CHARS = 30000


class ProtocolError(ValueError):
    """模型输出不符合动作协议（对应 shujuku 的 `WorldSimulationProtocolIssue`）。"""

    def __init__(self, reason_code, path, expected, actual=None):
        self.reason_code = reason_code
        self.path = path
        self.expected = expected
        self.actual = actual
        detail = f'{reason_code} at {path}: 期望 {expected}'
        if actual is not None:
            detail += f'，实际 {actual!r}'
        super().__init__(detail)

    def fingerprint(self) -> str:
        """同一类错误只允许重试一次（照抄 shujuku 的去重口径）。"""
        return f'{self.reason_code}|{self.path}|{self.expected}'


def _require_object(value, path='$'):
    if not isinstance(value, dict):
        raise ProtocolError('OBJECT_REQUIRED', path, 'object', type(value).__name__)
    return value


def _require_text(value, path):
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError('TEXT_REQUIRED', path, 'non-empty string', value)
    return value.strip()


def _optional_list(value, path):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ProtocolError('ARRAY_REQUIRED', path, 'array', value)
    return [item for item in value if isinstance(item, str) and item.strip()]


def parse_action(value) -> dict:
    """把模型输出解析成三种动作之一。

    **严格封闭**：多余字段一律报错。理由同 shujuku ——
    模型多写一个字段而我们静默忽略，会让它以为「这个字段有效」，
    下一轮继续写、继续被忽略，行为就漂了。
    """
    data = _require_object(value)
    action = data.get('action')
    if not isinstance(action, str) or action not in ACTIONS:
        raise ProtocolError(
            'INVALID_ACTION', '$.action',
            ' | '.join(ACTIONS), action,
        )

    if action == ACTION_READ:
        _reject_extra(data, ['action', 'ids', 'detail'], '$')
        ids = _optional_list(data.get('ids'), '$.ids')
        if not ids:
            raise ProtocolError('IDS_REQUIRED', '$.ids', 'non-empty array of card ids', data.get('ids'))
        if len(ids) > MAX_IDS_PER_READ:
            raise ProtocolError(
                'TOO_MANY_IDS', '$.ids', f'at most {MAX_IDS_PER_READ}', len(ids),
            )
        detail = data.get('detail') or DETAIL_SUMMARY
        if detail not in DETAILS:
            raise ProtocolError('INVALID_DETAIL', '$.detail', ' | '.join(DETAILS), detail)
        return {'kind': ACTION_READ, 'ids': ids, 'detail': detail}

    if action == ACTION_SEARCH:
        _reject_extra(data, ['action', 'query', 'limit'], '$')
        query = _require_text(data.get('query'), '$.query')
        limit = data.get('limit') or 10
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ProtocolError('INVALID_LIMIT', '$.limit', 'positive integer', limit)
        return {'kind': ACTION_SEARCH, 'query': query,
                'limit': min(limit, MAX_SEARCH_RESULTS)}

    # finalize：这里只校验「带着提议」，具体字段由 core.proposal 收敛
    _reject_extra(data, ['action', 'categories', 'tag_merges', 'rationale'], '$')
    if not isinstance(data.get('categories'), list):
        raise ProtocolError('CATEGORIES_REQUIRED', '$.categories', 'array', data.get('categories'))
    return {
        'kind': ACTION_FINALIZE,
        'proposal': {
            'categories': data.get('categories'),
            'tag_merges': data.get('tag_merges'),
            'rationale': data.get('rationale'),
        },
    }


def _reject_extra(data, allowed, path):
    extra = [key for key in data if key not in allowed]
    if extra:
        raise ProtocolError(
            'UNKNOWN_FIELD', f'{path}.{extra[0]}',
            'no additional fields', extra[0],
        )


def render_rejection(error, budget=None) -> str:
    """把协议错误渲染成给模型的纠正提示（照抄 shujuku 的做法：回灌具体原因）。"""
    issue = error if isinstance(error, ProtocolError) else ProtocolError(
        'INVALID_OUTPUT', '$', 'JSON object', str(error)[:120],
    )
    lines = [
        '【协议未通过】',
        f'问题：{issue.reason_code}（{issue.path}）',
        f'期望：{issue.expected}',
    ]
    if issue.actual is not None:
        lines.append(f'实际：{issue.actual!r}'[:300])

    b = normalize_budget(budget)
    lines.append('')
    lines.append('请重新输出一个 JSON 对象，只能是以下三种之一：')
    lines.append('  读取卡片：{"action": "read", "ids": ["card1.png"], "detail": "summary"}')
    lines.append('  搜索卡片：{"action": "search", "query": "关键词", "limit": 10}')
    lines.append('  提交提议：{"action": "finalize", "categories": [...], "tag_merges": [...], "rationale": "..."}')
    lines.append(f'上限：单次最多读 {MAX_IDS_PER_READ} 张，整个运行最多 {b["max_reads"]} 张。')
    return '\n'.join(lines)


class RepairState:
    """协议修正预算（照抄 shujuku 的 `createWorldSimulationProtocolRepairState`）。

    同一个错误只重试一次：模型连续两次犯同样的错，多半是提示词或通道的问题，
    再重试只是白烧 token。
    """

    def __init__(self, max_attempts=2):
        self.max_attempts = max(0, int(max_attempts))
        self.attempts = 0
        self.fingerprints = {}

    def record(self, error) -> dict:
        issue = error if isinstance(error, ProtocolError) else ProtocolError(
            'INVALID_OUTPUT', '$', 'JSON object', str(error)[:120],
        )
        key = issue.fingerprint()
        self.attempts += 1
        self.fingerprints[key] = self.fingerprints.get(key, 0) + 1
        return {
            'retry': self.attempts <= self.max_attempts and self.fingerprints[key] < 2,
            'issue': issue,
        }


# ------------------------------------------------------------------ 读取门禁


def gate_read_batch(items, state, budget=None, *, context_tokens=0) -> dict:
    """决定这一批读取能否注入（对应 shujuku 的 `gateWorldSimulationReadBatch_ACU`）。

    **这是「卡片太多读不完」的核心闸门**：不看这个，模型会一口气要求读 200 张，
    请求直接超长失败，整轮白跑。

    两道限制：
    1. **累计**授权 token 不超过 `read_batch_tokens × 轮数上限`（防止化整为零）
    2. 当前上下文 + 本批 超过 `context_budget` 时，只有本批够小才放行
       （`context_budget × 0.15` 相当于 shujuku 的 `fallbackTokens`）

    Returns:
        `{'allowed', 'reason', 'batch_tokens', 'item_tokens', 'report'}`
    """
    spec = normalize_budget(budget)
    entries = entries_list = items if isinstance(items, list) else []

    item_tokens = [int(item.get('tokens') or estimate(str(item.get('text') or '')))
                   for item in entries_list]
    batch_tokens = sum(item_tokens)

    if not entries:
        return {'allowed': True, 'reason': '', 'batch_tokens': 0,
                'item_tokens': [], 'report': ''}

    granted = int(state.get('granted_tokens') or 0)
    max_total = spec['read_batch_tokens'] * spec['max_iterations']
    fallback = max(1, int(spec['context_budget'] * 0.15))

    reason = ''
    if granted + batch_tokens > max_total:
        reason = 'read-batch-too-large'
    elif (spec['context_budget'] > 0 and context_tokens > 0
          and context_tokens + batch_tokens > spec['context_budget']
          and batch_tokens > fallback):
        reason = 'near-context-overflow'

    if not reason:
        return {'allowed': True, 'reason': '', 'batch_tokens': batch_tokens,
                'item_tokens': item_tokens, 'report': ''}

    limit = max_total if reason == 'read-batch-too-large' else fallback
    sizes = '\n'.join(
        f'- {entries_list[index].get("label") or entries_list[index].get("card_id") or "?"}: '
        f'{item_tokens[index]} tokens'
        for index in range(len(entries_list))
    )
    report = (
        f'【读取被拒绝】\n原因：{reason}\n已授予：{granted}\n本批：{batch_tokens}\n'
        f'上限：{limit}\n{sizes}\n'
        f'请缩小本次读取范围（少读几张，或改用 summary 详情）后重试；本批正文未注入。'
    )
    return {'allowed': False, 'reason': reason, 'batch_tokens': batch_tokens,
            'item_tokens': item_tokens, 'report': report}


# ------------------------------------------------------------------ 上下文压缩


def measure_messages(messages) -> int:
    """估算一组 messages 的 token。"""
    total = 0
    for message in (messages if isinstance(messages, list) else []):
        if isinstance(message, dict):
            total += estimate(str(message.get('content') or ''))
    return total


def compact_transcript(transcript, budget=None, *, reserved_tokens=0) -> dict:
    """丢弃较早的读取结果，让上下文回到预算内。

    **比 shujuku 简单得多，且是正确的简化**：shujuku 丢弃的是「已发生的推演过程」，
    必须精心生成交接报告才不丢信息；这里丢弃的是**可重放的读取结果**，
    所以只需要告诉模型「这些可以重新 read」。

    保留最近的一轮（最相关），从最早的开始丢。
    """
    spec = normalize_budget(budget)
    messages = transcript if isinstance(transcript, list) else []
    total = measure_messages(messages) + int(reserved_tokens or 0)

    result = {
        'messages': list(messages),
        'changed': False,
        'dropped': 0,
        'total_tokens': total,
        'within_budget': total <= spec['context_budget'],
    }
    if total <= spec['context_budget'] or not messages:
        return result

    # 按「assistant 动作 + user 反馈」成对分组，整组丢弃
    groups = []
    index = 0
    while index < len(messages):
        group = [messages[index]]
        if (index + 1 < len(messages)
                and messages[index].get('role') == 'assistant'
                and messages[index + 1].get('role') == 'user'):
            group.append(messages[index + 1])
            index += 2
        else:
            index += 1
        groups.append(group)

    # 最近一组永远保留
    dropped_groups = []
    while len(groups) > 1 and total > spec['context_budget']:
        removed = groups.pop(0)
        dropped_groups.append(removed)
        total -= measure_messages(removed)
        total += estimate(_DROPPED_NOTE) if len(dropped_groups) == 1 else 0

    if not dropped_groups:
        return result

    note = {'role': 'user', 'content': (
        f'【已被移出上下文】较早的 {len(dropped_groups)} 次读取结果因上下文预算被移除。'
        '这些卡片的内容可以随时重新 read，不影响你继续判断。'
    )}
    kept = [note]
    for group in groups:
        kept.extend(group)

    result['messages'] = kept
    result['changed'] = True
    result['dropped'] = len(dropped_groups)
    result['total_tokens'] = measure_messages(kept) + int(reserved_tokens or 0)
    result['within_budget'] = result['total_tokens'] <= spec['context_budget']
    return result


_DROPPED_NOTE = '较早的读取结果已移除，可重新 read'


# ------------------------------------------------------------------ 目录（全库概览）


def build_catalog(rows, *, budget=None) -> dict:
    """把卡片行整理成「全库目录」—— **覆盖全库但便宜**。

    这是本模块的核心取舍：**目录覆盖全部卡片，读取只覆盖抽样**。
    - 目录：名称 + 标签 + 分类，每张 ~15 token → 1000 张约 15k token，一次给全
    - 读取：带正文细节，每张 ~150 token → 只够读 40 张

    模型先看目录看清明细全貌，再挑有代表性的读。这样「卡片太多读不完」
    就不是障碍了 —— 读不完的部分由目录兜底。

    目录本身也有预算：超了就**截断并如实标注**，同时给出统计摘要
    （分类计数、标签频次），让模型知道被截掉的是什么形状。
    """
    spec = normalize_budget(budget)
    entries = [item for item in (rows if isinstance(rows, list) else [])
               if isinstance(item, dict)]

    category_counts = {}
    tag_counts = {}
    for entry in entries:
        category = str(entry.get('category') or '')
        category_counts[category] = category_counts.get(category, 0) + 1
        for tag in (entry.get('tags') or []):
            name = str(tag or '').strip()
            if name:
                tag_counts[name] = tag_counts.get(name, 0) + 1

    lines = []
    used = 0
    budget_tokens = spec['catalog_tokens']
    truncated_at = None

    for index, entry in enumerate(entries):
        card_id = str(entry.get('card_id') or '')
        if not card_id:
            continue
        name = str((entry.get('brief') or {}).get('name')
                   or entry.get('name') or card_id)
        tags = [str(tag) for tag in (entry.get('tags') or [])][:5]
        category = str(entry.get('category') or '')

        line = f'- {name} [id={card_id}]'
        if tags:
            line += ' 标签:' + ','.join(tags)
        if category:
            line += f' 分类:{category}'

        cost = estimate(line)
        if used + cost > budget_tokens:
            truncated_at = index
            break
        lines.append(line)
        used += cost

    stats = {
        'total': len(entries),
        'listed': len(lines),
        'truncated': truncated_at is not None,
        'truncated_at': truncated_at,
        'tokens': used,
        'budget_tokens': budget_tokens,
        'category_counts': dict(sorted(
            category_counts.items(), key=lambda item: (-item[1], item[0]),
        )),
        'tag_counts': dict(sorted(
            tag_counts.items(), key=lambda item: (-item[1], item[0]),
        )[:60]),
    }
    return {'stats': stats, 'lines': lines}


def format_catalog(catalog) -> str:
    """把目录渲染成提示词片段（含统计摘要 + 卡片清单）。"""
    data = catalog if isinstance(catalog, dict) else {}
    stats = data.get('stats') or {}
    lines = data.get('lines') or []

    total = int(stats.get('total') or 0)
    if not total:
        return ''

    parts = [f'【卡片库目录】共 {total} 张，下面列出 {len(lines)} 张。']

    categories = stats.get('category_counts') or {}
    if categories:
        shown = list(categories.items())[:30]
        parts.append('现有分类分布：' + '、'.join(
            f'{name or "(未分类)"}={count}' for name, count in shown
        ))

    tags = stats.get('tag_counts') or {}
    if tags:
        parts.append('标签频次（前 40）：' + '、'.join(
            f'{name}={count}' for name, count in list(tags.items())[:40]
        ))

    if stats.get('truncated'):
        parts.append(
            f'⚠ 目录已按 token 预算截断，只列出前 {len(lines)} 张（共 {total} 张）。'
            '未列出的部分可用 search 按关键词查找。'
        )

    parts.append('')
    parts.append('卡片清单：')
    parts.append('\n'.join(lines))
    return '\n'.join(parts)


def search_catalog(rows, query, limit=10) -> list:
    """在目录里按关键词搜卡片（不消耗读取配额）。

    匹配名称、标签、分类三个位置。故意**不搜正文** —— 那样就得读卡，
    也就失去了「廉价搜索」的意义。
    """
    keyword = str(query or '').strip().lower()
    if not keyword:
        return []

    hits = []
    for entry in (rows if isinstance(rows, list) else []):
        if not isinstance(entry, dict):
            continue
        name = str((entry.get('brief') or {}).get('name') or entry.get('name') or '')
        tags = ' '.join(str(tag) for tag in (entry.get('tags') or []))
        category = str(entry.get('category') or '')
        haystack = f'{name} {tags} {category}'.lower()
        if keyword in haystack:
            hits.append(entry)
        if len(hits) >= limit:
            break
    return hits



# ------------------------------------------------------------------ 提示词


def build_agent_instruction(budget=None, *, catalog_summary='') -> str:
    """生成 Agent 的动作说明（追加在任务规格之后）。"""
    spec = normalize_budget(budget)
    lines = [
        '你可以分多轮工作：先看目录，再按需读取部分卡片的细节，最后提交提议。',
        '',
        '每轮只输出一个 JSON 对象，三选一：',
        '',
        '1) 读取卡片细节',
        '   {"action": "read", "ids": ["卡1.png", "卡2.png"], "detail": "summary"}',
        f'   - detail 可选 summary（默认，字段摘要）或 full（更完整的正文）',
        f'   - 单次最多 {MAX_IDS_PER_READ} 张；整个运行最多读 {spec["max_reads"]} 张',
        '',
        '2) 按关键词搜索卡片（只返回名称与标签，不消耗读取配额）',
        '   {"action": "search", "query": "奇幻", "limit": 10}',
        f'   - 单次最多返回 {MAX_SEARCH_RESULTS} 条',
        '',
        '3) 提交最终提议（结束）',
        '   {"action": "finalize",',
        '    "categories": [{"name": "分类名", "description": "...", "examples": ["..."],',
        '                    "suggested_tags": ["..."], "card_count_estimate": 10}],',
        '    "tag_merges": [{"from": ["旧名"], "to": "规范名", "reason": "近义"}],',
        '    "rationale": "分类思路"}',
        '',
        '工作建议：',
        '- 目录已经给了全库概览，先看清结构，再挑**有代表性**的卡片读，不要平均用力。',
        '- reading 的配额有限，优先读那些「说不清该归哪类」的卡。',
        '- 分类要互斥且覆盖全库；数量 5–20 个。',
        f'- 不要在没读任何卡的情况下直接 finalize；也请在 {spec["max_iterations"]} 轮内完成。',
    ]
    if catalog_summary:
        lines.append('')
        lines.append(catalog_summary)
    return '\n'.join(lines)


def format_card_text(card_id, brief, detail=DETAIL_SUMMARY) -> str:
    """把一张卡的摘要渲染成给模型看的文本。"""
    data = brief if isinstance(brief, dict) else {}
    lines = [f'### {data.get("name") or card_id}  (id: {card_id})']

    tags = data.get('tags') or []
    if tags:
        lines.append('标签：' + '、'.join(str(tag) for tag in tags))
    category = data.get('category') or ''
    if category:
        lines.append('现有分类：' + str(category))

    description = str(data.get('description') or '').strip()
    if description:
        lines.append('简介：' + description)

    if detail == DETAIL_FULL:
        for key, label in (('personality', '性格'), ('scenario', '场景'),
                           ('first_mes', '开场')):
            value = str(data.get(key) or '').strip()
            if value:
                lines.append(f'{label}：{value}')

        entries = data.get('worldbook') or []
        if entries:
            lines.append('世界书条目：' + '、'.join(str(item) for item in entries[:10]))

    return '\n'.join(lines)


def card_tokens(brief, detail=DETAIL_SUMMARY) -> int:
    return estimate(format_card_text('x', brief, detail))


# ------------------------------------------------------------------ 解析模型输出


_FENCE_PATTERN = re.compile(r'```(?:json)?\s*(.*?)```', re.S)


def extract_json(text) -> dict:
    """从模型输出里抠出 JSON 对象（容忍代码块与前后废话）。"""
    raw = str(text or '').strip()
    if not raw:
        raise ProtocolError('EMPTY_OUTPUT', '$', 'JSON object', '')

    candidates = []
    fenced = _FENCE_PATTERN.search(raw)
    if fenced:
        candidates.append(fenced.group(1).strip())
    candidates.append(raw)

    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value

    # 退化路径：找第一个 {...}
    start = raw.find('{')
    end = raw.rfind('}')
    if 0 <= start < end:
        try:
            value = json.loads(raw[start:end + 1])
        except (ValueError, TypeError):
            value = None
        if isinstance(value, dict):
            return value

    raise ProtocolError('JSON_PARSE_FAILED', '$', 'valid JSON object', raw[:200])
