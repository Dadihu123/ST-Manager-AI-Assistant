"""LLM 输出的容错解析与后置校验。

纯函数层。对应设计文档 §5.5 / §7.5。

设计立场：**先挽救、再判失败**。真实模型会输出代码块包裹、前后废话、
单对象 / 裸数组 / `{"results": [...]}` 三种形态、字符串数组形式的标签，
这些都必须能解析；只有完全无法解析时才判 `parse_failed`。
"""

import json
import re

THINK_PATTERNS = (
    re.compile(r'<think(?:ing)?\b[^>]*>.*?</think(?:ing)?>', re.DOTALL | re.IGNORECASE),
    re.compile(r'<reasoning\b[^>]*>.*?</reasoning>', re.DOTALL | re.IGNORECASE),
    re.compile(r'<analysis\b[^>]*>.*?</analysis>', re.DOTALL | re.IGNORECASE),
)
UNCLOSED_THINK_PATTERN = re.compile(r'<think(?:ing)?\b[^>]*>.*$', re.DOTALL | re.IGNORECASE)
FENCE_PATTERN = re.compile(r'```[ \t]*[A-Za-z0-9_+-]*[ \t]*\r?\n(.*?)```', re.DOTALL)
SENTENCE_ENDINGS = '。！？!?；;\n'

MAX_TAGS = 32
MAX_SUMMARY_CHARS = 4000
MAX_FLAGS = 8


# ------------------------------------------------------------------ 文本清洗


def strip_think(text) -> str:
    """剥离思考段（`<think>` / `<thinking>` / `<reasoning>` / `<analysis>`）。"""
    source = '' if text is None else str(text)
    if not source:
        return ''

    result = source
    for pattern in THINK_PATTERNS:
        result = pattern.sub('', result)
    result = UNCLOSED_THINK_PATTERN.sub('', result)
    return result.strip()


def strip_fences(text) -> tuple:
    """剥离 Markdown 代码块围栏。

    Returns:
        `(text, stripped)` —— `stripped=True` 表示确实剥掉了围栏（用于 warning）。
    """
    source = '' if text is None else str(text)
    stripped = False

    def _replace(match):
        nonlocal stripped
        stripped = True
        return match.group(1)

    result = FENCE_PATTERN.sub(_replace, source)
    return result.strip(), stripped


def extract_balanced(text, open_char: str = '{', close_char: str = '}') -> str:
    """提取第一段括号平衡的片段（字符串与转义感知）；找不到返回空串。"""
    source = '' if text is None else str(text)
    start = source.find(open_char)
    if start < 0:
        return ''

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(source)):
        char = source[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue
        if char == open_char:
            depth += 1
            continue
        if char == close_char:
            depth -= 1
            if depth == 0:
                return source[start:index + 1]

    return ''


def loads_lenient(text) -> tuple:
    """尽最大努力把文本解析成 JSON。

    Returns:
        `(data, strategy)`；无法解析时抛 `ValueError`。

    strategy 取值：`direct` / `brace` / `bracket` / `fence_brace` / `fence_bracket`。
    """
    source = '' if text is None else str(text).strip()
    if not source:
        raise ValueError('空响应')

    try:
        return json.loads(source), 'direct'
    except (ValueError, TypeError):
        pass

    # 先尝试「最先出现的那种括号」，避免把 `[{"a": 1}]` 里的对象误当成整体结果
    brace_index = source.find('{')
    bracket_index = source.find('[')

    if brace_index >= 0 and (bracket_index < 0 or brace_index < bracket_index):
        order = (('{', '}', 'brace'), ('[', ']', 'bracket'))
    else:
        order = (('[', ']', 'bracket'), ('{', '}', 'brace'))

    for open_char, close_char, label in order:
        candidate = extract_balanced(source, open_char, close_char)
        if not candidate:
            continue
        try:
            return json.loads(candidate), label
        except (ValueError, TypeError):
            continue

    raise ValueError('无法从响应中解析出 JSON')


# ------------------------------------------------------------------ 归一化


def _to_text(value, limit: int = MAX_SUMMARY_CHARS) -> str:
    if value is None:
        return ''
    if isinstance(value, (dict, list)):
        try:
            value = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            value = str(value)
    text = str(value).strip()
    if len(text) > limit:
        text = text[:limit]
    return text


def _to_confidence(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1 and number <= 100:
        number = number / 100.0
    if number < 0:
        return None
    return round(min(number, 1.0), 4)


def normalize_tag_list(raw) -> tuple:
    """归一化标签列表。

    支持三种形态：`["a", "b"]`、`[{"name": "a", "confidence": 0.9}]`、
    `[{"tag": "a", "score": 0.9}]`。

    Returns:
        `(tags, warnings)`，tags 为 `[{"name", "confidence"}]`（按 name 去重，保留最高置信度）。
    """
    warnings = []
    items = raw if isinstance(raw, list) else ([] if raw in (None, '') else [raw])

    collected = {}
    order = []

    for item in items:
        name = ''
        confidence = None

        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get('name') or item.get('tag') or item.get('label') or '').strip()
            confidence = _to_confidence(item.get('confidence', item.get('score')))
        elif item is not None:
            name = str(item).strip()
            warnings.append(f'标签项格式异常，已按文本处理: {name[:24]}')

        if not name:
            continue
        if len(name) > 64:
            name = name[:64]
            warnings.append('标签名超过 64 字符，已截断')

        if name not in collected:
            collected[name] = confidence
            order.append(name)
        elif confidence is not None:
            current = collected.get(name)
            if current is None or confidence > current:
                collected[name] = confidence

    tags = [{'name': name, 'confidence': collected.get(name)} for name in order]
    return tags, warnings


def normalize_new_tags(raw) -> tuple:
    """归一化新标签建议：`[{"name", "reason"}]`。"""
    warnings = []
    items = raw if isinstance(raw, list) else ([] if raw in (None, '') else [raw])

    result = []
    seen = set()
    for item in items:
        if isinstance(item, str):
            name, reason = item.strip(), ''
        elif isinstance(item, dict):
            name = str(item.get('name') or item.get('tag') or '').strip()
            reason = _to_text(item.get('reason') or item.get('why') or '', limit=200)
        else:
            continue

        if not name or name in seen:
            continue
        seen.add(name)
        result.append({'name': name[:64], 'reason': reason})

    if len(result) > MAX_TAGS:
        warnings.append(f'新标签建议超过 {MAX_TAGS} 条，已截断')
        result = result[:MAX_TAGS]

    return result, warnings


def _pick_payload(data):
    """从三种形态里取出「单卡结果」对象。

    - 单对象 → 直接用
    - `{"results": [...]}` → 取第一项（并标记 batch）
    - 裸数组 → 取第一项（并标记 batch）
    """
    if isinstance(data, dict):
        for key in ('results', 'items', 'data', 'cards'):
            value = data.get(key)
            if isinstance(value, list) and value:
                first = value[0]
                if isinstance(first, dict):
                    return first, True
        return data, False

    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            return first, True

    return {}, False


def normalize_result(data, output_schema=None) -> dict:
    """把任意解析结果归一化为统一结构。

    Args:
        output_schema: 声明了哪些字段。**schema 里声明、但内置归一化不认识的自定义
            字段会原样透传** —— 否则 task spec 新增字段（如 `card_type` 的枚举、
            `new_content`）会在归一化阶段被静默丢弃，永远取不到值。
    """
    payload, is_batch = _pick_payload(data)

    tags, tag_warnings = normalize_tag_list(payload.get('tags'))
    new_tags, new_tag_warnings = normalize_new_tags(
        payload.get('suggested_new_tags') or payload.get('new_tags')
    )

    flags = payload.get('content_flags') or []
    if isinstance(flags, str):
        flags = [part.strip() for part in flags.replace('，', ',').split(',') if part.strip()]
    if not isinstance(flags, list):
        flags = []
    flags = [str(item).strip()[:32] for item in flags if str(item or '').strip()][:MAX_FLAGS]

    warnings = tag_warnings + new_tag_warnings
    if is_batch:
        warnings.append('响应包含多项结果，已取第一项')

    result = {
        'summary': _to_text(payload.get('summary')),
        'tags': tags,
        'suggested_new_tags': new_tags,
        'card_type': _to_text(payload.get('card_type'), limit=32),
        'content_flags': flags,
        'language': _to_text(payload.get('language'), limit=16),
        'quality_notes': _to_text(payload.get('quality_notes'), limit=500),
        '_warnings': warnings,
    }

    # schema 声明的自定义字段透传（内置归一化不认识的键）
    if isinstance(output_schema, dict):
        declared = output_schema.get('properties')
        if isinstance(declared, dict):
            for name in declared:
                key = str(name or '').strip()
                if key and key not in result and key in payload:
                    result[key] = payload[key]

    return result


# ------------------------------------------------------------------ 后置校验


def truncate_text(text, limit: int) -> str:
    """把文本裁剪到 `limit` 以内：优先在句末标点处断开。"""
    source = '' if text is None else str(text)
    if limit <= 0 or len(source) <= limit:
        return source

    window = source[:limit]
    best = -1
    for index, char in enumerate(window):
        if char in SENTENCE_ENDINGS:
            best = index

    if best >= max(8, limit // 3):
        return window[:best + 1].strip()
    # 硬截断：为省略号留出一格，保证结果长度不超过 limit
    return window.rstrip()[:max(1, limit - 1)].rstrip() + '…'


def validate_result(result, output_schema=None, *, strict: bool = False) -> dict:
    """对归一化结果做后置校验与挽救。

    规则来自 task spec 的 `output_schema`（设计文档 §8.5）：
    「生成约束」由 `schema.describe_constraints` 拼进 system，
    「后置校验」由本函数逐条核对 —— 两者读同一份声明，不会漂移。

    Returns:
        `{'result', 'issues', 'warnings', 'ok'}` —— `ok=False` 表示 strict 模式下判失败。
    """
    data = result if isinstance(result, dict) else {}

    # 延迟导入：`schema` 模块依赖本模块的 `truncate_text`，模块级导入会成环
    from . import schema as schema_core  # noqa: PLC0415

    spec = schema_core.normalize_schema(output_schema)

    # tags / suggested_new_tags 允许传入「未归一化」的形态（字符串数组等），此处再做一次幂等归一化
    tags_normalized, tag_warnings = normalize_tag_list(data.get('tags'))
    new_tags_normalized, new_tag_warnings = normalize_new_tags(data.get('suggested_new_tags'))
    warnings = list(data.get('_warnings') or []) + tag_warnings + new_tag_warnings

    normalized = {
        'summary': str(data.get('summary') or '').strip(),
        'tags': tags_normalized,
        'suggested_new_tags': new_tags_normalized,
        'card_type': str(data.get('card_type') or '').strip(),
        'content_flags': list(data.get('content_flags') or []),
        'language': str(data.get('language') or '').strip(),
        'quality_notes': str(data.get('quality_notes') or '').strip(),
    }

    # schema 声明的自定义字段：从入参透传（内置归一化不认识它们）
    for name in spec['properties']:
        if name not in normalized and name in data:
            normalized[name] = data[name]

    # ---- 置信度下限：先筛掉低分的（在数量截断之前，否则会把低分项也算进上限）----
    confidence_min = schema_core.confidence_floor(spec, 'tags')
    if confidence_min:
        kept = []
        dropped_low = []
        for item in normalized['tags']:
            name = str(item.get('name') or '').strip()
            if not name:
                continue
            confidence = item.get('confidence')
            if confidence is None:
                warnings.append(f'标签「{name}」缺少置信度，已保留')
                kept.append({'name': name, 'confidence': None})
                continue
            if float(confidence) < confidence_min:
                dropped_low.append(name)
                continue
            kept.append({'name': name, 'confidence': confidence})
        if dropped_low:
            warnings.append(
                f'已丢弃 {len(dropped_low)} 个低置信度标签: {"、".join(dropped_low[:5])}',
            )
        normalized['tags'] = kept

    # ---- 超量标签按置信度保留（`order_by` 声明了排序字段才排）----
    tags_prop = spec['properties'].get('tags') or {}
    tags_max = int(tags_prop.get('max_items') or 0)
    if tags_max and len(normalized['tags']) > tags_max:
        if tags_prop.get('order_by') == 'confidence':
            normalized['tags'].sort(
                key=lambda item: (item.get('confidence') is None,
                                  -(item.get('confidence') or 0)),
            )
        warnings.append(f'标签超过 {tags_max} 个，已截断')
        normalized['tags'] = normalized['tags'][:tags_max]

    # ---- 与现有标签重复的新标签建议 ----
    existing = {str(item.get('name') or '').strip() for item in normalized['tags']}
    normalized['suggested_new_tags'] = [
        item for item in normalized['suggested_new_tags']
        if str(item.get('name') or '').strip() not in existing
    ]

    # ---- 逐字段按 schema 校验（字数、枚举值域、数组长度、必填）----
    # schema 声明了但归一化没产出的字段补成空值，否则 `schema.validate` 的
    # `if name not in values: continue` 会把它们整段跳过 —— 枚举越界就查不出来。
    for name in spec['properties']:
        normalized.setdefault(name, '')

    checked = schema_core.validate(normalized, spec, strict=strict)

    return {
        'result': checked['values'],
        'issues': checked['issues'],
        'warnings': warnings + checked['warnings'],
        'ok': checked['ok'],
    }


# ------------------------------------------------------------------ 组合入口


def parse_llm_result(text, output_schema=None, *, strict: bool = False,
                     prefill: str = '') -> dict:
    """完整解析链路：清洗 →（拼回预填充）→ 解析 → 归一化 → 校验。

    Args:
        text: 模型返回的 `content`。
        prefill: 本次请求使用的 `assistant_prefill`。

    **为什么需要 `prefill`**：预填充的语义是「模型从这里续写」，所以返回的
    `content` **不含**预填充那一段。例如发出 `{role:'assistant', content:'{'}`，
    模型返回的是 `'"summary": "…", "tags": [...]}'`（没有开头的 `{`）。

    不拼回会发生什么（已实测）：`loads_lenient` 找不到 `{`，退而按 `[` 提取，
    命中 `tags` 数组 —— 那是**合法 JSON**，于是解析「成功」，
    但 `summary` 与 `tags` 全空且 `ok=True`。**静默丢数据**。

    拼回顺序不可反：shujuku 的实测记录指出，对已完整的原文再拼 `prefill`
    必然括号不配平，会被当成截断抢救出一份空载荷。所以这里是
    **原文优先、拼接兜底**。

    Returns:
        `{'ok', 'result', 'issues', 'warnings', 'strategy', 'raw_excerpt'}`。
        解析彻底失败时 `ok=False`，`issues` 内含原因，`raw_excerpt` 为响应片段（≤2KB）。
    """
    from . import schema as schema_core  # noqa: PLC0415 - 循环导入：schema 依赖本模块

    rules = schema_core.normalize_schema(output_schema)
    source = '' if text is None else str(text)
    excerpt = source[:2048]

    cleaned = source
    if rules.get('strip_think', True):
        cleaned = strip_think(cleaned)

    cleaned, fenced = strip_fences(cleaned)

    warnings = []
    if fenced:
        warnings.append('响应包含 Markdown 代码块包裹，已剥离')

    # 候选顺序：**先判断原文是否已完整**，再决定先后。
    #
    # 不能简单地「原文优先」——续写片段往往也能被 `loads_lenient` 「解析成功」
    # （例如片段里有 `tags` 数组，退去按 `[` 提取就成了合法 JSON），
    # 于是原文优先会抢先命中一个**退化结果**，永远走不到拼接。
    #
    # 判据与 shujuku 一致（`agent-protocol.ts:182`）：剥掉围栏后以 `{` / `[`
    # 开头，视为模型完整重输；否则视为续写预填充。
    prefix = '' if prefill is None else str(prefill)
    stripped_head = cleaned.lstrip()
    looks_complete = stripped_head.startswith(('{', '['))

    candidates = [(cleaned, '')]
    if prefix and not looks_complete:
        # 续写形态：拼接在前，原文兜底
        candidates = [(f'{prefix}{cleaned}', 'prefill'), (cleaned, '')]

    data = None
    strategy = ''
    last_error = None
    stitched = False

    for candidate, tag in candidates:
        try:
            data, strategy = loads_lenient(candidate)
        except ValueError as exc:
            last_error = exc
            continue
        if tag == 'prefill':
            stitched = True
        break

    if data is None:
        return {
            'ok': False,
            'result': None,
            'issues': [f'无法解析响应: {last_error}'],
            'warnings': warnings,
            'strategy': '',
            'raw_excerpt': excerpt,
        }

    if stitched:
        warnings.append('模型响应续写了预填充，已在解析前拼回')

    normalized = normalize_result(data, rules)
    validation = validate_result(normalized, output_schema, strict=strict)

    return {
        'ok': bool(validation['ok']),
        'result': validation['result'],
        'issues': list(validation['issues']),
        'warnings': warnings + list(validation['warnings']),
        'strategy': strategy,
        'raw_excerpt': excerpt if not validation['ok'] else '',
    }
