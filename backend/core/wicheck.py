"""世界书改写的校验：只查「内容正确性」，不查「格式合法性」。

设计文档 §6.3 的定位（用户已拍板）：

**这不是要产出「程序能解析的 YAML」，而是要「让 AI 优先按 YAML 输出」的提示词约束。**
所以本模块**不做** YAML 解析/校验，也**不引入 `pyyaml`**。

判断依据是「谁读这个输出」：

| | 严格 JSON（分析输出） | 世界书改写 |
|---|---|---|
| 谁读 | **代码**（`json.loads` → 写回卡片） | **人和 AI**（正文进提示词） |
| 格式错的后果 | 解析失败 → 作业失败/静默空结果 | 读起来稍差，**不影响使用** |
| 因此 | 必须强校验 + 容错挽救 | 只需提示词要求 |

**保留的校验**（都是「内容被破坏」而非「格式不合法」）：

| 规则 | 为什么 |
|---|---|
| 宏保真 | `{{…}}` 是**程序**读的注入机制，丢了就是功能性破坏 |
| HTML 配平 | 卡片前端靠 HTML 渲染，配平破坏会显示错乱 |
| 压缩有效 | 这是本能力的目的，不达标就没意义 |
| 长度比 | 防止 AI 把整条删成一句话（静默丢设定） |
| 专有名词覆盖 | 防止丢关键设定；缺失项进 `dropped_facts` 供人确认 |
| 调度字段不变 | `constant`/`insertion_order` 等是**程序**读的，改了会变触发行为 |
"""

import re

from .tokens import estimate

#: 宏：`{{user}}` / `{{char}}` / `{{getvar::x}}` 等
MACRO_PATTERN = re.compile(r'\{\{[^{}]{1,80}\}\}')

#: HTML 标签
HTML_TAG_PATTERN = re.compile(r'<(/?)([A-Za-z][A-Za-z0-9-]{0,20})\b[^>]{0,400}(/?)>')

#: 空元素（不需要配平）
VOID_TAGS = frozenset({
    'br', 'hr', 'img', 'input', 'meta', 'link', 'area', 'base',
    'col', 'embed', 'source', 'track', 'wbr',
})

#: 引号内的专有名词（中英文引号都算）
QUOTED_PATTERN = re.compile(r'[「『"\u201c]([^」』"\u201d\n]{2,30})[」』"\u201d]')

#: 大写英文词（≥2 字母，全大写或首字母大写+全大写缩写）
PROPER_NOUN_PATTERN = re.compile(r'\b([A-Z][A-Za-z0-9]{1,19})\b')

#: 数字 + 常见单位（防止丢数值设定）
NUMBER_UNIT_PATTERN = re.compile(
    r'(\d+(?:\.\d+)?)\s*(%|级|层|米|公里|千米|岁|年|月|日|小时|分钟|秒|'
    r'kg|km|cm|mm|hp|mp|HP|MP)',
)

#: 调度字段：程序读的，必须原样带回
SCHEDULE_FIELDS = (
    'constant', 'vectorized', 'selective', 'selectiveLogic',
    'insertion_order', 'depth', 'probability', 'useProbability',
    'position', 'role', 'group', 'disable', 'enabled',
)

#: 长度比允许区间
MIN_LENGTH_RATIO = 0.30
MAX_LENGTH_RATIO = 0.95

#: 压缩目标：新文本至多是原文的 95% token
COMPRESS_TARGET = 0.95

MAX_REPORTED_FACTS = 20


def _macros(text) -> set:
    return {match.group(0) for match in MACRO_PATTERN.finditer('' if text is None else str(text))}


def html_balance(text) -> tuple:
    """检查 HTML 标签配平，返回 `(ok, message)`。

    只报**结构错**（未闭合 / 错配 / 多余闭合），不报「用了哪些标签」。
    """
    source = '' if text is None else str(text)
    stack = []

    for match in HTML_TAG_PATTERN.finditer(source):
        closing, name, self_closing = match.group(1), match.group(2), match.group(3)
        lowered = name.lower()

        if lowered in VOID_TAGS or self_closing:
            continue

        if closing:
            if not stack:
                return False, f'多余的闭合标签 </{lowered}>'
            expected = stack.pop()
            if expected != lowered:
                return False, f'标签错配：<{expected}> 被 </{lowered}> 关闭'
        else:
            stack.append(lowered)

    if stack:
        return False, f'未闭合的标签：<{stack[-1]}>' + (
            f'（还有 {len(stack) - 1} 个）' if len(stack) > 1 else ''
        )
    return True, ''


def _proper_nouns(text) -> list:
    """抽取「专有名词」候选：引号内词、大写英文词、数字+单位。"""
    source = '' if text is None else str(text)
    result = []
    seen = set()

    for pattern in (QUOTED_PATTERN, PROPER_NOUN_PATTERN):
        for match in pattern.finditer(source):
            word = match.group(1).strip()
            if word and word not in seen:
                seen.add(word)
                result.append(word)

    for match in NUMBER_UNIT_PATTERN.finditer(source):
        word = f'{match.group(1)}{match.group(2)}'
        if word not in seen:
            seen.add(word)
            result.append(word)

    return result


def _numbers(text) -> set:
    source = '' if text is None else str(text)
    return {f'{m.group(1)}{m.group(2)}' for m in NUMBER_UNIT_PATTERN.finditer(source)}


def validate_rewrite(original, candidate, *, original_entry=None, candidate_entry=None) -> dict:
    """校验一次世界书条目改写。

    Args:
        original: 原条目正文。
        candidate: 改写后的正文。
        original_entry / candidate_entry: 完整条目对象（用于比对调度字段）。

    Returns:
        `{'ok', 'issues', 'warnings', 'dropped_facts', 'metrics'}`
        —— `ok=False` 表示这次改写**不该写回**（内容被破坏），
        而 `warnings` 里的项只是提示（仍可写回，由人确认）。
    """
    src = '' if original is None else str(original)
    dst = '' if candidate is None else str(candidate)

    issues = []
    warnings = []

    # ---- 1. 宏保真（硬）----
    lost_macros = sorted(_macros(src) - _macros(dst))
    if lost_macros:
        issues.append(f'丢失宏: {"、".join(lost_macros[:5])}')

    new_macros = sorted(_macros(dst) - _macros(src))
    if new_macros:
        warnings.append(f'新增了原文没有的宏: {"、".join(new_macros[:5])}')

    # ---- 2. HTML 配平（硬）----
    balanced, balance_message = html_balance(dst)
    if not balanced:
        issues.append(f'HTML 未配平: {balance_message}')

    # 原文本来就不配平的话，不因「延续原状」而判失败
    if balanced and not html_balance(src)[0]:
        warnings.append('原文 HTML 本来就不配平，改写后已配平')

    # ---- 3. 压缩有效（硬：这是本能力的目的）----
    src_tokens = estimate(src)
    dst_tokens = estimate(dst)
    ratio = (dst_tokens / src_tokens) if src_tokens else 0.0

    if src_tokens and dst_tokens > src_tokens * COMPRESS_TARGET:
        issues.append(
            f'没有压缩效果：{dst_tokens} token vs 原文 {src_tokens} token'
            f'（需 ≤ {int(src_tokens * COMPRESS_TARGET)}）',
        )

    # ---- 4. 长度比（软警告 + 硬下限）----
    src_len = len(src)
    dst_len = len(dst)
    length_ratio = (dst_len / src_len) if src_len else 0.0

    if src_len and length_ratio < MIN_LENGTH_RATIO:
        issues.append(
            f'压缩过度：只剩原文的 {length_ratio:.0%}'
            f'（下限 {MIN_LENGTH_RATIO:.0%}），疑似丢失设定',
        )
    elif src_len and length_ratio > MAX_LENGTH_RATIO:
        warnings.append(f'压缩比偏高：仍有原文的 {length_ratio:.0%}')

    # ---- 5. 专有名词覆盖（软：缺失项供人确认）----
    dropped = []
    dst_lowered = dst.lower()
    for noun in _proper_nouns(src):
        # 数字+单位的判定要精确到「同值」；其它按子串
        if noun in _numbers(src) - _numbers(dst):
            dropped.append(noun)
            continue
        if noun.lower() not in dst_lowered:
            dropped.append(noun)

    dropped = dropped[:MAX_REPORTED_FACTS]
    if dropped:
        warnings.append(f'可能丢失的专有名词: {"、".join(dropped)}')

    # ---- 6. 调度字段不变（硬）----
    if isinstance(original_entry, dict) and isinstance(candidate_entry, dict):
        for field in SCHEDULE_FIELDS:
            if field not in original_entry:
                continue
            before = original_entry.get(field)
            after = candidate_entry.get(field)
            if before != after:
                issues.append(f'调度字段被改动: {field}（{before!r} → {after!r}）')

    return {
        'ok': not issues,
        'issues': issues,
        'warnings': warnings,
        'dropped_facts': dropped,
        'metrics': {
            'original_tokens': src_tokens,
            'candidate_tokens': dst_tokens,
            'token_ratio': round(ratio, 4),
            'original_chars': src_len,
            'candidate_chars': dst_len,
            'length_ratio': round(length_ratio, 4),
        },
    }


def build_rewrite_instruction(target_format: str = 'yaml') -> str:
    """生成「怎么改写」的要求文本（拼进 system 或任务说明）。

    **只是提示词约束，不做格式校验** —— 见模块 docstring。
    YAML 用 block scalar 包裹多行文本，一次性消除转义问题。
    """
    if str(target_format or '').strip().lower() != 'yaml':
        return (
            '输出要求：\n'
            '1. 只输出改写后的正文，不要解释、前言或 Markdown 代码块。\n'
            '2. 保留全部 {{…}} 宏、HTML 标签结构与所有专有名词、数值设定。\n'
            '3. 去掉重复表述与空话，使内容明显短于原文。'
        )

    return (
        '输出要求：\n'
        '1. 用 YAML 组织正文，便于后续阅读与复用。\n'
        '2. **所有多行文本一律用 block scalar（`|-`）包裹**，'
        '这样正文里的 `:`、`#`、行首 `- `、`---` 都不需要转义。\n'
        '3. 保留全部 {{…}} 宏、HTML 标签结构与所有专有名词、数值设定。\n'
        '4. 去掉重复表述与空话，使内容明显短于原文。\n'
        '5. 只输出 YAML 正文，不要加 Markdown 代码块包裹，也不要解释。'
    )
