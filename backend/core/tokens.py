"""Token 估算。

后端 `PluginAPI` **没有** token 估算能力（`estimateTokens()` 只存在于前端
`static/js/pluginHost.js:464-469`，见设计文档 C1），因此插件自带一份实现。

公式必须与主程序一致（`core/utils/text.py:41-44`）：
    ceil(CJK 字符数 + 非 CJK 字符数 / 3.5)
其中 CJK 指基本汉字区 `[\\u4e00-\\u9fa5]`（三处实现都只覆盖这一区间）。
"""

import math
import re

CJK_PATTERN = re.compile(r'[\u4e00-\u9fa5]')
NON_CJK_PER_TOKEN = 3.5


def estimate(text) -> int:
    """估算一段文本的 token 数；空文本返回 0。"""
    source = '' if text is None else str(text)
    if not source:
        return 0

    cjk_count = len(CJK_PATTERN.findall(source))
    other_count = len(source) - cjk_count
    return math.ceil(cjk_count + (other_count / NON_CJK_PER_TOKEN))


def estimate_messages(messages) -> dict:
    """估算一组 chat messages 的 token 数，按 role 汇总。

    Returns:
        `{'total': int, 'by_role': {role: int}, 'by_index': [int, ...]}`
    """
    by_role = {}
    by_index = []
    total = 0

    for item in messages or []:
        if not isinstance(item, dict):
            by_index.append(0)
            continue
        count = estimate(item.get('content'))
        role = str(item.get('role') or 'unknown')
        by_role[role] = by_role.get(role, 0) + count
        by_index.append(count)
        total += count

    return {'total': total, 'by_role': by_role, 'by_index': by_index}


def estimate_cost(prompt_tokens: int, completion_tokens: int, *,
                  price_per_1k_prompt: float = 0.0,
                  price_per_1k_completion: float = 0.0,
                  currency: str = 'CNY') -> dict:
    """按单价估算金额；单价为 0 时 `priced=False`（不假装知道价格）。"""
    prompt_price = float(price_per_1k_prompt or 0.0)
    completion_price = float(price_per_1k_completion or 0.0)
    priced = prompt_price > 0 or completion_price > 0

    prompt_cost = (int(prompt_tokens or 0) / 1000.0) * prompt_price
    completion_cost = (int(completion_tokens or 0) / 1000.0) * completion_price

    return {
        'prompt': round(prompt_cost, 6),
        'completion': round(completion_cost, 6),
        'total': round(prompt_cost + completion_cost, 6),
        'currency': str(currency or 'CNY'),
        'priced': priced,
    }


def estimate_blocks(variable_hits, variables, *, total_tokens: int = 0) -> dict:
    """按变量拆解 token 消耗（让用户看清「钱花在哪」）。

    Args:
        variable_hits: `{变量名: 出现次数}`（出现次数来自模板扫描）。
        variables: `{变量名: 值}`。
        total_tokens: 实际渲染后的总 token；用于反推模板骨架的开销。

    Returns:
        `{'items': [{'name','tokens','count','empty'}], 'by_variable': {...},
           'variables_total': int, 'template_overhead': int, 'total': int}`
    """
    hits = variable_hits if isinstance(variable_hits, dict) else {}
    values = variables if isinstance(variables, dict) else {}

    items = []
    by_variable = {}
    variables_total = 0

    for name, count in hits.items():
        occurrences = max(0, int(count or 0))
        if occurrences <= 0:
            continue
        raw = values.get(name)
        text = '' if raw is None else str(raw)
        unit = estimate(text)
        cost = unit * occurrences
        variables_total += cost
        by_variable[name] = cost
        items.append({
            'name': name,
            'tokens': cost,
            'count': occurrences,
            'empty': not text.strip(),
        })

    items.sort(key=lambda entry: (-entry['tokens'], entry['name']))

    total = max(0, int(total_tokens or 0))
    # 「骨架」= 模板里的固定文字 + 消息包装开销。可能为负（估算误差），夹到 0。
    overhead = max(0, total - variables_total)

    return {
        'items': items,
        'by_variable': by_variable,
        'variables_total': variables_total,
        'template_overhead': overhead,
        'total': total,
    }
