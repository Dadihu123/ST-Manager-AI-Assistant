"""输出 schema：一份声明同时驱动「生成约束」与「后置校验」。

**为什么要独立成模块**（设计文档 §8.1 / §8.5）：

task spec 要回答四件事，其中「答什么形状」原先硬编码在
`parsing.normalize_result` / `validate_result`，而「怎么要求模型这样答」又硬编码在
`prompt.format_instruction`。两处各写一遍，必然漂移 —— 改了字数上限却忘了改校验，
模型就永远被判失败。

本模块把这件事收敛成 **一份 schema**：

- `describe_constraints()` → 拼进 system 的文字（告诉模型要什么）
- `validate()` → 逐条核对（检查模型给了什么）

两者读同一份声明，天然同源。

**只做一层对象 + 数组 + 标量约束**，不实现完整 JSON Schema（§8.5）：
这覆盖 95% 的场景，且能复用 `parsing` 的容错链。

schema 形状（字段级键都能省略）：

```jsonc
{
  "strip_think": true,          // 清洗：剥离 <think> 块
  "json_only": true,            // 清洗/要求：只输出 JSON
  "forbidden_wrappers": ["```json"],
  "required": ["summary", "tags"],
  "properties": {
    "summary": {"type": "string", "min_chars": 0, "max_chars": 300},
    "tags":    {"type": "array", "max_items": 8, "min_confidence": 0.5,
                "item": {"name": {"type": "string"},
                         "confidence": {"type": "number", "min": 0, "max": 1}}},
    "card_type": {"type": "enum", "values": ["OC", "同人"]}
  }
}
```

类型：`string` / `number` / `array` / `enum` / `object`。
"""

#: 支持的字段类型
TYPES = ('string', 'number', 'array', 'enum', 'object')

#: 全局限值（防止坏 schema 把内存吃满）
MAX_ITEMS_LIMIT = 200


def _truncate(text, limit: int) -> str:
    """裁剪文本（实现在 `parsing`；延迟导入以避开模块级循环）。"""
    from .parsing import truncate_text  # noqa: PLC0415 - 循环导入：parsing 依赖本模块

    return truncate_text(text, limit)


def _to_int(value, default=0, *, minimum=0, maximum=None):
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def _to_float(value, default=0.0, *, minimum=None, maximum=None):
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


def normalize_property(raw) -> dict:
    """规范化单个字段声明（坏值回落最宽松的 string）。"""
    data = raw if isinstance(raw, dict) else {}
    kind = str(data.get('type') or 'string').strip().lower()
    if kind not in TYPES:
        kind = 'string'

    spec = {'type': kind}

    if kind == 'string':
        spec['min_chars'] = _to_int(data.get('min_chars'), 0, minimum=0)
        spec['max_chars'] = _to_int(data.get('max_chars'), 0, minimum=0)
        if 'trim' in data:
            spec['trim'] = bool(data.get('trim'))

    elif kind == 'number':
        spec['min'] = _to_float(data.get('min'), 0.0)
        if data.get('max') is not None:
            spec['max'] = _to_float(data.get('max'), 0.0)

    elif kind == 'enum':
        values = data.get('values')
        if isinstance(values, (list, tuple)):
            spec['values'] = [str(item).strip() for item in values if str(item or '').strip()]
        else:
            spec['values'] = []
        # 不在值域内时：`keep` 保留原值 / `empty` 清空
        fallback = str(data.get('fallback') or 'keep').strip().lower()
        spec['fallback'] = fallback if fallback in ('keep', 'empty') else 'keep'

    elif kind == 'array':
        spec['min_items'] = _to_int(data.get('min_items'), 0, minimum=0)
        spec['max_items'] = _to_int(
            data.get('max_items'), 0, minimum=0, maximum=MAX_ITEMS_LIMIT,
        )
        item = data.get('item')
        if isinstance(item, dict) and item:
            spec['item'] = {
                str(key): normalize_property(value)
                for key, value in item.items()
                if isinstance(value, dict)
            }
        # 数组内对象的某个数值字段的下限（现有语义：标签置信度下限）
        confidence = data.get('min_confidence')
        if confidence is not None:
            spec['min_confidence'] = _to_float(confidence, 0.0, minimum=0.0, maximum=1.0)
        # 按某字段降序截断（现有语义：超量标签按置信度保留）
        order_by = data.get('order_by')
        if isinstance(order_by, str) and order_by.strip():
            spec['order_by'] = order_by.strip()

    elif kind == 'object':
        item = data.get('item') or data.get('properties')
        if isinstance(item, dict) and item:
            spec['item'] = {
                str(key): normalize_property(value)
                for key, value in item.items()
                if isinstance(value, dict)
            }

    return spec


def normalize_schema(raw) -> dict:
    """把任意输入收敛为合法 schema（缺字段补默认，坏值回落）。"""
    data = raw if isinstance(raw, dict) else {}

    properties = {}
    raw_props = data.get('properties')
    if isinstance(raw_props, dict):
        for name, value in raw_props.items():
            key = str(name or '').strip()
            if key:
                properties[key] = normalize_property(value)

    required = []
    raw_required = data.get('required')
    if isinstance(raw_required, (list, tuple)):
        for item in raw_required:
            key = str(item or '').strip()
            if key and key not in required:
                required.append(key)

    wrappers = []
    raw_wrappers = data.get('forbidden_wrappers')
    if isinstance(raw_wrappers, (list, tuple)):
        wrappers = [str(item).strip() for item in raw_wrappers if str(item or '').strip()]

    return {
        'strip_think': bool(data.get('strip_think', True)),
        'json_only': bool(data.get('json_only', True)),
        'forbidden_wrappers': wrappers,
        'required': required,
        'properties': properties,
    }


# ------------------------------------------------------------------ 生成约束


def _describe_property(name: str, spec: dict) -> list:
    """把一个字段声明翻译成中文要求句。"""
    kind = spec.get('type')
    lines = []

    if kind == 'string':
        low = int(spec.get('min_chars') or 0)
        high = int(spec.get('max_chars') or 0)
        if high and low:
            lines.append(f'{name} 为文本，{low}–{high} 字。')
        elif high:
            lines.append(f'{name} 为文本，不超过 {high} 字。')
        elif low:
            lines.append(f'{name} 为文本，不少于 {low} 字。')

    elif kind == 'enum':
        values = spec.get('values') or []
        if values:
            lines.append(f'{name} 只能取以下之一：{"、".join(values)}。')

    elif kind == 'array':
        low = int(spec.get('min_items') or 0)
        high = int(spec.get('max_items') or 0)
        item = spec.get('item') or {}
        keys = list(item)

        if keys:
            shape = '、'.join(keys)
            core = f'{name} 为对象数组，每项含 {shape}'
        else:
            core = f'{name} 为数组'

        if high and low:
            core += f'，{low}–{high} 项'
        elif high:
            core += f'，最多 {high} 项'
        elif low:
            core += f'，至少 {low} 项'
        lines.append(core + '。')

        confidence = spec.get('min_confidence')
        if confidence:
            lines.append(f'{name} 中每项的置信度不低于 {confidence}。')

    elif kind == 'object':
        keys = list(spec.get('item') or {})
        if keys:
            lines.append(f'{name} 为对象，含 {"、".join(keys)}。')

    return lines


def describe_constraints(schema) -> list:
    """把 schema 翻译成中文要求句（喂给 system）。

    Returns:
        字符串列表；调用方按顺序拼进 `format_instruction`。
    """
    spec = normalize_schema(schema)
    lines = []

    if spec['json_only']:
        lines.append('只输出一个 JSON 对象，不要输出解释、前言、思考过程或 Markdown 代码块。')

    wrappers = spec['forbidden_wrappers']
    if wrappers:
        lines.append(f'不要使用 {"、".join(wrappers)} 之类的包裹。')

    if spec['required']:
        lines.append(f'必需字段：{"、".join(spec["required"])}。')

    for name, prop in spec['properties'].items():
        lines.extend(_describe_property(name, prop))

    return lines


# ------------------------------------------------------------------ 后置校验


def _check_string(value, spec: dict, name: str) -> tuple:
    text = '' if value is None else str(value)
    if spec.get('trim', True):
        text = text.strip()

    warnings = []
    low = int(spec.get('min_chars') or 0)
    high = int(spec.get('max_chars') or 0)

    if text and high and len(text) > high:
        text = _truncate(text, high)
        warnings.append(f'{name} 超过 {high} 字，已裁剪')

    issues = []
    if low and len(text) < low:
        issues.append(f'{name} 短于下限 {low} 字（实际 {len(text)} 字）')

    return text, issues, warnings


def _check_enum(value, spec: dict, name: str) -> tuple:
    """枚举值域检查。

    与字数 / 数量不同，**越界不是「差一点」，而是「这个值根本不属于这个字段」**
    —— `card_type` 返回「宇宙飞船」时不能原样写进卡片。因此越界一律进 `issues`
    （由调用方决定 strict 与否都不该放过），只有 `fallback='empty'` 清空时才降级为提示。
    """
    text = '' if value is None else str(value).strip()
    allowed = spec.get('values') or []
    if not allowed or not text or text in allowed:
        return text, [], []

    message = f'{name} 的值「{text}」不在允许范围（{"、".join(allowed)}）内'
    if spec.get('fallback') == 'empty':
        # 已被清空 → 不是致命问题，仅提示
        return '', [], [message + '，已清空']
    return text, [message], []


def _check_array(value, spec: dict, name: str) -> tuple:
    items = value if isinstance(value, list) else ([] if value in (None, '') else [value])
    warnings = []

    low = int(spec.get('min_items') or 0)
    high = int(spec.get('max_items') or 0)

    if high and len(items) > high:
        items = items[:high]
        warnings.append(f'{name} 超过 {high} 项，已截断')

    issues = []
    if low and len(items) < low:
        issues.append(f'{name} 少 {low} 项（实际 {len(items)} 项）')

    return items, issues, warnings


def validate(value_map, schema, *, strict: bool = False) -> dict:
    """按 schema 校验并挽救一份归一化结果。

    Args:
        value_map: `{字段名: 值}`（已由 `parsing.normalize_result` 归一）。
        schema: 输出 schema。
        strict: True 时把「软问题」也升级为 `issues`（判失败）。

    Returns:
        `{'values', 'issues', 'warnings', 'ok'}`
    """
    spec = normalize_schema(schema)
    values = dict(value_map) if isinstance(value_map, dict) else {}

    issues = []
    warnings = []

    # ---- 必填 ----
    # 空数组视为「已提供但为空」（模型确实没找到），只有缺失/空串才判失败。
    for name in spec['required']:
        value = values.get(name)
        if value is None or value == '' or (isinstance(value, dict) and not value):
            issues.append(f'缺少必需字段: {name}')

    # ---- 逐字段 ----
    for name, prop in spec['properties'].items():
        if name not in values:
            continue

        kind = prop.get('type')
        current = values.get(name)

        if kind == 'string':
            current, field_issues, field_warnings = _check_string(current, prop, name)
            values[name] = current
        elif kind == 'enum':
            current, field_issues, field_warnings = _check_enum(current, prop, name)
            values[name] = current
        elif kind == 'array':
            current, field_issues, field_warnings = _check_array(current, prop, name)
            values[name] = current
        elif kind == 'number':
            current = _to_float(current, 0.0)
            values[name] = current
            field_issues, field_warnings = [], []
        else:
            field_issues, field_warnings = [], []

        warnings.extend(field_warnings)

        if kind == 'enum':
            # 枚举越界是硬违规（值根本不属于该字段），strict 与否都判问题
            issues.extend(field_issues)
        elif strict:
            issues.extend(field_issues)
        else:
            warnings.extend(field_issues)

    return {
        'values': values,
        'issues': issues,
        'warnings': warnings,
        'ok': not issues,
    }


def confidence_floor(schema, field: str = 'tags') -> float:
    """取数组字段的置信度下限（0 表示不筛）。"""
    prop = normalize_schema(schema)['properties'].get(field) or {}
    return float(prop.get('min_confidence') or 0.0)


def item_limits(schema, field: str = 'tags') -> tuple:
    """取数组字段的 `(min_items, max_items)`。"""
    prop = normalize_schema(schema)['properties'].get(field) or {}
    return int(prop.get('min_items') or 0), int(prop.get('max_items') or 0)
