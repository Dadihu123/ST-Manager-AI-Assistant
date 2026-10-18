"""提示词渲染预览与成本估算（HTTP 路由与 CLI 共用）。

预览是「先看再跑」的落点（设计文档 §5.6）：不消耗 API 配额、不写任何状态，
只把最终 messages、变量表、世界书取样结果与 token 估算摊开给用户。
"""

import logging

from .core import hashing
from .core import prompt as prompt_core
from .core import selector as selector_core
from .core import tagpool as tagpool_core
from .core import tokens
from .core import trimming
from .core.errors import CODE_CARD_UNREADABLE, CODE_INVALID_PAYLOAD, AiAssistantError
from .jobs import (
    DEFAULT_CONTEXT_BUDGET,
    MIN_WI_BUDGET,
    read_extensions,
    read_local_note,
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
                    budget_tokens: int = 0, card_id: str = '') -> dict:
    """组装变量表，并返回世界书取样结果与选择器摘要。

    选择器来自档案（`profile['content_selector']`）；`extensions` / `local_note`
    **只在选择器要求时才向主程序取**，避免无谓的 IO。
    """
    from . import config as config_module  # 局部导入避免循环

    spec = selector_core.normalize_selector(
        prompt_core.profile_selector(profile, config),
    )
    limits = prompt_core.build_limits(config, profile)

    entries = embedded_entries(card)
    wi_mode = spec['worldbook']['mode']
    if wi_mode == selector_core.WI_NONE:
        budget = 0
        only_uids = []
        pinned_uids = []
    else:
        budget = worldbook_budget(config, budget_tokens)
        if spec['worldbook']['budget_tokens']:
            budget = max(MIN_WI_BUDGET, min(budget, spec['worldbook']['budget_tokens']))
        only_uids = spec['worldbook']['entry_uids'] if wi_mode == selector_core.WI_SELECTED else []
        pinned_uids = spec['worldbook']['entry_uids'] if wi_mode != selector_core.WI_SELECTED else []

    excerpt = trimming.select_wi_excerpt(
        entries, budget,
        keywords=config_module.wi_keywords(config),
        pinned_uids=pinned_uids,
        only_uids=only_uids,
    )

    extensions = None
    if (selector_core.wants_extra(spec, selector_core.EXTRA_REGEX)
            or selector_core.wants_extra(spec, selector_core.EXTRA_ST_SCRIPTS)):
        extensions = read_extensions(api, card_id)

    local_note = None
    if selector_core.wants_extra(spec, selector_core.EXTRA_LOCAL_NOTE):
        local_note = read_local_note(api, card_id)

    # ---- 标签池：来源过滤 + 预算 + 相关性排序 ----
    tag_library = read_tag_library(api)
    usage_map = tag_usage(api)
    pool_spec = prompt_core.profile_tag_pool(profile, config)
    pool_result = tagpool_core.build_pool_text(
        tag_library, pool_spec, card=card, usage=usage_map,
    )

    variables = prompt_core.collect_variables(
        card=card, excerpt=excerpt, tag_library=tag_library, usage=usage_map,
        limits=limits, model_name=model_name, profile_id=str(profile.get('id') or ''),
        selector=spec, extensions=extensions, local_note=local_note,
        tag_pool=pool_spec,
    )

    return {
        'variables': variables,
        'excerpt': excerpt,
        'limits': limits,
        'selector': spec,
        'selector_summary': selector_core.describe_selector(spec),
        'tag_pool': pool_result,
        'tag_pool_spec': pool_spec,
    }


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

    card = api.cards.read(identifier)
    if not isinstance(card, dict):
        raise AiAssistantError(CODE_CARD_UNREADABLE, f'无法读取卡片: {identifier}', http_status=404)

    provider = store.resolve_provider(str(provider_id or ''))
    model_name = str(model or provider.get('model') or '')
    profile = store.resolve_profile(str(profile_id or ''), model_name)

    bundle = build_variables(
        api=api, config=config, card=card, profile=profile,
        model_name=model_name, budget_tokens=budget_tokens, card_id=identifier,
    )
    rendered = prompt_core.render_messages(profile, bundle['variables'], task='analyze')
    prompt_tokens = tokens.estimate_messages(rendered['messages'])

    # 按变量拆解 token：让用户看清钱花在哪个字段上
    usage = prompt_core.variable_usage(profile, bundle['variables'])
    by_block = tokens.estimate_blocks(
        usage['hits'], bundle['variables'], total_tokens=prompt_tokens['total'],
    )

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
        'selector': {
            'spec': bundle['selector'],
            'summary': bundle['selector_summary'],
            # 选择器与模板的两类不一致（空值警告 / 白勾提示）
            'gaps': prompt_core.selector_gaps(bundle['selector'], profile, bundle['variables']),
            'referenced': usage['referenced'],
        },
        'worldbook': {
            'count': excerpt.get('count', 0),
            'total_tokens': excerpt.get('total_tokens', 0),
            'excerpt_tokens': excerpt.get('excerpt_tokens', 0),
            'truncated': excerpt.get('truncated', False),
            'selected': excerpt.get('selected', []),
            'dropped': excerpt.get('dropped', []),
            'pinned': excerpt.get('pinned', []),
            'pinned_missing': excerpt.get('pinned_missing', []),
        },
        'tag_pool': {
            'available': bundle['tag_pool'].get('available', 0),
            'listed': bundle['tag_pool'].get('listed', []),
            'dropped': bundle['tag_pool'].get('dropped', []),
            'tokens': bundle['tag_pool'].get('tokens', 0),
            'budget_tokens': bundle['tag_pool'].get('budget_tokens', 0),
            'isolated_categories': bundle['tag_pool'].get('isolated_categories', []),
            'excluded': bundle['tag_pool'].get('excluded', {}),
            'spec': bundle['tag_pool_spec'],
        },
        'tokens': {
            'system': prompt_tokens['by_role'].get('system', 0),
            'user': prompt_tokens['by_role'].get('user', 0),
            'total': prompt_tokens['total'],
            'max_completion': completion_budget,
            'by_block': by_block,
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
