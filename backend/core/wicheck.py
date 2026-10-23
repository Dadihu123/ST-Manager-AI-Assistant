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

**两级判定**（设计文档 §6.3）——这是本模块最关键的语义：

| 级别 | 含义 | 落点 |
|---|---|---|
| `issues` | 内容被**破坏**（结构/宏/标签/数字/调度字段） | 默认不落地，但**不禁止人手动采纳** |
| `warnings` | 结果不理想但没坏（变长、没精简、删得偏多） | 永不拦，只显示 |

**「变长」是 note，「压坏」是 block。** 两者混在一起会让短条目全被判失败——
短条目本来最多只能省几个字，要求它「必须变短」等于逼模型扩写或加壳
（实测 53 条 yaml 候选里 39 条被加了「名称/关键词/原文」外壳，写回后进正文）。

**保留的硬校验**（都是「内容被破坏」而非「格式不合法」）：

| 规则 | 为什么 |
|---|---|
| 宏保真 | `{{…}}` 是**程序**读的注入机制，丢了就是功能性破坏 |
| HTML 配平（相对原文） | 卡片前端靠 HTML 渲染；但**原文本来就不配平**的条目，候选延续原状不算破坏 |
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

#: 改写后仍有原文 95% 以上 → 提示「精简不明显」（note，可正常落地）
MAX_LENGTH_RATIO = 0.95

#: 比原文短一半以上 → 提示「可能删多了」（note，可正常落地）。
#: 注意这是**提示**不是门限：压缩本来就是本能力的目的，压得狠不等于做坏了。
SHORT_NOTE_RATIO = 0.5

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

    # ---- 2. HTML 配平（硬，但只判「候选比原文更差」）----
    #
    # 实测教训：`[MVU] 变量更新规则`、`[核心] 中央统合系统` 两条**原文本身就不配平**
    # （`<pokemon_custom_start>`、`<system_guide>` 原本没闭合），候选延续了同样的
    # 不平衡状态，却被判失败。判据必须是「相对原文」，不是「绝对配平」。
    src_balanced = html_balance(src)[0]
    dst_balanced, balance_message = html_balance(dst)
    if src_balanced and not dst_balanced:
        issues.append(f'HTML 未配平: {balance_message}')
    elif dst_balanced and not src_balanced:
        warnings.append('原文 HTML 本来就不配平，改写后已配平')

    # ---- 3. 指标：只算不判（不设压缩门限）----
    src_tokens = estimate(src)
    dst_tokens = estimate(dst)
    ratio = (dst_tokens / src_tokens) if src_tokens else 0.0

    # ---- 4. 长度：两个方向都只是提示，永不拦 ----
    src_len = len(src)
    dst_len = len(dst)
    length_ratio = (dst_len / src_len) if src_len else 0.0

    if src_len and length_ratio > MAX_LENGTH_RATIO:
        # 持平或变长：如实显示数字即可，「不出现失败字样」（§6.4）
        warnings.append(
            f'精简不明显：改写后仍有原文的 {length_ratio:.0%}（可正常落地）',
        )
    elif src_len and length_ratio < SHORT_NOTE_RATIO:
        # 压得狠不等于做坏了：只提示人确认，不拦
        warnings.append(
            f'删得偏多：只剩原文的 {length_ratio:.0%}，建议确认有没有丢设定',
        )

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


#: 固定的「怎么输出」要求（设计文档 §0.2 v2 版，实测 8/8 合理）。
#:
#: 历史教训：这里曾经按 `target_format` 分 yaml / text 两支，yaml 支强制
#: 「所有多行文本一律用 block scalar 包裹」，结果模型给正文套了三层壳
#: （`名称:` / `关键词:` / `原文: |-`）。那层壳**没有任何地方会拆**——
#: `routes.py` 取 `candidate` 原样写进 `entry.content`。于是：
#:
#: - 校验侧：壳的 token 全算在压缩率上，短条目数学上不可能过；
#: - 落地侧：侥幸过了的，世界书正文变成一份 YAML 文档。
#:
#: 这就是「越简越胀」的全部来源。现在只描述意图，不给容器：
#: 「原文是什么形态就保持什么形态」，用户不用理解格式开关。
REWRITE_OUTPUT_RULE = (
    '输出要求：\n'
    '- 直接输出条目正文本身。不要前言，不要用 Markdown 代码块包裹，\n'
    '  不要加「名称：」「关键词：」「原文：」这类外层键。\n'
    '- 原文是「定义：解释 + 缩进」的层级结构时，沿用这种 YAML 风格——\n'
    '  它比 JSON/Markdown 省 token，人读起来也不差。\n'
    '- 原文是散文就还是散文，不要为了方便 YAML 而自己造键。\n'
    '- 原文是代码、模板、表格、INI 或机器解析的数据时，保持原样，不要转成 YAML。'
)


def build_rewrite_instruction() -> str:
    """生成「怎么改写」的要求文本（拼进 system 末尾）。

    只有一条固定规则，不再按格式分支 —— 格式意图已经写进规则本身，
    `target_format` 这个用户可选项目前的价值为零（选哪个都不是「用 YAML
    表达层级」），所以一并删掉（设计文档 §2.2）。
    """
    return REWRITE_OUTPUT_RULE
