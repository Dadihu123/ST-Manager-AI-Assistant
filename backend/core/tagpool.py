"""标签池：来源、预算、相关性排序、隔离分类硬过滤、别名出站还原。

纯函数层，无 flask、无主程序依赖（设计文档 §4）。

**为什么需要独立的标签池**：

- 现状把「全部标签库、硬截断 200 条」塞进提示词。200 个中文标签 +
  分类 + 频次 ≈ **3000–5000 token**，对「分析一张卡」来说是荒谬开销，
  很可能超过卡片本身。
- 现状只按使用频次排序。频次高不代表**这张卡**用得上。

两组改进：

| 维度 | 改善 |
|---|---|
| 来源 | `library`（全库）/ `categories`（只取指定分类）/ `manual`（只用手填） |
| 预算 | 按 **token** 而非条数截断，两层注入（分类概览 + 预算内标签） |
| 排序 | `(-频次, -与卡片的词面重叠, name)` —— 让「这张卡可能用到的」优先进预算 |
| 过滤 | 隔离分类变成**硬过滤**（原先只当变量给 AI 看，是软约束） |
| 别名 | AI 输出的替代词 → **出站**还原成真实标签（单向，入站不转换） |

**与主程序的联动边界**：基底来自 `api.tags.library()`；用户裁剪只存插件档案，
**不回写主程序**（避免插件篡改用户的标签治理配置）。
"""

import json
import re

from .tokens import estimate

#: 标签池来源
SOURCE_LIBRARY = 'library'
SOURCE_CATEGORIES = 'categories'
SOURCE_MANUAL = 'manual'
SOURCES = (SOURCE_LIBRARY, SOURCE_CATEGORIES, SOURCE_MANUAL)

DEFAULT_BUDGET_TOKENS = 1200

#: 分类概览（第 1 层）最多占预算的比例
OUTLINE_RATIO = 0.25

#: 单个标签行的最小估算（含换行）
MIN_LINE_TOKENS = 4

#: 与卡片词面重叠的判定：CJK 按 2 字滑窗，拉丁按 >=3 字母的词
_CJK_PATTERN = re.compile(r'[\u4e00-\u9fa5]+')
_WORD_PATTERN = re.compile(r'[A-Za-z][A-Za-z0-9]{2,}')
MAX_CARD_TOKENS_SCAN = 20000


def default_pool() -> dict:
    """默认标签池：等同改造前的行为（全库、按频次、尊重隔离分类）。"""
    return {
        'source': SOURCE_LIBRARY,
        'categories_include': [],
        'categories_exclude': [],
        'tags_include': [],
        'tags_exclude': [],
        'respect_isolated': True,
        'budget_tokens': DEFAULT_BUDGET_TOKENS,
        'allow_new_tags': True,
        'max_new_tags': 5,
    }


def _to_str_list(values) -> list:
    """去空白 + 去重（保留首次出现的顺序）。

    注意 `str`/`dict_keys` 这类**不是** list/tuple/set 的有序序列不收：
    `classify_tags()` 的分类名走的是 `dict_keys`，历史上一直是空列表，
    改宽它会改变 `/tag-pool` 的返回内容（见 routes.py 的分类名汇总）。
    """
    if not isinstance(values, (list, tuple, set)):
        return []
    result = []
    # 用 set 记账而不是 `text not in result`：后者是 list 上的线性查找，
    # 3000 个标签时单次 28ms（O(N²)），而 classify_tags 每张卡要被调 4–5 次。
    # 元素全是 `str()` 的产物，set 与 list 的相等判定结果完全一致。
    seen = set()
    for item in values:
        text = str(item or '').strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def normalize_pool(raw) -> dict:
    """把任意输入收敛为合法标签池（坏值回落默认，不抛错）。"""
    data = raw if isinstance(raw, dict) else {}
    base = default_pool()

    source = str(data.get('source') or SOURCE_LIBRARY).strip().lower()
    if source not in SOURCES:
        source = SOURCE_LIBRARY

    try:
        budget = int(data.get('budget_tokens', DEFAULT_BUDGET_TOKENS) or 0)
    except (TypeError, ValueError):
        budget = DEFAULT_BUDGET_TOKENS
    budget = max(0, min(budget, 20000))

    try:
        max_new = int(data.get('max_new_tags', base['max_new_tags']) or 0)
    except (TypeError, ValueError):
        max_new = base['max_new_tags']

    return {
        'source': source,
        'categories_include': _to_str_list(data.get('categories_include')),
        'categories_exclude': _to_str_list(data.get('categories_exclude')),
        'tags_include': _to_str_list(data.get('tags_include')),
        'tags_exclude': _to_str_list(data.get('tags_exclude')),
        'respect_isolated': bool(data.get('respect_isolated', True)),
        'budget_tokens': budget,
        'allow_new_tags': bool(data.get('allow_new_tags', True)),
        'max_new_tags': max(0, min(max_new, 20)),
    }


def profile_pool(profile, config=None) -> dict:
    """解析最终生效的标签池。

    优先级：**档案 > 全局配置 > 内置默认**（与 `content_selector` 一致：
    档案是「这次任务要看什么」的直接表达，理应压过全局）。
    """
    spec = profile if isinstance(profile, dict) else {}
    conf = config if isinstance(config, dict) else {}

    base = conf.get('tag_pool') if isinstance(conf.get('tag_pool'), dict) else None
    if base is None:
        base = default_pool()

    override = spec.get('tag_pool')
    if not isinstance(override, dict) or not override:
        return normalize_pool(base)

    merged = dict(normalize_pool(base))
    merged.update(override)
    return normalize_pool(merged)


def protected_tags(tag_library, pool, *, index=None) -> set:
    """「冻结分类」下的标签名集合：所属分类被排除，或该分类是隔离分类。

    这些分类是用户**手写并已固定**的内容（「排除」的语义就是「别动我这些」），
    因此它们不只是「不给 AI 看」，连 AI 的「建议删除标签」也不该指向它们。

    与 `blocked` 的区别：`blocked` 还包含「不在 include 白名单里」的标签
    （那是「这次不参与挑词」，不代表用户冻结了它们）；这里只认**明确排除 /
    隔离**的分类。

    Args:
        index: 已经算好的 `classify_tags()` 结果（可选）。作业里每张卡都要调
            这里，而 `classify_tags()` 的成本随标签数近似平方增长
            （3000 个标签时约 27ms）—— 传进来就能整批复用。
    """
    spec = normalize_pool(pool)
    if index is None:
        index = classify_tags(tag_library)

    frozen = set(spec['categories_exclude'])
    if spec['respect_isolated']:
        frozen |= set(index['isolated'])
    if not frozen:
        return set()

    return {
        tag for tag, category in index['tag_to_category'].items()
        if category in frozen
    }


# ------------------------------------------------------------------ 词面重叠


def card_text_vocabulary(card, *, limit: int = MAX_CARD_TOKENS_SCAN) -> set:
    """从卡片文本里抽「词面词汇」，用于判断某个标签跟这张卡是否相关。

    做法刻意保守（宁可漏判也不要错判）：

    - CJK：连续汉字串的 **2 字滑窗**（「校园恋爱」→ 校园/园恋/恋爱）
    - 拉丁：≥3 字母的词，小写化

    这不是分词，只是「共享子串」的粗判据 —— 对「标签是否与卡面文字沾边」
    这个用途足够了，且零依赖。
    """
    if not isinstance(card, dict):
        return set()

    # 卡片正文可能很大；只取前 N 字符，避免为了排序把整张卡过一遍
    block = card.get('data') if isinstance(card.get('data'), dict) else card
    parts = []
    for key in ('name', 'description', 'personality', 'scenario', 'first_mes',
                'mes_example', 'creator_notes', 'system_prompt'):
        value = block.get(key)
        if value:
            parts.append(str(value))
    tags = block.get('tags')
    if isinstance(tags, (list, tuple)):
        parts.extend(str(item) for item in tags)

    text = '\n'.join(parts)[:limit]
    if not text:
        return set()

    vocabulary = set()
    for match in _CJK_PATTERN.finditer(text):
        run = match.group(0)
        if len(run) == 1:
            vocabulary.add(run)
            continue
        for index in range(len(run) - 1):
            vocabulary.add(run[index:index + 2])
    for match in _WORD_PATTERN.finditer(text):
        vocabulary.add(match.group(0).lower())

    return vocabulary


def _overlap_score(tag: str, vocabulary) -> int:
    """标签与卡面词汇的共享度（0 表示毫无关系）。"""
    if not vocabulary:
        return 0
    text = str(tag or '').strip()
    if not text:
        return 0

    score = 0
    for match in _CJK_PATTERN.finditer(text):
        run = match.group(0)
        if len(run) == 1:
            score += 1 if run in vocabulary else 0
            continue
        for index in range(len(run) - 1):
            if run[index:index + 2] in vocabulary:
                score += 1
    for match in _WORD_PATTERN.finditer(text):
        if match.group(0).lower() in vocabulary:
            score += 1

    return score


# ------------------------------------------------------------------ 候选集合


def classify_tags(tag_library) -> dict:
    """把标签库拆成便于过滤的几份索引。

    Returns:
        `{'tags': [...], 'tag_to_category': {...}, 'categories': [...],
           'isolated': [...], 'usage': {...}, 'blacklist': [...]}`
    """
    data = tag_library if isinstance(tag_library, dict) else {}

    tags = _to_str_list(data.get('tags'))

    taxonomy = data.get('taxonomy') if isinstance(data.get('taxonomy'), dict) else {}
    tag_to_category = {}
    raw_map = taxonomy.get('tag_to_category')
    if isinstance(raw_map, dict):
        for tag, category in raw_map.items():
            name = str(tag or '').strip()
            group = str(category or '').strip()
            if name and group:
                tag_to_category[name] = group

    categories = _to_str_list(taxonomy.get('categories').keys()
                              if isinstance(taxonomy.get('categories'), dict) else [])

    # 没有显式分类映射的标签归入 `default_category`（宿主标签页显示为「未整理」）。
    # 不做这一步，「未整理」只统计显式映射的那部分，数量和宿主界面对不上
    # （实测：宿主显示 287，插件只数到 12）。同时让「排除 未整理」真的能挡住它们。
    #
    # 但**必须记住哪些是回填的**：`source='categories'`（只取已归类）依赖
    # 「这个标签没有分类」这个事实。回填后人人都有分类，那条判断就永远不成立，
    # 该模式会静默退化成 `source='library'`（未归类标签全被放进来）。
    default_category = str(taxonomy.get('default_category') or '').strip()
    defaulted_tags = set()
    if default_category:
        for tag in tags:
            if tag not in tag_to_category:
                tag_to_category[tag] = default_category
                defaulted_tags.add(tag)
        if default_category not in categories:
            categories.append(default_category)

    prefs = data.get('prefs') if isinstance(data.get('prefs'), dict) else {}

    isolated = data.get('isolated_categories') or prefs.get('isolated_categories') or []
    if isinstance(isolated, dict):
        isolated = list(isolated.keys())
    isolated = _to_str_list(isolated)

    blacklist = prefs.get('tag_blacklist') or []
    if isinstance(blacklist, str):
        blacklist = [part.strip() for part in blacklist.split(',') if part.strip()]
    blacklist = _to_str_list(blacklist)

    return {
        'tags': tags,
        'tag_to_category': tag_to_category,
        'categories': categories,
        'default_category': default_category,
        # 只有「显式映射」才算已归类；回填到 default_category 的属于「未整理」。
        # `source='categories'` 与 /tag-pool 的 uncategorized 计数都读这一份，
        # 不能直接看 tag_to_category（那里面已经没有空分类了）。
        'defaulted_tags': defaulted_tags,
        'isolated': isolated,
        'usage': data.get('usage') if isinstance(data.get('usage'), dict) else {},
        'blacklist': blacklist,
    }


def resolve_candidates(tag_library, pool, *, card=None, index=None) -> dict:
    """按标签池的「来源」与「排除项」算出可用候选集。

    Args:
        index: 已经算好的 `classify_tags()` 结果（可选）。`build_pool_text()`
            自己就有一份，传进来就不用再拆一遍标签库。

    Returns:
        `{'allowed': [...], 'excluded': {tag: reason}, 'isolated_categories': [...]}`
    """
    spec = normalize_pool(pool)
    if index is None:
        index = classify_tags(tag_library)

    known = set(index['tags'])
    tag_to_category = index['tag_to_category']
    # 回填到 default_category 的标签：分类字段有值，但语义上仍是「未整理」。
    defaulted_tags = index.get('defaulted_tags') or set()

    isolated_categories = set(index['isolated']) if spec['respect_isolated'] else set()
    include_categories = set(spec['categories_include'])
    exclude_categories = set(spec['categories_exclude']) | isolated_categories

    manual_in = list(spec['tags_include'])
    manual_out = set(spec['tags_exclude'])

    excluded = {}

    def _category_of(tag):
        return tag_to_category.get(tag, '')

    # ---- 决定基础集合 ----
    if spec['source'] == SOURCE_MANUAL:
        base = list(manual_in)
    else:
        base = list(index['tags'])

    # ---- 分类过滤（categories 来源额外要求「必须在 include 里」）----
    selected = []
    for tag in base:
        category = _category_of(tag)

        if category and category in exclude_categories:
            if category in isolated_categories:
                excluded[tag] = f'隔离分类 {category}'
            else:
                excluded[tag] = f'排除分类 {category}'
            continue

        # 「只取这些分类」= 白名单：**没有分类的标签同样不算数**。
        # 不这么判，用户设了「只取 题材」之后，未分类标签仍会漏进候选集，
        # 表现就是「设了跟没设一样」（用户报过的同一类问题）。
        if include_categories:
            if not category:
                excluded[tag] = '无分类'
                continue
            if category not in include_categories:
                excluded[tag] = f'不在指定分类 {category}'
                continue

        if spec['source'] == SOURCE_CATEGORIES:
            # 「只取已归类」：回填进 default_category 的标签算未归类，
            # 否则 default_category 回填会让这个判断永远不成立，该模式静默失效。
            if tag in defaulted_tags:
                excluded[tag] = '无分类'
                continue
            if not category:
                excluded[tag] = '无分类'
                continue
            if include_categories and category not in include_categories:
                excluded[tag] = f'不在指定分类 {category}'
                continue

        if tag in manual_out:
            excluded[tag] = '手动排除'
            continue

        selected.append(tag)

    # ---- 手动追加（即使不在库里也允许；但排除项优先）----
    for tag in manual_in:
        if tag in manual_out:
            excluded[tag] = '手动排除'
            continue
        category = _category_of(tag)
        if category and category in exclude_categories:
            excluded[tag] = f'隔离分类 {category}' if category in isolated_categories else f'排除分类 {category}'
            continue
        if tag not in selected:
            selected.append(tag)

    return {
        'allowed': selected,
        # 被「明确排除」的标签（隔离/排除分类/手动排除）。
        # **注意与 allowed 的区别**：`allowed` 是候选全集，不在里面**不等于**被禁；
        # 只有这里列出的才是「无论如何都不许写」的标签。
        'blocked': sorted(excluded),
        'excluded': excluded,
        'isolated_categories': sorted(isolated_categories),
        'known': known,
    }


# ------------------------------------------------------------------ 每分类规则（置信度 / 数量）

#: 没有单独设置的分类，默认置信度门槛（用户定的：未展开 = 0.5）
DEFAULT_CATEGORY_CONFIDENCE = 0.5

#: 一次最多打几个标签的上限（与 manifest `config_schema.max_tags.maximum` 保持一致）。
#: 之前是 20，用户在宿主配置页填 30 会被夹成 20，看起来像「两个地方数量对不上」。
MAX_LABEL_COUNT = 99

#: 一次最多接受多少个分类规则（防脏数据）
MAX_RULE_CATEGORIES = 100


def _finite_float(value):
    """把值转成**有限**浮点数；解析失败 / NaN / ±Inf 一律返回 None。

    这些值很容易进来：`tag_category_rules` 是宿主设置页可直接编辑的字符串键，
    用户（或某次 JSON 往返）写进 `1e400` / `"Infinity"` 都是合法的 JSON。
    不在这里挡掉，`int(float(...))` 会抛 `OverflowError` —— 它不属于
    `TypeError/ValueError`，会一路穿透 `normalize_config()`（`load_config()`
    只兜 `api.config.get()` 那一步）让所有接口 500。
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float('inf'), float('-inf')):  # NaN / ±Inf
        return None
    return number


def _to_confidence(value):
    """把置信度收敛到 0–1；无法解析返回 None（= 用默认值）。"""
    number = _finite_float(value)
    if number is None:
        return None
    return max(0.0, min(1.0, number))


def _to_quota(value):
    """把「设置数量」收敛为非负整数；None / 空 = 不限（返回 None）。"""
    if isinstance(value, str) and not value.strip():
        return None
    number = _finite_float(value)
    if number is None:
        return None
    return max(0, min(MAX_LABEL_COUNT, int(number)))


def normalize_rules(raw) -> dict:
    """把「每个标签分类的规则」收敛为 `{分类: {'confidence_min': float, 'max_tags': int|None}}`。

    输入既接受 dict，也接受 JSON 字符串（配置里只能存字符串）。

    语义（与设置页一致）：
    - `confidence_min` 缺省 → 用 `DEFAULT_CATEGORY_CONFIDENCE`；
    - `max_tags` 缺省 / `None` → **不限**；`0` → 该分类**一个都不选**。
    """
    data = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return {}
    if not isinstance(data, dict):
        return {}

    rules = {}
    for index, (raw_name, raw_rule) in enumerate(data.items()):
        if index >= MAX_RULE_CATEGORIES:
            break
        name = str(raw_name or '').strip()
        if not name or len(name) > 64:
            continue
        if not isinstance(raw_rule, dict):
            continue
        confidence = _to_confidence(raw_rule.get('confidence_min'))
        quota = _to_quota(raw_rule.get('max_tags'))
        rule = {}
        if confidence is not None:
            rule['confidence_min'] = confidence
        if quota is not None:
            rule['max_tags'] = quota
        if rule:
            rules[name] = rule
    return rules


def rule_for(rules, category, *, default_confidence=DEFAULT_CATEGORY_CONFIDENCE) -> dict:
    """取某个分类的生效规则：阈值缺省为 `default_confidence`，数量缺省为「不限」。"""
    source = rules if isinstance(rules, dict) else {}
    rule = source.get(str(category or '')) or {}
    confidence = _to_confidence(rule.get('confidence_min'))
    quota = _to_quota(rule.get('max_tags'))
    return {
        'confidence_min': default_confidence if confidence is None else confidence,
        'max_tags': quota,
    }


def _fmt_confidence(value) -> str:
    return f'{float(value):.2f}'


def rules_text(rules, max_total, *, default_confidence=DEFAULT_CATEGORY_CONFIDENCE) -> str:
    """把规则写成给模型看的「标签配额」段落（空规则返回空串）。"""
    source = normalize_rules(rules)
    total = _to_quota(max_total)
    if not source and not total:
        return ''

    lines = []
    if total:
        lines.append(f'标签总数最多 {total} 个。')
    else:
        lines.append('标签总数不限。')
    if source:
        lines.append('按分类的额外要求：')
        for name in sorted(source, key=lambda item: (-(source[item].get('max_tags') or 0), item)):
            rule = rule_for(source, name, default_confidence=default_confidence)
            parts = [f'置信度不低于 {_fmt_confidence(rule["confidence_min"])}']
            quota = rule['max_tags']
            if quota == 0:
                parts.append('这个分类不要输出任何标签')
            elif quota is None:
                parts.append('数量不单独限制')
            else:
                parts.append(f'最多 {quota} 个')
            lines.append(f'- {name}：' + '，'.join(parts))
        lines.append(
            f'- 其余分类：置信度不低于 {_fmt_confidence(default_confidence)}，数量不单独限制。',
        )
    return '\n'.join(lines)


def apply_rules(tags, *, tag_to_category, rules, max_total,
                default_confidence=DEFAULT_CATEGORY_CONFIDENCE,
                profile_floor=0.0) -> dict:
    """按「分类置信度门槛 + 分类数量配额 + 总数上限」过滤 AI 给的标签。

    Args:
        tags: `[{'name': str, 'confidence': float|None}, ...]`（已做别名还原）。
        tag_to_category: 标签 → 分类（`classify_tags()` 的映射；未登记的标签为空串）。
        rules: `normalize_rules()` 的结果。
        max_total: 总上限（0/None = 不限）。
        default_confidence: 未单独设置的分类用这个门槛。
        profile_floor: 档案（`output_schema.tags.min_confidence`）自己的额外门槛。

    **档案门槛与分类门槛取更严格者**：分类规则是全局配置，档案是「这次任务」的
    具体声明，两者都表达「低于它别打」。只认分类规则会让用户自建档案里写的
    `min_confidence` 完全失效（README 明确承诺它仍生效）。

    **缺少置信度的标签保留**（与原 `parsing.validate_result` 的行为一致）：
    模型偶尔漏写 `confidence`，或直接返回字符串数组（`{"tags":["A","B"]}`）。
    这种标签没有分数可比，丢掉它等于因为一个字段格式偏差而清空全部标签；
    保留并告警比静默清空安全。

    Returns:
        `{'kept': [...], 'dropped': [{'name','reason','category'}], 'per_category': {...}}`
    """
    source = normalize_rules(rules)
    total_limit = _to_quota(max_total)
    # 总上限 0 / 缺省 = 不限（分类配额才是「0 = 不选」）
    if total_limit is None or total_limit <= 0:
        total_limit = None
    profile_floor = _to_confidence(profile_floor) or 0.0

    kept = []
    dropped = []
    missing_confidence = []
    for item in tags or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip()
        if not name:
            continue
        category = str(tag_to_category.get(name) or '') if isinstance(tag_to_category, dict) else ''
        rule = rule_for(source, category, default_confidence=default_confidence)
        threshold = max(float(rule['confidence_min']), profile_floor)
        confidence = _to_confidence(item.get('confidence'))
        if confidence is None:
            # 没有分数可比：保留，交给数量/总数配额决定去留，并单独告警
            missing_confidence.append(name)
            kept.append({'name': name, 'confidence': None, 'category': category})
            continue
        if confidence < threshold:
            dropped.append({
                'name': name,
                'category': category,
                'reason': f'置信度 {confidence:.2f} < {threshold:.2f}',
            })
            continue
        kept.append({'name': name, 'confidence': confidence, 'category': category})

    # ---- 每个分类的数量配额（按置信度从高到低保留）----
    by_category = {}
    for item in kept:
        by_category.setdefault(item['category'], []).append(item)

    after_quota = []
    for category, items in by_category.items():
        rule = rule_for(source, category, default_confidence=default_confidence)
        ordered = sorted(items, key=lambda entry: (-float(entry.get('confidence') or 0), entry['name']))
        quota = rule['max_tags']
        if quota is None:
            after_quota.extend(ordered)
            continue
        if quota <= 0:
            for entry in ordered:
                dropped.append({
                    'name': entry['name'], 'category': category,
                    'reason': f'分类「{category or "未分类"}」设为不选',
                })
            continue
        after_quota.extend(ordered[:quota])
        for entry in ordered[quota:]:
            dropped.append({
                'name': entry['name'], 'category': category,
                'reason': f'分类「{category or "未分类"}」超过 {quota} 个',
            })

    # ---- 总数上限（全局，按置信度从高到低）----
    ordered_all = sorted(
        after_quota, key=lambda entry: (-float(entry.get('confidence') or 0), entry['name']),
    )
    if total_limit is not None and len(ordered_all) > total_limit:
        final = ordered_all[:total_limit]
        for entry in ordered_all[total_limit:]:
            dropped.append({'name': entry['name'], 'category': entry['category'],
                            'reason': f'超过总上限 {total_limit} 个'})
    else:
        final = ordered_all

    per_category = {}
    for entry in final:
        per_category[entry['category']] = per_category.get(entry['category'], 0) + 1

    return {
        'kept': [{'name': entry['name'], 'confidence': entry['confidence']} for entry in final],
        'dropped': dropped,
        'per_category': per_category,
        # 缺置信度但被保留的标签（调用方据此写告警，别静默）
        'missing_confidence': [name for name in missing_confidence
                               if name in {entry['name'] for entry in final}],
    }


# ------------------------------------------------------------------ 预算与分层


def _format_line(tag: str, category: str, used: int) -> str:
    parts = [tag]
    if category:
        parts.append(f'分类:{category}')
    if used:
        parts.append(f'已用{used}次')
    return '- ' + ' · '.join(parts)


def _outline_line(category: str, count: int) -> str:
    return f'- {category}（{count} 个）'


def build_pool_text(tag_library, pool, *, card=None, budget_tokens=None,
                    usage=None, index=None) -> dict:
    """按预算构造两层标签池文本。

    第 1 层：分类概览（只有分类名 + 数量，成本极低但能让模型知道「有什么维度」）。
    第 2 层：预算内的标签明细，按 `(-频次, -词面重叠, name)` 排序。

    Args:
        usage: 使用频次表；不给则取标签库自带的那份，
            再没有就不按频次排序（仍会按词面重叠与名称排）。
        index: 已经算好的 `classify_tags()` 结果（可选）。不给就自己算；
            作业里每张卡都要调这里，把作业开头的索引传进来即可整批复用。

    Returns:
        `{'text', 'outline', 'listed', 'dropped', 'tokens', 'budget_tokens',
           'excluded', 'isolated_categories', 'available'}`
    """
    spec = normalize_pool(pool)
    if index is None:
        index = classify_tags(tag_library)
    resolved = resolve_candidates(tag_library, spec, card=card, index=index)

    allowed = resolved['allowed']
    usage_map = usage if isinstance(usage, dict) else index['usage']
    if not isinstance(usage_map, dict):
        usage_map = {}
    tag_to_category = index['tag_to_category']

    budget = int(budget_tokens if budget_tokens is not None else spec['budget_tokens'] or 0)
    budget = max(0, budget)

    # ---- 候选统计（给 UI 与预览用，不受预算影响）----
    available = len(allowed)
    result = {
        'text': '',
        'outline': '',
        'listed': [],
        'dropped': [],
        'tokens': 0,
        'budget_tokens': budget,
        'excluded': dict(sorted(resolved['excluded'].items())),
        'isolated_categories': resolved['isolated_categories'],
        'available': available,
    }

    if not allowed or budget <= 0:
        result['dropped'] = list(allowed)
        # 预算为 0 时仍要给出候选集与黑名单：硬过滤与「发多少」是两件事
        result['allowed'] = list(allowed)
        result['blocked'] = resolved['blocked']
        return result

    vocabulary = card_text_vocabulary(card)

    def _sort_key(tag):
        return (
            -int(usage_map.get(tag) or 0),
            -_overlap_score(tag, vocabulary),
            tag,
        )

    ordered = sorted(allowed, key=_sort_key)

    # ---- 第 1 层：分类概览 ----
    category_counts = {}
    for tag in ordered:
        category = tag_to_category.get(tag)
        if category:
            category_counts[category] = category_counts.get(category, 0) + 1

    outline_lines = []
    outline_tokens = 0
    outline_budget = int(budget * OUTLINE_RATIO)

    for category in sorted(category_counts, key=lambda name: (-category_counts[name], name)):
        line = _outline_line(category, category_counts[category])
        cost = estimate(line) + 1
        if outline_tokens + cost > outline_budget and outline_lines:
            break
        outline_lines.append(line)
        outline_tokens += cost

    # ---- 第 2 层：预算内标签明细 ----
    remaining = budget - outline_tokens
    listed = []
    dropped = []
    detail_lines = []
    detail_tokens = 0

    for tag in ordered:
        line = _format_line(tag, tag_to_category.get(tag, ''), int(usage_map.get(tag) or 0))
        cost = estimate(line) + 1
        if cost >= remaining or detail_tokens + cost > remaining:
            dropped.append(tag)
            continue
        detail_lines.append(line)
        detail_tokens += cost
        listed.append(tag)

    sections = []
    if outline_lines:
        sections.append('（分类概览）\n' + '\n'.join(outline_lines))
    if detail_lines:
        sections.append('\n'.join(detail_lines))
    if dropped:
        sections.append(f'（还有 {len(dropped)} 个标签因预算未列出）')

    text = '\n\n'.join(sections)

    # 尾部括注也吃预算：超出就逐项回退，直到装进预算。
    # 不做这一步会出现「预算 20、实际 31」—— 预算形同虚设。
    while detail_lines and estimate(text) > budget:
        dropped.insert(0, listed.pop())
        detail_lines.pop()
        sections = []
        if outline_lines:
            sections.append('（分类概览）\n' + '\n'.join(outline_lines))
        if detail_lines:
            sections.append('\n'.join(detail_lines))
        if dropped:
            sections.append(f'（还有 {len(dropped)} 个标签因预算未列出）')
        text = '\n\n'.join(sections)

    if estimate(text) > budget:
        # 连索引 + 括注都放不下 → 只留一句最小提示（再不行就彻底不出）
        sections = []
        if detail_lines:
            sections.append('\n'.join(detail_lines))
        text = '\n\n'.join(sections)
        outline_lines = []

    result.update({
        'text': text,
        'outline': '\n'.join(outline_lines),
        'listed': listed,
        'dropped': dropped,
        'tokens': estimate(text),
        # 候选全集（不在于此 = 陌生标签，会走「新标签建议」分支）
        'allowed': list(allowed),
        # 明确被禁的标签：只有这些才该被硬过滤挡下
        'blocked': resolved['blocked'],
    })
    return result


# ------------------------------------------------------------------ 别名还原


def normalize_aliases(raw) -> dict:
    """把别名表规范化为 `{alias: canonical}`。

    支持三种输入形态（与主程序 `parse_merge_rules` 同源，便于从规则集导入）：

    - `[{"alias": "a", "canonical": "b"}]`（本插件原生形态）
    - `[{"from": "a", "to": "b"}]`
    - `["a→b", "c -> d"]`
    """
    result = {}

    if isinstance(raw, dict):
        items = [{'alias': key, 'canonical': value} for key, value in raw.items()]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return result

    arrow = re.compile(r'(.*?)(?:→|->|=>)(.+)')

    for entry in items:
        alias = canonical = ''

        if isinstance(entry, str):
            match = arrow.search(entry)
            if match:
                alias = match.group(1).strip()
                canonical = match.group(2).strip()
        elif isinstance(entry, dict):
            alias = str(entry.get('alias') or entry.get('from') or '').strip()
            canonical = str(entry.get('canonical') or entry.get('to')
                            or entry.get('target') or '').strip()

        if alias and canonical and alias != canonical:
            result[alias] = canonical

    return result


def resolve_alias(tag: str, aliases, *, _seen=None) -> str:
    """沿别名链解析到终点（`a→b→c` 得到 `c`），带环检测。"""
    current = str(tag or '').strip()
    if not current:
        return ''

    seen = set(_seen or ())
    while current in aliases:
        if current in seen:
            # 成环：停在当前值，不追下去（避免死循环）
            break
        seen.add(current)
        nxt = str(aliases.get(current) or '').strip()
        if not nxt or nxt == current:
            break
        current = nxt

    return current


def restore_aliases(tags, aliases) -> tuple:
    """把 AI 输出的替代词还原成真实标签（**出站**方向）。

    必须在 `split_tags` **之前**做：那里用真实标签集判断「已知 / 新标签」，
    若 AI 输出的是别名而没先换回来，真实标签会被误判成新标签进建议队列。

    Args:
        tags: `[{'name', 'confidence'}]` 或字符串列表。
        aliases: 别名表（三种形态皆可）。

    Returns:
        `(归一化后的 tags, {'applied': [{'from','to'}], 'aliases': {...}})`
    """
    table = normalize_aliases(aliases)

    items = tags if isinstance(tags, list) else []
    applied = []
    result = []
    seen = {}

    for entry in items:
        if isinstance(entry, dict):
            original = str(entry.get('name') or '').strip()
        else:
            original = str(entry or '').strip()

        if not original:
            continue

        resolved = resolve_alias(original, table) or original
        if resolved != original:
            applied.append({'from': original, 'to': resolved})

        # 还原后可能与已有项重合：合并置信度，取较高者
        if resolved in seen:
            existing = result[seen[resolved]]
            current_conf = entry.get('confidence') if isinstance(entry, dict) else None
            kept = existing.get('confidence')
            if current_conf is not None and (kept is None or current_conf > kept):
                existing['confidence'] = current_conf
            continue

        seen[resolved] = len(result)
        if isinstance(entry, dict):
            merged = dict(entry)
            merged['name'] = resolved
            result.append(merged)
        else:
            result.append(resolved)

    return result, {'applied': applied, 'aliases': table}


def validate_aliases(aliases, known_tags=None) -> list:
    """校验别名表，返回错误信息列表。

    检查两件真会出错的事：

    1. **同一别名映射到多个 canonical**（歧义）→ 报错。
    2. **别名恰好是真实标签**（出站会误替换真词）→ 报错。
       例如把 `校园` 当别名映射到 `学园`，那么模型正常输出的真实标签
       `校园` 也会被悄悄改掉。
    """
    errors = []

    raw_items = []
    if isinstance(aliases, dict):
        raw_items = [{'alias': key, 'canonical': value} for key, value in aliases.items()]
    elif isinstance(aliases, (list, tuple)):
        raw_items = list(aliases)

    arrow = re.compile(r'(.*?)(?:→|->|=>)(.+)')
    seen = {}

    for entry in raw_items:
        if isinstance(entry, str):
            match = arrow.search(entry)
            if not match:
                continue
            alias = match.group(1).strip()
            canonical = match.group(2).strip()
        elif isinstance(entry, dict):
            alias = str(entry.get('alias') or entry.get('from') or '').strip()
            canonical = str(entry.get('canonical') or entry.get('to')
                            or entry.get('target') or '').strip()
        else:
            continue

        if not alias or not canonical:
            continue
        if alias == canonical:
            errors.append(f'别名与目标相同，无意义: {alias}')
            continue

        if alias in seen and seen[alias] != canonical:
            errors.append(
                f'别名「{alias}」同时映射到「{seen[alias]}」与「{canonical}」（歧义）',
            )
            continue
        seen[alias] = canonical

    if known_tags:
        known = {str(item or '').strip() for item in known_tags}
        for alias in seen:
            if alias in known:
                errors.append(
                    f'别名「{alias}」与真实标签同名，出站会误替换真实标签',
                )

    return errors
