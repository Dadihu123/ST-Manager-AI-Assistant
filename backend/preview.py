"""提示词渲染预览与成本估算（HTTP 路由与 CLI 共用）。

预览是「先看再跑」的落点（设计文档 §5.6）：不消耗 API 配额、不写任何状态，
只把最终 messages、变量表、世界书取样结果与 token 估算摊开给用户。
"""

import logging

from .core import hashing
from .core import prompt as prompt_core
from .core import tokens
from .core import trimming
from .core.errors import CODE_CARD_UNREADABLE, CODE_INVALID_PAYLOAD, AiAssistantError
from .jobs import (
    DEFAULT_CONTEXT_BUDGET,
    MIN_WI_BUDGET,
    read_tag_library,
    tag_usage,
)

logger = logging.getLogger(__name__)

VARIABLE_PREVIEW_LIMIT = 200


def embedded_entries(card) -> list:
    """取卡片内嵌世界书条目（兼容 V2 数组与 V3 对象）。"""
    block = hashing.card_data_block(card)
    book = block.get('character_book')
    if isinstance(book, dict) and isinstance(book.get('entries'), list):
        return book['entries']
    if isinstance(book, list):
        return book
    return []


def worldbook_budget(config, budget_tokens: int = 0) -> int:
    """世界书部分的 token 预算。"""
    base = int(budget_tokens or DEFAULT_CONTEXT_BUDGET)
    ratio = float((config or {}).get('wi_excerpt_ratio') or 0.3)
    return max(MIN_WI_BUDGET, int(base * ratio))


def build_variables(*, api, config, card, profile, model_name: str,
                    budget_tokens: int = 0) -> dict:
    """组装变量表，并返回世界书取样结果。"""
    from . import config as config_module  # 局部导入避免循环

    limits = prompt_core.build_limits(config, profile)
    excerpt = trimming.select_wi_excerpt(
        embedded_entries(card),
        worldbook_budget(config, budget_tokens),
        keywords=config_module.wi_keywords(config),
    )

    variables = prompt_core.collect_variables(
        card=card, excerpt=excerpt, tag_library=read_tag_library(api), usage=tag_usage(api),
        limits=limits, model_name=model_name, profile_id=str(profile.get('id') or ''),
    )

    return {'variables': variables, 'excerpt': excerpt, 'limits': limits}


def _summarize_variables(variables: dict) -> dict:
    summary = {}
    for key, value in (variables or {}).items():
        if isinstance(value, str) and len(value) > VARIABLE_PREVIEW_LIMIT:
            summary[key] = f'{value[:VARIABLE_PREVIEW_LIMIT]}…'
        else:
            summary[key] = value
    return summary


def build_preview(*, api, store, config, card_id: str, profile_id: str = '',
                  provider_id: str = '', model: str = '', budget_tokens: int = 0) -> dict:
    """构建预览结果；卡片不可读时抛 `card_unreadable`。"""
    identifier = str(card_id or '').strip()
    if not identifier:
        raise AiAssistantError(CODE_INVALID_PAYLOAD, '缺少 card_id')

    card = api.read_card(identifier)
    if not isinstance(card, dict):
        raise AiAssistantError(CODE_CARD_UNREADABLE, f'无法读取卡片: {identifier}', http_status=404)

    provider = store.resolve_provider(str(provider_id or ''))
    model_name = str(model or provider.get('model') or '')
    profile = store.resolve_profile(str(profile_id or ''), model_name)

    bundle = build_variables(
        api=api, config=config, card=card, profile=profile,
        model_name=model_name, budget_tokens=budget_tokens,
    )
    rendered = prompt_core.render_messages(profile, bundle['variables'], task='analyze')
    prompt_tokens = tokens.estimate_messages(rendered['messages'])

    completion_budget = int(profile.get('max_tokens') or 0)
    cost = tokens.estimate_cost(
        prompt_tokens['total'], completion_budget,
        price_per_1k_prompt=config.get('price_per_1k_prompt'),
        price_per_1k_completion=config.get('price_per_1k_completion'),
        currency=config.get('currency', 'CNY'),
    )

    excerpt = bundle['excerpt']

    return {
        'profile': {
            'id': profile.get('id'),
            'name': profile.get('name'),
            'version': profile.get('version'),
            'model_match': profile.get('model_match'),
            'builtin': profile.get('builtin'),
        },
        'provider': {'id': provider.get('id'), 'model': model_name},
        'messages': rendered['messages'],
        'variables': _summarize_variables(bundle['variables']),
        'missing': {
            'required': rendered['missing_required'],
            'optional': rendered['missing_optional'],
            'unknown': rendered['unknown'],
        },
        'limits': bundle['limits'],
        'worldbook': {
            'count': excerpt.get('count', 0),
            'total_tokens': excerpt.get('total_tokens', 0),
            'excerpt_tokens': excerpt.get('excerpt_tokens', 0),
            'truncated': excerpt.get('truncated', False),
            'selected': excerpt.get('selected', []),
            'dropped': excerpt.get('dropped', []),
        },
        'tokens': {
            'system': prompt_tokens['by_role'].get('system', 0),
            'user': prompt_tokens['by_role'].get('user', 0),
            'total': prompt_tokens['total'],
            'max_completion': completion_budget,
        },
        'cost': cost,
    }


def estimate_run(*, api, config, cards, profile, total: int, budget_tokens: int = 0) -> dict:
    """抽样估算整批作业的用量：对前几张卡真实渲染一次，按平均外推。"""
    samples = [card for card in (cards or []) if isinstance(card, dict)]
    if not samples or total <= 0:
        return {'sampled': 0, 'prompt_tokens': 0, 'completion_tokens': 0, 'cost': None}

    prompt_total = 0
    for card in samples:
        bundle = build_variables(
            api=api, config=config, card=card, profile=profile,
            model_name='', budget_tokens=budget_tokens,
        )
        rendered = prompt_core.render_messages(profile, bundle['variables'], task='analyze')
        prompt_total += tokens.estimate_messages(rendered['messages'])['total']

    sampled = len(samples)
    average_prompt = int(prompt_total / sampled)
    completion_budget = int(profile.get('max_tokens') or 0)

    prompt_tokens = average_prompt * total
    completion_tokens = min(completion_budget, 512) * total

    cost = tokens.estimate_cost(
        prompt_tokens, completion_tokens,
        price_per_1k_prompt=config.get('price_per_1k_prompt'),
        price_per_1k_completion=config.get('price_per_1k_completion'),
        currency=config.get('currency', 'CNY'),
    )

    return {
        'sampled': sampled,
        'average_prompt_tokens': average_prompt,
        'prompt_tokens': prompt_tokens,
        'completion_tokens': completion_tokens,
        'cost': cost,
    }
