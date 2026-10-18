"""结构化提议：分析现有卡片库，提议分类体系与标签池（设计文档 §9.2）。

这是 Agent 能力的**阶段一**：不需要工具循环，一次 LLM 调用产出结构化提议，
人工审批后落地。它交付了用户要的核心价值
——「让 AI 大致分析已有角色卡，然后设置该如何分类，包括标签池的设计」。

**为什么先做阶段一**：阶段二（工具循环）的复杂度是成倍的，而阶段一已经够用。
真正的风险不在「AI 想不出来」，而在**上下文爆炸**与**提议没法落地**，
所以本模块的重心是：

| 关注点 | 做法 |
|---|---|
| 卡片库可能有上千张 | **分层采样 + token 预算**，不把整库塞进提示词 |
| AI 产出的分类名可能是路径穿越 | 复用 `core.classify` 的路径安全校验 |
| 提议可能不靠谱 | 输出**只能创建目录与标签**，不碰任何卡片内容 |
"""

import re

from . import classify as classify_core
from .tokens import estimate

#: 默认采样张数
DEFAULT_SAMPLE_SIZE = 30

#: 采样结果的 token 预算（整段采样的上限，不是每张）
DEFAULT_SAMPLE_BUDGET_TOKENS = 4000

#: 单张卡片在采样里的目标 token（名称 + 标签 + 摘要）
PER_CARD_TARGET_TOKENS = 110

#: 摘要/简介的截断长度
EXCERPT_CHARS = 160

#: 提议的分类数上限（防止 AI 一口气提 200 个）
MAX_CATEGORIES = 40

#: 每个分类的示例卡名数上限
MAX_EXAMPLES = 5

#: 每个分类的建议标签数上限
MAX_SUGGESTED_TAGS = 20

#: 标签合并建议数上限
MAX_MERGES = 50


def default_options() -> dict:
    return {
        'sample_size': DEFAULT_SAMPLE_SIZE,
        'sample_budget_tokens': DEFAULT_SAMPLE_BUDGET_TOKENS,
        'include_existing_categories': True,
        'include_existing_tags': True,
    }


def normalize_options(raw) -> dict:
    """规范化采样选项（坏值回落默认）。"""
    data = raw if isinstance(raw, dict) else {}
    base = default_options()

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
        'sample_size': _int('sample_size', 1, 200),
        'sample_budget_tokens': _int('sample_budget_tokens', 200, 60000),
        'include_existing_categories': bool(
            data.get('include_existing_categories', True),
        ),
        'include_existing_tags': bool(data.get('include_existing_tags', True)),
    }


# ------------------------------------------------------------------ 采样


def _card_brief(card) -> dict:
    """从卡片里抽采样需要的少量字段（绝不整卡带出）。"""
    from .hashing import card_data_block, normalize_text  # noqa: PLC0415

    block = card_data_block(card) if isinstance(card, dict) else {}

    tags = block.get('tags') or []
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.split(',') if part.strip()]
    tags = [str(item).strip() for item in tags if str(item or '').strip()][:8]

    description = normalize_text(block.get('description'))
    scenario = normalize_text(block.get('scenario'))

    return {
        'name': normalize_text(block.get('name')),
        'tags': tags,
        'excerpt': (description or scenario)[:EXCERPT_CHARS],
    }


def _brief_tokens(brief: dict) -> int:
    text = brief.get('name', '')
    text += ''.join(brief.get('tags') or [])
    text += brief.get('excerpt', '')
    return estimate(text) + 6


def stratified_sample(cards, *, options=None, categories=None) -> dict:
    """按分类 / 标签分层抽样。

    分层方式取**两层轮转**：先按分类分桶，桶内按标签排序，
    然后各桶轮流取一张 —— 这样分类数量悬殊时也不会全被大类占满。

    Args:
        cards: `[{'card_id', 'category', 'tags', 'brief'}]`（调用方已抽好 brief）。
        options: 采样选项。
        categories: 现有分类列表（仅用于统计覆盖情况）。

    Returns:
        `{'items', 'sampled', 'total', 'tokens', 'budget_tokens',
           'category_coverage', 'truncated'}`
    """
    spec = normalize_options(options)
    pool = [item for item in (cards if isinstance(cards, (list, tuple)) else [])
            if isinstance(item, dict)]

    total = len(pool)
    result = {
        'items': [],
        'sampled': 0,
        'total': total,
        'tokens': 0,
        'budget_tokens': spec['sample_budget_tokens'],
        'category_coverage': {},
        'truncated': False,
    }

    if not pool:
        return result

    # ---- 分桶 ----
    buckets = {}
    for item in pool:
        category = str(item.get('category') or '')
        buckets.setdefault(category, []).append(item)

    # 桶内按「标签数」降序：信息量大的卡优先（更能代表一类）
    for items in buckets.values():
        items.sort(key=lambda entry: (-len(entry.get('tags') or []),
                                      str(entry.get('card_id') or '')))

    # ---- 轮转取样 ----
    order = sorted(buckets, key=lambda name: (name == '', name))
    queues = {name: list(buckets[name]) for name in order}

    picked = []
    budget = spec['sample_budget_tokens']
    used = 0

    while len(picked) < spec['sample_size']:
        progressed = False
        for name in order:
            queue = queues[name]
            if not queue or len(picked) >= spec['sample_size']:
                continue

            candidate = queue.pop(0)
            cost = int(candidate.get('tokens') or _brief_tokens(candidate.get('brief') or {}))
            if used + cost > budget:
                # 预算用尽：停止（不跳过，避免为了塞进来而乱序）
                result['truncated'] = True
                queues[name] = []
                continue

            picked.append(candidate)
            used += cost
            progressed = True

        if not progressed:
            break

    result['items'] = picked
    result['sampled'] = len(picked)
    result['tokens'] = used

    coverage = {}
    for item in picked:
        category = str(item.get('category') or '')
        coverage[category] = coverage.get(category, 0) + 1
    result['category_coverage'] = dict(sorted(coverage.items()))

    if len(picked) < total:
        result['truncated'] = True

    return result


# ------------------------------------------------------------------ 提示词片段


def format_sample(sample_items) -> str:
    """把采样结果渲染成提示词里的卡片清单（紧凑，一张一行）。"""
    items = sample_items if isinstance(sample_items, list) else []
    lines = []

    for index, item in enumerate(items, start=1):
        brief = item.get('brief') if isinstance(item.get('brief'), dict) else {}
        name = str(brief.get('name') or item.get('card_id') or '(未命名)')
        parts = [f'{index}. {name}']

        tags = brief.get('tags') or []
        if tags:
            parts.append(f'标签：{"、".join(tags)}')

        category = str(item.get('category') or '')
        if category:
            parts.append(f'现有分类：{category}')

        line = '｜'.join(parts)
        excerpt = str(brief.get('excerpt') or '').strip()
        if excerpt:
            line += f'\n   {excerpt}'
        lines.append(line)

    return '\n'.join(lines)


def format_existing_context(categories, tags, *, tag_limit: int = 120) -> str:
    """把「现有分类 / 现有标签」渲染成提示词片段（空则返回空串）。"""
    sections = []

    category_list = [str(item).strip() for item in (categories or []) if str(item or '').strip()]
    if category_list:
        sections.append('现有分类：\n' + '\n'.join(f'- {name}' for name in category_list[:60]))

    tag_list = [str(item).strip() for item in (tags or []) if str(item or '').strip()]
    if tag_list:
        shown = tag_list[:tag_limit]
        text = '现有标签：' + '、'.join(shown)
        if len(tag_list) > len(shown):
            text += f'（共 {len(tag_list)} 个）'
        sections.append(text)

    return '\n\n'.join(sections)


# ------------------------------------------------------------------ 提议收敛


def _to_str_list(values, limit) -> list:
    if not isinstance(values, (list, tuple)):
        return []
    result = []
    for item in values:
        text = str(item or '').strip()
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def normalize_tag_merges(raw, known_tags=None) -> tuple:
    """规范化标签合并建议。

    复用 `core.tagpool` 的别名语义（`from → to`，链式 + 环检测），
    因为二者本质是同一件事：把近义标签收敛到规范名。

    Returns:
        `(merges, warnings)`；`merges` 形如 `[{'from': [...], 'to': str, 'reason': str}]`
    """
    items = raw if isinstance(raw, (list, tuple)) else []
    merges = []
    warnings = []

    known = {str(item or '').strip() for item in (known_tags or []) if str(item or '').strip()}

    for entry in items[:MAX_MERGES]:
        if not isinstance(entry, dict):
            continue

        target = str(entry.get('to') or entry.get('canonical') or entry.get('target') or '').strip()
        sources = _to_str_list(
            entry.get('from') or entry.get('sources') or entry.get('aliases'), 20,
        )
        reason = str(entry.get('reason') or '').strip()[:200]

        if not target:
            warnings.append('有一条合并建议缺少目标标签，已忽略')
            continue
        if not sources:
            warnings.append(f'合并建议「{target}」没有来源标签，已忽略')
            continue

        # 去掉「自己是自己」的项
        sources = [name for name in sources if name != target]
        if not sources:
            continue

        entry_out = {'from': sources, 'to': target, 'reason': reason}

        # 目标不在现有标签库时提示（不拦——AI 可能正是在提议新规范名）
        if known and target not in known:
            entry_out['new_target'] = True

        merges.append(entry_out)

    return merges, warnings


def normalize_category_proposal(raw, *, existing_categories=None) -> tuple:
    """规范化分类提议。

    **复用 `core.classify` 的路径安全校验**：AI 产出的分类名绝不能直接当目录用。

    Returns:
        `(categories, warnings)`；非法项被丢弃并记入 warnings。
    """
    items = raw if isinstance(raw, (list, tuple)) else []
    categories = []
    warnings = []

    existing = {str(item or '').strip() for item in (existing_categories or [])}
    seen = set()

    for entry in items[:MAX_CATEGORIES]:
        if isinstance(entry, dict):
            name = entry.get('name') or entry.get('path') or entry.get('category')
            description = str(entry.get('description') or '').strip()[:300]
            examples = _to_str_list(entry.get('examples'), MAX_EXAMPLES)
            suggested_tags = _to_str_list(entry.get('suggested_tags'), MAX_SUGGESTED_TAGS)
            estimate_raw = entry.get('card_count_estimate')
        else:
            name = entry
            description = ''
            examples = []
            suggested_tags = []
            estimate_raw = None

        path, error = classify_core.normalize_category_path(name)
        if error:
            warnings.append(f'分类提议被丢弃：{error}')
            continue
        if not path:
            warnings.append(f'分类提议被丢弃：不能是根目录（{name!r}）')
            continue
        if path in seen:
            continue

        seen.add(path)
        count = None
        if isinstance(estimate_raw, int) and not isinstance(estimate_raw, bool):
            count = max(0, estimate_raw)

        categories.append({
            'path': path,
            'name': path.rsplit(classify_core.PATH_SEPARATOR, 1)[-1],
            'description': description,
            'examples': examples,
            'suggested_tags': suggested_tags,
            'card_count_estimate': count,
            'existing': path in existing,
        })

    return categories, warnings


def normalize_proposal(data, *, existing_categories=None, known_tags=None) -> dict:
    """收敛 AI 的完整提议。

    Returns:
        `{'categories', 'tag_merges', 'rationale', 'issues', 'warnings',
           'new_categories', 'new_tags'}`
    """
    payload = data if isinstance(data, dict) else {}

    categories, cat_warnings = normalize_category_proposal(
        payload.get('categories') or payload.get('category_scheme'),
        existing_categories=existing_categories,
    )
    merges, merge_warnings = normalize_tag_merges(
        payload.get('tag_merges') or payload.get('merges'), known_tags=known_tags,
    )

    rationale = str(payload.get('rationale') or payload.get('reason') or '').strip()[:2000]

    issues = []
    if not categories and not merges:
        issues.append('提议既没有分类也没有标签合并，无法落地')

    # 汇总所有建议标签（去重），作为「标签池设计」的产出
    suggested_tags = []
    for category in categories:
        for tag in category['suggested_tags']:
            if tag not in suggested_tags:
                suggested_tags.append(tag)

    known = {str(item or '').strip() for item in (known_tags or [])}
    new_tags = [tag for tag in suggested_tags if tag not in known]

    return {
        'categories': categories,
        'tag_merges': merges,
        'rationale': rationale,
        'suggested_tags': suggested_tags,
        'new_tags': new_tags,
        'new_categories': [item['path'] for item in categories if not item['existing']],
        'issues': issues,
        'warnings': cat_warnings + merge_warnings,
    }


def build_proposal_instruction() -> str:
    """生成「怎么提议」的要求文本（追加在任务规格之后）。"""
    return '\n'.join([
        '输出要求：',
        '1. 只输出一个 JSON 对象，不要解释或 Markdown 代码块。',
        '2. categories：给出 5–20 个互斥且覆盖全库的分类。每个分类含',
        '   name（简短中文，可用「父级/子级」表达层级）、description（一句话）、',
        '   examples（1–5 个示例卡名）、suggested_tags（5–20 个标签）、',
        '   card_count_estimate（估计的卡片数量，整数）。',
        '3. tag_merges：把语义重复的现有标签收敛，形如',
        '   {"from": ["学园", "学校"], "to": "校园", "reason": "近义"}；没有就给空数组。',
        '4. rationale：用两三句话说明你的分类思路。',
        '5. 分类名不能含 \\ / : * ? " < > | 等字符，不能用「..」或绝对路径，',
        '   不能与 notes / backups / lorebooks / thumbnails / cards / trash 重名。',
    ])


def build_apply_plan(proposal) -> dict:
    """把提议拆成「可执行的动作计划」（供审批页展示与落地使用）。

    刻意**只包含创建类动作**：建目录、建标签。不碰卡片内容 ——
    「AI 提议 → 人审批 → 创建结构」这条链路上没有破坏性写入。
    """
    data = proposal if isinstance(proposal, dict) else {}

    categories = data.get('categories') or []
    new_categories = [item['path'] for item in categories if not item.get('existing')]
    existing_categories = [item['path'] for item in categories if item.get('existing')]

    all_tags = list(data.get('suggested_tags') or [])
    new_tags = list(data.get('new_tags') or [])

    return {
        'create_categories': new_categories,
        'existing_categories': existing_categories,
        'create_tags': new_tags,
        'known_tags': [tag for tag in all_tags if tag not in new_tags],
        'tag_merges': list(data.get('tag_merges') or []),
        # 标签合并**不自动执行**：它会改卡片标签，必须由用户单独确认
        'requires_manual_review': bool(data.get('tag_merges')),
    }
