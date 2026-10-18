"""作业处理器：`ai.analyze`（设计文档 §6.2）。

单次作业 = 一批卡片；每张卡一个 item。写回语义由 `apply_mode` 决定：
- `auto`：直接写概览（`summary_bridge`）+ 合并标签（`apply_tags(replace=False)`）；
- `review`：只产出建议，条目标 `awaiting_review`，等用户在 UI 里逐条确认后由
  `POST /jobs/<run_id>/apply` 统一写回；
- `dry_run`：与 review 同样只产出建议，但条目直接 `done`（永不写盘）。

密钥处理：payload 里只有 `secret_ref`，运行时通过 `ctx.resolve_secret()` 解析（C8）。
"""

import json
import logging
import time

from . import actions as actions_module
from . import config as config_module
from . import llm
from . import summary_bridge
from .core import agent as agent_core
from .core import classify as classify_core
from .core import hashing
from .core import parsing
from .core import proposal as proposal_core
from .core import prompt as prompt_core
from .core import selector as selector_core
from .core import tagpool as tagpool_core
from .core import trimming
from .core import wicheck as wicheck_core
from .core.errors import (
    CODE_CARD_UNREADABLE,
    CODE_LLM_AUTH_FAILED,
    CODE_MISSING_VARIABLE,
    CODE_PARSE_FAILED,
    CODE_SECRET_MISSING,
    CODE_SSRF_BLOCKED,
    CODE_VALIDATION_FAILED,
    CODE_WRITE_FAILED,
    AiAssistantError,
    CancelledError,
)

logger = logging.getLogger(__name__)

JOB_ANALYZE = 'ai.analyze'
JOB_WI_REWRITE = 'ai.wi_rewrite'
JOB_CLASSIFY = 'ai.classify'
JOB_PROPOSE = 'ai.propose'
JOB_AGENT = 'ai.agent'

MODE_BOTH = 'both'
MODE_SUMMARY = 'summary'
MODE_TAGS = 'tags'
VALID_MODES = (MODE_BOTH, MODE_SUMMARY, MODE_TAGS)

DEFAULT_CONTEXT_BUDGET = 8000
MIN_WI_BUDGET = 200

ABORT_AFTER_CONSECUTIVE_FAILURES = 5
FATAL_CODES = frozenset({CODE_SECRET_MISSING, CODE_LLM_AUTH_FAILED, CODE_SSRF_BLOCKED})

SECRET_REF_TEMPLATE = 'provider__{provider_id}__api_key'


def register_jobs(api, store) -> None:
    """注册全部作业处理器（`job_type` 必须已在 manifest 声明）。"""

    def analyze(ctx):
        run_analyze(ctx, api=api, store=store)

    def wi_rewrite(ctx):
        run_wi_rewrite(ctx, api=api, store=store)

    def classify(ctx):
        run_classify(ctx, api=api, store=store)

    def propose(ctx):
        run_propose(ctx, api=api, store=store)

    def agent(ctx):
        run_agent(ctx, api=api, store=store)

    api.jobs.register(JOB_ANALYZE, analyze)
    api.jobs.register(JOB_WI_REWRITE, wi_rewrite)
    api.jobs.register(JOB_CLASSIFY, classify)
    api.jobs.register(JOB_PROPOSE, propose)
    api.jobs.register(JOB_AGENT, agent)


# ------------------------------------------------------------------ 数据准备


def _embedded_entries(card) -> list:
    """取卡片内嵌世界书条目（兼容 V2 数组与 V3 对象两种结构）。"""
    block = hashing.card_data_block(card)
    book = block.get('character_book')
    if isinstance(book, dict) and isinstance(book.get('entries'), list):
        return book['entries']
    if isinstance(book, list):
        return book
    return []


def read_tag_library(api) -> dict:
    try:
        payload = api.tags.library()
    except Exception:  # noqa: BLE001 - 标签库不可用不应中断分析
        logger.warning('读取标签库失败', exc_info=True)
        return {'tags': [], 'taxonomy': {}, 'prefs': {}}
    return payload if isinstance(payload, dict) else {'tags': [], 'taxonomy': {}, 'prefs': {}}


def read_extensions(api, card_id: str) -> dict:
    """读取卡片的扩展数据（正则 / 酒馆助手脚本），仅在选择器要求时调用。

    `api.cards.read_extensions` 返回已归一的
    `{'regex_scripts': [...], 'tavern_helper': {'scripts', 'variables'}}`；
    旧卡片可能两者皆无 → 返回空 dict，由 `prompt.regex_scripts_text` 兜底。
    """
    identifier = str(card_id or '').strip()
    if not identifier:
        return {}
    try:
        payload = api.cards.read_extensions(identifier)
    except Exception:  # noqa: BLE001 - 扩展数据不可用不应中断分析
        logger.debug('读取卡片扩展数据失败: %s', identifier, exc_info=True)
        return {}
    return payload if isinstance(payload, dict) else {}


def read_local_note(api, card_id: str) -> str:
    """读取主程序的本地备注（即卡片列表上显示的「概览」）。"""
    identifier = str(card_id or '').strip()
    if not identifier:
        return ''
    try:
        return str(api.cards.read_note(identifier) or '')
    except Exception:  # noqa: BLE001
        logger.debug('读取本地备注失败: %s', identifier, exc_info=True)
        return ''


def tag_usage(api) -> dict:
    """统计标签使用频次（走只读查询；索引/主库不可用时返回空表）。"""
    try:
        rows = api.private.query('SELECT tags FROM card_metadata')
    except Exception:  # noqa: BLE001
        logger.debug('统计标签频次失败', exc_info=True)
        return {}

    counts = {}
    for row in rows:
        raw = row.get('tags') if isinstance(row, dict) else None
        try:
            tags = json.loads(raw or '[]')
        except (TypeError, ValueError):
            continue
        if not isinstance(tags, list):
            continue
        for tag in tags:
            text = str(tag or '').strip()
            if text:
                counts[text] = counts.get(text, 0) + 1
    return counts


def known_tag_set(tag_library) -> set:
    """标签库中的已知标签集合（含分类体系里的标签）。"""
    data = tag_library if isinstance(tag_library, dict) else {}
    known = {str(item).strip() for item in (data.get('tags') or []) if str(item).strip()}

    taxonomy = data.get('taxonomy') if isinstance(data.get('taxonomy'), dict) else {}
    tag_to_category = taxonomy.get('tag_to_category')
    if isinstance(tag_to_category, dict):
        known.update(str(key).strip() for key in tag_to_category if str(key).strip())

    category_tag_order = taxonomy.get('category_tag_order')
    if isinstance(category_tag_order, dict):
        for values in category_tag_order.values():
            if isinstance(values, (list, tuple)):
                known.update(str(item).strip() for item in values if str(item).strip())

    return known


def split_tags(tags, known: set, *, blocked=None) -> tuple:
    """把 AI 标签拆成「可写入」与「新标签建议」两部分。

    Args:
        known: 标签库里的**真实**标签集合（判断「已知 / 新」）。
        blocked: **明确被禁**的标签集合（隔离分类 / 排除分类 / 手动排除）。
            给了它就是硬过滤 —— 这些标签即使 AI 输出也进不了 `accepted`
            （原先隔离分类只是作为变量给 AI 看，是软约束）。

            **注意不要传「候选集」**：不在候选集里的标签是陌生标签，
            应该走「新标签建议」而非被丢弃。只有这里列出的才是禁止项。

    Returns:
        `(accepted, suggested, blocked_names)`；第三项是被挡下的标签名。
    """
    blocked_set = set(blocked) if isinstance(blocked, (set, list, tuple)) else None

    accepted = []
    suggested = []
    blocked_names = []

    for item in tags or []:
        name = str((item or {}).get('name') or '').strip()
        if not name:
            continue

        if blocked_set is not None and name in blocked_set:
            blocked_names.append(name)
            continue

        if name in known:
            if name not in accepted:
                accepted.append(name)
        else:
            suggested.append({'name': name, 'reason': '标签库中不存在'})

    return accepted, suggested, blocked_names


def merge_suggestions(primary, secondary) -> list:
    """合并两批新标签建议（按 name 去重）。"""
    result = []
    seen = set()
    for item in list(primary or []) + list(secondary or []):
        name = str((item or {}).get('name') or '').strip()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append({
            'name': name,
            'reason': str((item or {}).get('reason') or ''),
        })
    return result


# ------------------------------------------------------------------ 主流程


def run_analyze(ctx, *, api, store) -> None:
    """`ai.analyze` 作业主体。"""
    config = config_module.load_config(api)
    payload = ctx.payload if isinstance(ctx.payload, dict) else {}

    provider_id = str(payload.get('provider_id') or '')
    requested_model = str(payload.get('model') or '')
    profile_id = str(payload.get('profile_id') or '')
    apply_mode = str(payload.get('apply_mode') or config['apply_mode']).strip().lower()
    if apply_mode not in ('auto', 'review'):
        apply_mode = config['apply_mode']

    dry_run = bool(payload.get('dry_run'))
    force = bool(payload.get('force'))
    strict = bool(payload.get('strict', config['validation_strict']))
    budget_tokens = int(payload.get('budget_tokens') or 0)
    apply_tags_enabled = bool(payload.get('apply_tags', config['apply_tags']))

    total = len(ctx.items)
    ctx.set_total(total)

    # ---- provider 与密钥 ----
    try:
        provider = store.resolve_provider(provider_id)
    except AiAssistantError as exc:
        _abort(ctx, exc.code, exc.message, total)
        return

    api_key = store.resolve_provider_key(provider['id'])
    if not api_key:
        _abort(
            ctx, CODE_SECRET_MISSING,
            f'provider「{provider["id"]}」未配置 API Key，请先在插件设置里填写', total,
        )
        return

    extra_headers = store.resolve_provider_headers(provider['id'])
    model_name = requested_model or str(provider.get('model') or '')

    # ---- 档案与限制 ----
    try:
        profile = store.resolve_profile(profile_id, model_name)
    except AiAssistantError as exc:
        _abort(ctx, exc.code, exc.message, total)
        return

    limits = prompt_core.build_limits(config, profile)
    if not model_name:
        model_name = str(profile.get('model_match') or '').replace('*', '').strip() or 'unknown'

    # ---- 共享上下文（标签库、频次、世界书预算）----
    tag_library = read_tag_library(api)
    known_tags = known_tag_set(tag_library)
    usage_map = tag_usage(api)
    wi_budget = max(
        MIN_WI_BUDGET,
        int((budget_tokens or DEFAULT_CONTEXT_BUDGET) * float(config['wi_excerpt_ratio'])),
    )
    keywords = config_module.wi_keywords(config)

    ctx.log(
        'info',
        f'开始分析：{total} 张卡片 · provider={provider["id"]} · model={model_name} · '
        f'档案={profile["id"]}(v{profile.get("version")}) · 模式={apply_mode}'
        + ('（dry-run）' if dry_run else ''),
    )

    # ---- 逐项处理 ----
    counters = {'applied': 0, 'skipped': 0, 'failed': 0, 'review': 0}
    usage_totals = {'prompt_tokens': 0, 'completion_tokens': 0}
    consecutive_failures = 0
    last_error_code = ''

    for index, item in enumerate(ctx.items, start=1):
        if ctx.should_cancel():
            ctx.log('warning', f'收到取消/暂停请求，已在第 {index - 1}/{total} 项停下')
            break

        item_payload = item.get('payload') if isinstance(item.get('payload'), dict) else {}
        key = str(item.get('key') or '')

        ctx.set_message(f'分析中 {index}/{total}：{item_payload.get("card_id") or key}')

        try:
            outcome = analyze_one(
                api=api, store=store, config=config, ctx=ctx,
                card_id=str(item_payload.get('card_id') or key),
                ui_key=str(item_payload.get('ui_key') or key),
                content_hash=str(item_payload.get('content_hash') or ''),
                mode=str(item_payload.get('mode') or MODE_BOTH),
                profile=profile, limits=limits, model_name=model_name,
                provider=provider, api_key=api_key, extra_headers=extra_headers,
                tag_library=tag_library, known_tags=known_tags, usage_map=usage_map,
                wi_budget=wi_budget, keywords=keywords,
                apply_mode=apply_mode, dry_run=dry_run, force=force, strict=strict,
                apply_tags_enabled=apply_tags_enabled,
            )
        except AiAssistantError as exc:
            counters['failed'] += 1
            ctx.report(
                key, status='failed',
                error=f'{exc.code}: {exc.message}',
                result={'error': exc.code, 'message': exc.message, 'detail': exc.detail},
            )
            if exc.code == last_error_code:
                consecutive_failures += 1
            else:
                last_error_code = exc.code
                consecutive_failures = 1
            if exc.code in FATAL_CODES and consecutive_failures >= ABORT_AFTER_CONSECUTIVE_FAILURES:
                ctx.log('error', f'连续 {consecutive_failures} 项因 {exc.code} 失败，已中止作业')
                break
            continue
        except Exception as exc:  # noqa: BLE001 - 单条异常不能中断整个作业
            counters['failed'] += 1
            logger.exception('分析条目失败: %s', key)
            ctx.report(key, status='failed', error=f'{type(exc).__name__}: {exc}',
                       result={'error': 'internal_error', 'message': str(exc)})
            continue

        consecutive_failures = 0
        last_error_code = ''

        usage_totals['prompt_tokens'] += int(outcome['usage'].get('prompt_tokens') or 0)
        usage_totals['completion_tokens'] += int(outcome['usage'].get('completion_tokens') or 0)

        status = outcome['status']
        if status == 'awaiting_review':
            counters['review'] += 1
        elif status == 'done':
            counters['applied' if outcome.get('applied') else 'skipped'] += 1

        ctx.report(key, status=status, result=outcome['result'],
                   error=str(outcome.get('error') or ''))

    # ---- 收尾 ----
    summary = {
        'items': total,
        'applied': counters['applied'],
        'skipped': counters['skipped'],
        'failed': counters['failed'],
        'awaiting_review': counters['review'],
        'prompt_tokens': usage_totals['prompt_tokens'],
        'completion_tokens': usage_totals['completion_tokens'],
        'provider_id': provider['id'],
        'model': model_name,
        'profile_id': profile['id'],
        'apply_mode': apply_mode,
        'dry_run': dry_run,
    }
    ctx.set_summary(summary)
    ctx.set_message(
        f'完成：写入 {counters["applied"]} · 待审批 {counters["review"]} · '
        f'跳过 {counters["skipped"]} · 失败 {counters["failed"]}'
    )

    try:
        store.record_usage({
            'run_id': ctx.run_id,
            'job_type': JOB_ANALYZE,
            'provider_id': provider['id'],
            'model': model_name,
            'profile_id': profile['id'],
            'prompt_tokens': usage_totals['prompt_tokens'],
            'completion_tokens': usage_totals['completion_tokens'],
            'items': total,
        })
    except Exception:  # noqa: BLE001
        logger.warning('写入用量台账失败', exc_info=True)

    if counters['review']:
        try:
            store.mark_pending_review(ctx.run_id, JOB_ANALYZE, counters['review'])
        except Exception:  # noqa: BLE001
            logger.debug('登记待审批作业失败', exc_info=True)


def _abort(ctx, code: str, message: str, total: int) -> None:
    """作业级前置失败：抛出异常让主程序把作业置为 `failed`。

    条目保持 `pending`（`_run_job` 只认领 pending 项），用户修好配置后
    `retry_job(only_failed=True)` 或重新提交即可继续。
    """
    ctx.log('error', f'作业中止（{code}）：{message}')
    ctx.set_summary({'items': total, 'aborted': True, 'error': code, 'message': message})
    ctx.set_message(f'作业中止：{message}')
    raise AiAssistantError(code, message)


# ------------------------------------------------------------------ 单项处理


def analyze_one(*, api, store, config, ctx, card_id, ui_key, content_hash, mode,
                profile, limits, model_name, provider, api_key, extra_headers,
                tag_library, known_tags, usage_map, wi_budget, keywords,
                apply_mode, dry_run, force, strict, apply_tags_enabled) -> dict:
    """处理单张卡片：读取 → 渲染 → 调用 → 解析 → （可选）写回。"""
    started = time.monotonic()

    card = api.cards.read(card_id)
    if not isinstance(card, dict):
        raise AiAssistantError(CODE_CARD_UNREADABLE, f'无法读取卡片: {card_id}')

    # ---- 内容选择器：决定发什么（世界书取样、附加内容、正文字段）----
    selector_spec = prompt_core.profile_selector(profile, config)

    # ---- 标签池：来源过滤 + token 预算 + 相关性排序（卡片已知，可按词面重叠排）----
    pool_spec = prompt_core.profile_tag_pool(profile, config)
    pool_result = tagpool_core.build_pool_text(
        tag_library, pool_spec, card=card, usage=usage_map,
    )
    # 硬过滤只针对**明确被禁**的标签（隔离/排除分类/手动排除）。
    # 「不在候选集里」是陌生标签，应走新标签建议分支，不能当禁止项处理 ——
    # 否则标签池一设预算就会把大量合法标签误判成违规。
    blocked_tags_set = set(pool_result.get('blocked') or [])
    if pool_result.get('isolated_categories'):
        pool_spec = dict(pool_spec, _isolated=pool_result['isolated_categories'])

    entries = _embedded_entries(card)
    wi_spec = selector_spec['worldbook']
    if wi_spec['mode'] == selector_core.WI_NONE:
        excerpt = trimming.select_wi_excerpt(entries, 0, keywords=keywords)
    else:
        only_uids = wi_spec['entry_uids'] if wi_spec['mode'] == selector_core.WI_SELECTED else []
        pinned_uids = wi_spec['entry_uids'] if wi_spec['mode'] != selector_core.WI_SELECTED else []
        budget = wi_budget
        if wi_spec['budget_tokens']:
            budget = max(MIN_WI_BUDGET, min(budget, wi_spec['budget_tokens']))
        excerpt = trimming.select_wi_excerpt(
            entries, budget, keywords=keywords,
            pinned_uids=pinned_uids, only_uids=only_uids,
        )

    extensions = None
    if (selector_core.wants_extra(selector_spec, selector_core.EXTRA_REGEX)
            or selector_core.wants_extra(selector_spec, selector_core.EXTRA_ST_SCRIPTS)):
        extensions = read_extensions(api, card_id)

    local_note = None
    if selector_core.wants_extra(selector_spec, selector_core.EXTRA_LOCAL_NOTE):
        local_note = read_local_note(api, card_id)

    # ---- 渲染 ----
    variables = prompt_core.collect_variables(
        card=card, excerpt=excerpt, tag_library=tag_library, usage=usage_map,
        limits=limits, model_name=model_name, profile_id=str(profile.get('id') or ''),
        selector=selector_spec, extensions=extensions, local_note=local_note,
        tag_pool=pool_spec,
    )
    rendered = prompt_core.render_messages(profile, variables, task='analyze')

    if rendered['missing_required']:
        raise AiAssistantError(
            CODE_MISSING_VARIABLE,
            '提示词缺少必需变量: ' + '、'.join(rendered['missing_required']),
            detail={'variables': rendered['missing_required']},
        )

    # ---- 调用 ----
    result = llm.chat(
        rendered['messages'],
        provider=provider, api_key=api_key, extra_headers=extra_headers,
        model=model_name,
        temperature=profile.get('temperature'), top_p=profile.get('top_p'),
        max_tokens=profile.get('max_tokens'), stop=profile.get('stop'),
        extra_body=profile.get('extra_body'),
        cancel_check=ctx.should_cancel,
    )

    usage = result.get('usage') or {}
    duration_ms = int((time.monotonic() - started) * 1000)

    # ---- 解析与校验（必须带上 prefill：模型返回的是续写片段，不含预填充）----
    outcome = parsing.parse_llm_result(
        result.get('content'), profile.get('output_schema'), strict=strict,
        prefill=str(profile.get('assistant_prefill') or ''),
    )

    if not outcome['ok']:
        message = '；'.join(outcome['issues']) or '输出校验未通过'
        code = CODE_VALIDATION_FAILED if outcome['result'] else CODE_PARSE_FAILED
        raise AiAssistantError(
            code, message,
            detail={'issues': outcome['issues'], 'excerpt': outcome.get('raw_excerpt', '')},
        )

    parsed = outcome['result'] or {}
    warnings = list(outcome['warnings'])
    if result.get('usage_estimated'):
        warnings.append('供应商未返回 token 用量，已按公式估算')

    summary_text = str(parsed.get('summary') or '').strip()

    # ---- 别名还原必须早于 split_tags ----
    # `split_tags` 用真实标签集判断「已知 / 新」；若 AI 输出的是别名而没先换回来，
    # 真实标签会被误判成新标签塞进建议队列。
    aliased, alias_info = tagpool_core.restore_aliases(
        parsed.get('tags') or [], profile.get('tag_aliases') or [],
    )
    if alias_info['applied']:
        pairs = '、'.join(f'{item["from"]}→{item["to"]}' for item in alias_info['applied'])
        warnings.append(f'已还原标签别名: {pairs}')

    tags = aliased
    accepted_tags, suggested_from_tags, blocked_tags = split_tags(
        tags, known_tags, blocked=blocked_tags_set,
    )
    if blocked_tags:
        warnings.append(
            '被标签池挡下（隔离/排除分类）: ' + '、'.join(blocked_tags[:8]),
        )

    suggestions = merge_suggestions(parsed.get('suggested_new_tags'), suggested_from_tags)
    if not pool_spec['allow_new_tags']:
        if suggestions:
            warnings.append(f'标签池不允许新标签，已丢弃 {len(suggestions)} 条建议')
        suggestions = []
    elif len(suggestions) > pool_spec['max_new_tags']:
        warnings.append(
            f'新标签建议超过上限 {pool_spec["max_new_tags"]} 条，已截断',
        )
        suggestions = suggestions[:pool_spec['max_new_tags']]

    if rendered['missing_optional']:
        warnings.append('可选变量缺失: ' + '、'.join(rendered['missing_optional']))

    result_payload = {
        'card_id': card_id,
        'ui_key': ui_key,
        'content_hash': content_hash,
        'summary': summary_text,
        'tags': tags,
        'tags_applied': [],
        'tags_suggested': [item['name'] for item in suggestions],
        'new_tags': suggestions,
        'card_type': parsed.get('card_type', ''),
        'language': parsed.get('language', ''),
        'content_flags': parsed.get('content_flags', []),
        'warnings': warnings,
        'usage': usage,
        'duration_ms': duration_ms,
        'model': model_name,
        'profile_id': profile.get('id', ''),
        'worldbook': {
            'count': excerpt.get('count', 0),
            'total_tokens': excerpt.get('total_tokens', 0),
            'excerpt_tokens': excerpt.get('excerpt_tokens', 0),
            'truncated': excerpt.get('truncated', False),
        },
        'applied': False,
        'summary_origin': '',
    }

    if dry_run:
        result_payload['dry_run'] = True
        return {'status': 'done', 'result': result_payload, 'usage': usage}

    if apply_mode == 'review':
        # 待审批：不写盘，也不记入 card_meta（避免被幂等判定误认为已分析）
        return {'status': 'awaiting_review', 'result': result_payload, 'usage': usage}

    # ---- auto：按 task spec 的 apply_actions 落地 ----
    apply_actions = profile.get('apply_actions') or []

    # 把「本次只写概览 / 只写标签」的意图折算成动作过滤。
    # 这是**调用方的临时意图**（作业条目参数），不是 task spec 的一部分，
    # 因此在这里过滤而不改 task spec 本身。
    mode_action_gate = {
        MODE_SUMMARY: ('write_card_note', 'queue_suggestions'),
        MODE_TAGS: ('apply_tags', 'queue_suggestions'),
    }.get(mode)
    if mode_action_gate:
        filtered = [
            entry for entry in apply_actions
            if str((entry or {}).get('action') or '') in mode_action_gate
        ]
        if len(filtered) != len(apply_actions):
            warnings.append(f'已按模式 {mode} 限制写回动作')
        apply_actions = filtered

    if not apply_tags_enabled:
        # 配置里关掉了标签写入：把该动作剔除，其余动作照常执行
        # （不是整批不动 —— 概览该写还是要写）
        kept = [
            entry for entry in apply_actions
            if str((entry or {}).get('action') or '') != 'apply_tags'
        ]
        if len(kept) != len(apply_actions):
            warnings.append('已按配置跳过标签写入')
        apply_actions = kept

    action_outcome = actions_module.run_actions(
        apply_actions, parsed,
        api=api, store=store, card_id=card_id,
        context={
            'ui_key': ui_key,
            'accepted_tags': accepted_tags,
            'worldbook_source': None,
        },
    )

    result_payload['actions'] = action_outcome['actions']
    warnings.extend(action_outcome['warnings'])

    # 回填「实际写进去的标签」与概览来源，供审批页与账本展示
    for record in action_outcome['actions']:
        if record.get('action') == 'apply_tags' and record.get('added'):
            result_payload['tags_applied'] = list(record['added'])
        if record.get('action') == 'write_card_note':
            result_payload['summary_origin'] = str(record.get('origin') or '')

    applied = bool(action_outcome['applied'])

    try:
        store.update_card_meta(ui_key, {
            'content_hash': content_hash,
            'profile_id': profile.get('id', ''),
            'model': model_name,
            'analyzed_at': int(time.time()),
            'duration_ms': duration_ms,
            'usage': usage,
            'tags_applied': result_payload['tags_applied'],
            'tags_suggested': result_payload['tags_suggested'],
            'summary_origin': result_payload['summary_origin'],
            'summary': summary_text,
            'last_run_id': ctx.run_id,
        })
    except Exception:  # noqa: BLE001
        logger.warning('写入卡片元数据失败', exc_info=True)

    result_payload['applied'] = applied
    return {'status': 'done', 'result': result_payload, 'usage': usage, 'applied': applied}


# ------------------------------------------------------------------ 世界书改写


def resolve_wi_source(card_id: str, worldbook_source=None) -> dict:
    """决定要改写的世界书来源。

    - 给了 `worldbook_source`（作业参数）就照用；
    - 否则按卡片的**内嵌世界书**处理。
    """
    source = worldbook_source if isinstance(worldbook_source, dict) else {}
    if source.get('source_type') or source.get('card_id') or source.get('file_path'):
        return dict(source)
    return {'source_type': 'embedded', 'card_id': str(card_id or '')}


def run_wi_rewrite(ctx, *, api, store) -> None:
    """世界书改写作业：逐条目改写 → 校验 → （可选）写回。

    与 `run_analyze` 的差别是**条目级**而非卡片级：一个作业项 = 一条世界书条目。

    **格式只是提示词约束**（设计文档 §6.3）：不做 YAML 解析校验，
    只校验「内容有没有被破坏」（宏、HTML、压缩比、长度比、专有名词、调度字段）。
    """
    config = config_module.load_config(api)
    payload = ctx.payload if isinstance(ctx.payload, dict) else {}

    provider_id = str(payload.get('provider_id') or '')
    requested_model = str(payload.get('model') or '')
    profile_id = str(payload.get('profile_id') or '')
    budget_tokens = int(payload.get('budget_tokens') or 0)
    apply_mode = str(payload.get('apply_mode') or config['apply_mode'])
    dry_run = bool(payload.get('dry_run', False))
    target_format = str(payload.get('target_format') or 'yaml')
    worldbook_source = payload.get('worldbook_source')

    total = len(ctx.items)
    ctx.set_total(total)

    # ---- provider 与密钥 ----
    try:
        provider = store.resolve_provider(provider_id)
    except AiAssistantError as exc:
        _abort(ctx, exc.code, exc.message, total)
        return

    api_key = store.resolve_provider_key(provider['id'])
    if not api_key:
        _abort(
            ctx, CODE_SECRET_MISSING,
            f'provider「{provider["id"]}」未配置 API Key，请先在插件设置里填写', total,
        )
        return

    extra_headers = store.resolve_provider_headers(provider['id'])
    model_name = requested_model or str(provider.get('model') or '')

    try:
        profile = store.resolve_profile(profile_id, model_name)
    except AiAssistantError as exc:
        _abort(ctx, exc.code, exc.message, total)
        return

    if not model_name:
        model_name = str(profile.get('model_match') or '').replace('*', '').strip() or 'unknown'

    ctx.log(
        'info',
        f'开始世界书改写：{total} 条 · provider={provider["id"]} · model={model_name} · '
        f'档案={profile["id"]} · 目标格式={target_format} · 模式={apply_mode}'
        + ('（dry-run）' if dry_run else ''),
    )

    counters = {'applied': 0, 'skipped': 0, 'failed': 0, 'review': 0}
    usage_totals = {'prompt_tokens': 0, 'completion_tokens': 0}

    # 按来源分组：同一来源只读一次、写一次（写回是整本写，不是逐条写）
    pending_writes = {}

    for index, item in enumerate(ctx.items, start=1):
        if ctx.should_cancel():
            ctx.log('warning', f'收到取消/暂停请求，已在第 {index - 1}/{total} 条停下')
            break

        item_payload = item.get('payload') if isinstance(item.get('payload'), dict) else {}
        key = str(item.get('key') or '')
        card_id = str(item_payload.get('card_id') or '')
        entry_uid = str(item_payload.get('entry_uid') or key)

        ctx.set_message(f'改写中 {index}/{total}：{item_payload.get("entry_name") or entry_uid}')

        try:
            outcome = rewrite_entry(
                api=api, config=config, ctx=ctx,
                card_id=card_id, entry_uid=entry_uid,
                worldbook_source=worldbook_source or item_payload.get('worldbook_source'),
                profile=profile, model_name=model_name,
                provider=provider, api_key=api_key, extra_headers=extra_headers,
                target_format=target_format, apply_mode=apply_mode,
                dry_run=dry_run, force=bool(payload.get('force')),
            )
        except AiAssistantError as exc:
            if exc.code in FATAL_CODES:
                _abort(ctx, exc.code, exc.message, total)
                return
            counters['failed'] += 1
            ctx.report(key, status='failed', error=exc.code,
                       result={'error': exc.code, 'message': exc.message})
            continue
        except CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            counters['failed'] += 1
            logger.exception('改写条目失败: %s', key)
            ctx.report(key, status='failed', error=f'{type(exc).__name__}: {exc}',
                       result={'error': 'internal_error', 'message': str(exc)})
            continue

        usage = outcome.get('usage') or {}
        usage_totals['prompt_tokens'] += int(usage.get('prompt_tokens') or 0)
        usage_totals['completion_tokens'] += int(usage.get('completion_tokens') or 0)

        status = outcome['status']
        if status == 'awaiting_review':
            counters['review'] += 1
        elif status == 'failed':
            # 条目级校验失败（不是异常）也要计数，否则汇总里看不到
            counters['failed'] += 1
        elif status == 'done':
            counters['applied' if outcome.get('applied') else 'skipped'] += 1
            # 攒起来最后整本写回
            patch = outcome.get('write')
            if patch:
                bucket = pending_writes.setdefault(patch['source_key'], {
                    'source': patch['source'], 'book': patch['book'], 'uids': [],
                })
                bucket['uids'].append(patch['entry_uid'])

        ctx.report(key, status=status, result=outcome['result'],
                   error=str(outcome.get('error') or ''))

    # ---- 整本写回 ----
    written = 0
    write_errors = []
    for bucket in pending_writes.values():
        try:
            result = api.worldbook.write(
                bucket['source'], bucket['book'],
                expected_revision=str(bucket['book'].get('source_revision') or ''),
            )
            if result.get('success'):
                written += len(bucket['uids'])
            else:
                write_errors.append(str(result.get('msg') or result.get('error') or '写入失败'))
        except Exception as exc:  # noqa: BLE001
            logger.exception('世界书写回失败')
            write_errors.append(str(exc))

    if pending_writes:
        ctx.log('info', f'世界书写回：{written} 条成功' + (
            f'，{len(write_errors)} 处失败：{"；".join(write_errors[:3])}' if write_errors else ''
        ))

    summary = {
        'items': total,
        'applied': counters['applied'],
        'skipped': counters['skipped'],
        'failed': counters['failed'],
        'awaiting_review': counters['review'],
        'written': written,
        'write_errors': write_errors,
        'prompt_tokens': usage_totals['prompt_tokens'],
        'completion_tokens': usage_totals['completion_tokens'],
        'provider_id': provider['id'],
        'model': model_name,
        'profile_id': profile['id'],
        'target_format': target_format,
        'apply_mode': apply_mode,
        'dry_run': dry_run,
    }
    ctx.set_summary(summary)
    ctx.set_message(
        f'完成：改写 {counters["applied"]} · 待审批 {counters["review"]} · '
        f'跳过 {counters["skipped"]} · 失败 {counters["failed"]} · 写入 {written} 条'
    )

    try:
        store.record_usage({
            'run_id': ctx.run_id,
            'job_type': JOB_WI_REWRITE,
            'provider_id': provider['id'],
            'model': model_name,
            'profile_id': profile['id'],
            'prompt_tokens': usage_totals['prompt_tokens'],
            'completion_tokens': usage_totals['completion_tokens'],
            'items': total,
        })
    except Exception:  # noqa: BLE001
        logger.warning('写入用量台账失败', exc_info=True)

    if counters['review']:
        try:
            store.mark_pending_review(ctx.run_id, JOB_WI_REWRITE, counters['review'])
        except Exception:  # noqa: BLE001
            logger.debug('登记待审批作业失败', exc_info=True)


def _render_rewrite_prompt(profile, entry, target_format: str) -> dict:
    """渲染「改写这一条」的 messages。

    直接复用任务规格的模板 + 新增的 `wi.*` 变量（`entry_name` / `entry_keys` /
    `entry_content` / `target_ratio` 本来就在变量表里）。
    """
    variables = {
        'wi.entry_name': str(entry.get('comment') or entry.get('name') or ''),
        'wi.entry_keys': '、'.join(str(item) for item in (entry.get('keys') or [])),
        'wi.entry_content': str(entry.get('content') or ''),
        'wi.target_ratio': str(wicheck_core.MAX_LENGTH_RATIO),
    }
    rendered = prompt_core.render_messages(profile, variables, task='wi_rewrite')

    instruction = wicheck_core.build_rewrite_instruction(target_format)
    if instruction:
        # 追加到 system 末尾（不覆盖任务规格自己的要求）
        for message in rendered['messages']:
            if message.get('role') == 'system':
                message['content'] = message['content'] + '\n\n' + instruction
                break
        else:
            rendered['messages'].insert(0, {'role': 'system', 'content': instruction})

    return rendered


def rewrite_entry(*, api, config, ctx, card_id, entry_uid, worldbook_source,
                  profile, model_name, provider, api_key, extra_headers,
                  target_format, apply_mode, dry_run, force=False) -> dict:
    """处理单条世界书条目：读取 → 渲染 → 调用 → 校验 →（可选）准备写回。"""
    started = time.monotonic()

    source = resolve_wi_source(card_id, worldbook_source)
    book = api.worldbook.read(source)
    if not isinstance(book, dict):
        raise AiAssistantError(
            CODE_CARD_UNREADABLE, f'无法读取世界书: {source}', http_status=404,
        )

    entries = book.get('entries')
    if isinstance(entries, dict):
        entries = list(entries.values())
    if not isinstance(entries, list):
        raise AiAssistantError(CODE_INVALID_PAYLOAD, '世界书结构异常（无 entries）')

    entry = None
    for item in entries:
        if not isinstance(item, dict):
            continue
        if str(item.get('uid') or item.get('id') or '') == entry_uid:
            entry = item
            break

    if entry is None:
        raise AiAssistantError(
            CODE_INVALID_PAYLOAD, f'世界书里找不到条目: {entry_uid}', http_status=404,
        )

    original_content = str(entry.get('content') or '')
    if not original_content.strip():
        return {
            'status': 'done',
            'result': {'entry_uid': entry_uid, 'skipped': 'empty_entry'},
            'usage': {}, 'applied': False,
        }

    rendered = _render_rewrite_prompt(profile, entry, target_format)

    result = llm.chat(
        rendered['messages'],
        provider=provider, api_key=api_key, extra_headers=extra_headers,
        model=model_name,
        temperature=profile.get('temperature'), top_p=profile.get('top_p'),
        max_tokens=profile.get('max_tokens'), stop=profile.get('stop'),
        extra_body=profile.get('extra_body'),
        cancel_check=ctx.should_cancel,
    )

    usage = result.get('usage') or {}
    duration_ms = int((time.monotonic() - started) * 1000)

    # 世界书正文不是 JSON：直接取文本，不走 `parse_llm_result`
    candidate = parsing.strip_think(str(result.get('content') or ''))
    candidate, _fenced = parsing.strip_fences(candidate)
    candidate = candidate.strip()

    checked = wicheck_core.validate_rewrite(
        original_content, candidate,
        original_entry=entry,
        candidate_entry=entry,   # 候选沿用原条目的调度字段，校验它们是否被改动
    )

    warnings = list(checked['warnings'])
    result_payload = {
        'entry_uid': entry_uid,
        'entry_name': str(entry.get('comment') or entry.get('name') or ''),
        'original_tokens': checked['metrics']['original_tokens'],
        'candidate_tokens': checked['metrics']['candidate_tokens'],
        'token_ratio': checked['metrics']['token_ratio'],
        'length_ratio': checked['metrics']['length_ratio'],
        'dropped_facts': checked['dropped_facts'],
        'candidate': candidate,
        'warnings': warnings,
        'issues': list(checked['issues']),
        'duration_ms': duration_ms,
        'model': model_name,
        'profile_id': profile.get('id', ''),
        'usage': usage,
        'applied': False,
    }

    # 校验不过 → 不写回，结果标记失败（用户能看到原因）
    if not checked['ok']:
        result_payload['error'] = 'wi_validation_failed'
        return {
            'status': 'failed',
            'result': result_payload,
            'usage': usage,
        }

    if dry_run:
        result_payload['dry_run'] = True
        return {'status': 'done', 'result': result_payload, 'usage': usage, 'applied': False}

    if apply_mode == 'review':
        return {'status': 'awaiting_review', 'result': result_payload, 'usage': usage}

    # 内嵌世界书写回前先快照卡片（外部世界书由主程序的条目历史兜底）
    if source.get('source_type') == 'embedded' and card_id:
        try:
            api.backups.snapshot_card(card_id, remark='ai-wi-rewrite')
        except Exception:  # noqa: BLE001
            logger.warning('世界书改写前快照失败', exc_info=True)

    # 在**副本**上改，原对象留给下一次读取（避免同批次多条时互相污染）
    updated = dict(book)
    updated_entries = []
    for item in entries:
        if (isinstance(item, dict)
                and str(item.get('uid') or item.get('id') or '') == entry_uid):
            patched = dict(item)
            patched['content'] = candidate
            updated_entries.append(patched)
        else:
            updated_entries.append(item)
    updated['entries'] = updated_entries

    result_payload['applied'] = True

    return {
        'status': 'done',
        'result': result_payload,
        'usage': usage,
        'applied': True,
        'write': {
            'source_key': f"{source.get('source_type')}|{source.get('card_id')}|{source.get('file_path')}",
            'source': source,
            'book': updated,
            'entry_uid': entry_uid,
        },
    }


# ------------------------------------------------------------------ 自动归类


def run_classify(ctx, *, api, store) -> None:
    """自动归类作业：逐卡判断所属分类 →（审批后）移动。

    与 `run_analyze` 共用「选择器 + 渲染 + 解析 + 校验」的骨架，
    差别只在输出字段（`category` 而非 `summary`/`tags`）与应用动作（`move_cards`）。
    """
    config = config_module.load_config(api)
    payload = ctx.payload if isinstance(ctx.payload, dict) else {}

    provider_id = str(payload.get('provider_id') or '')
    requested_model = str(payload.get('model') or '')
    profile_id = str(payload.get('profile_id') or '')
    apply_mode = str(payload.get('apply_mode') or config['apply_mode'])
    dry_run = bool(payload.get('dry_run', False))
    scheme = classify_core.normalize_scheme(payload.get('scheme'))
    allow_new = bool(payload.get('allow_new_category', False))
    confidence_min = payload.get('confidence_min')
    if confidence_min is None:
        confidence_min = classify_core.DEFAULT_CONFIDENCE_MIN
    try:
        confidence_min = float(confidence_min)
    except (TypeError, ValueError):
        confidence_min = classify_core.DEFAULT_CONFIDENCE_MIN

    total = len(ctx.items)
    ctx.set_total(total)

    try:
        provider = store.resolve_provider(provider_id)
    except AiAssistantError as exc:
        _abort(ctx, exc.code, exc.message, total)
        return

    api_key = store.resolve_provider_key(provider['id'])
    if not api_key:
        _abort(
            ctx, CODE_SECRET_MISSING,
            f'provider「{provider["id"]}」未配置 API Key，请先在插件设置里填写', total,
        )
        return

    extra_headers = store.resolve_provider_headers(provider['id'])
    model_name = requested_model or str(provider.get('model') or '')

    try:
        profile = store.resolve_profile(profile_id, model_name)
    except AiAssistantError as exc:
        _abort(ctx, exc.code, exc.message, total)
        return

    # 归类要的是 `category` 字段，而 `builtin-default` 的 schema 要求 `summary`/`tags`。
    # 调用方没指定档案时会按模型名回落 → AI 的归类输出会被判「缺少必需字段」。
    # 这里兜底到归类专用档案（仍是「配置」，不是硬编码提示词）。
    if not str(profile_id or '').strip():
        classify_profile = store.get_profile('builtin-classify')
        if isinstance(classify_profile, dict):
            profile = classify_profile

    limits = prompt_core.build_limits(config, profile)
    if not model_name:
        model_name = str(profile.get('model_match') or '').replace('*', '').strip() or 'unknown'

    tag_library = read_tag_library(api)
    usage_map = tag_usage(api)
    keywords = config_module.wi_keywords(config)

    ctx.log(
        'info',
        f'开始归类：{total} 张卡片 · 方案 {len(scheme)} 个分类 · '        f'provider={provider["id"]} · model={model_name} · 模式={apply_mode}'
        + ('（dry-run）' if dry_run else ''),
    )

    counters = {'applied': 0, 'skipped': 0, 'failed': 0, 'review': 0}
    usage_totals = {'prompt_tokens': 0, 'completion_tokens': 0}
    consecutive_failures = 0
    # 按分类攒起来批量移动（主程序 move 支持批量，比逐卡快得多）
    pending_moves = {}

    for index, item in enumerate(ctx.items, start=1):
        if ctx.should_cancel():
            ctx.log('warning', f'收到取消/暂停请求，已在第 {index - 1}/{total} 项停下')
            break

        item_payload = item.get('payload') if isinstance(item.get('payload'), dict) else {}
        key = str(item.get('key') or '')
        card_id = str(item_payload.get('card_id') or key)

        ctx.set_message(f'归类中 {index}/{total}：{item_payload.get("name") or card_id}')

        try:
            outcome = classify_one(
                api=api, store=store, config=config, ctx=ctx,
                card_id=card_id, ui_key=str(item_payload.get('ui_key') or key),
                current_category=str(item_payload.get('category') or ''),
                profile=profile, limits=limits, model_name=model_name,
                provider=provider, api_key=api_key, extra_headers=extra_headers,
                tag_library=tag_library, usage_map=usage_map, keywords=keywords,
                scheme=scheme, allow_new=allow_new, confidence_min=confidence_min,
                apply_mode=apply_mode, dry_run=dry_run,
                force=bool(payload.get('force')),
            )
        except AiAssistantError as exc:
            if exc.code in FATAL_CODES:
                _abort(ctx, exc.code, exc.message, total)
                return
            counters['failed'] += 1
            consecutive_failures += 1
            ctx.report(key, status='failed', error=exc.code,
                       result={'error': exc.code, 'message': exc.message})
            if consecutive_failures >= ABORT_AFTER_CONSECUTIVE_FAILURES:
                _abort(ctx, exc.code, f'连续 {consecutive_failures} 次失败，已中止', total)
                return
            continue
        except CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            counters['failed'] += 1
            logger.exception('归类条目失败: %s', key)
            ctx.report(key, status='failed', error=f'{type(exc).__name__}: {exc}',
                       result={'error': 'internal_error', 'message': str(exc)})
            continue

        consecutive_failures = 0

        usage = outcome.get('usage') or {}
        usage_totals['prompt_tokens'] += int(usage.get('prompt_tokens') or 0)
        usage_totals['completion_tokens'] += int(usage.get('completion_tokens') or 0)

        status = outcome['status']
        if status == 'awaiting_review':
            counters['review'] += 1
        elif status == 'failed':
            counters['failed'] += 1
        elif status == 'done':
            counters['applied' if outcome.get('applied') else 'skipped'] += 1
            move = outcome.get('move')
            if move:
                pending_moves.setdefault(move['category'], []).append(move['card_id'])

        ctx.report(key, status=status, result=outcome['result'],
                   error=str(outcome.get('error') or ''))

    # ---- 批量移动 ----
    moved = 0
    move_errors = []
    for category, card_ids in pending_moves.items():
        try:
            result = api.cards.move(card_ids, category)
            moved += int(result.get('count') or 0)
            for failure in (result.get('failed') or []):
                move_errors.append(
                    f'{failure.get("old_id", "")}: {failure.get("msg", "移动失败")}',
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception('移动卡片失败: %s', category)
            move_errors.append(f'{category}: {exc}')

    if pending_moves:
        ctx.log('info', f'移动 {moved} 张到 {len(pending_moves)} 个分类' + (
            f'，{len(move_errors)} 处失败' if move_errors else ''
        ))

    summary = {
        'items': total,
        'applied': counters['applied'],
        'skipped': counters['skipped'],
        'failed': counters['failed'],
        'awaiting_review': counters['review'],
        'moved': moved,
        'move_errors': move_errors,
        'scheme_size': len(scheme),
        'prompt_tokens': usage_totals['prompt_tokens'],
        'completion_tokens': usage_totals['completion_tokens'],
        'provider_id': provider['id'],
        'model': model_name,
        'profile_id': profile['id'],
        'apply_mode': apply_mode,
        'dry_run': dry_run,
    }
    ctx.set_summary(summary)
    ctx.set_message(
        f'完成：归类 {counters["applied"]} · 待审批 {counters["review"]} · '
        f'跳过 {counters["skipped"]} · 失败 {counters["failed"]} · 移动 {moved} 张'
    )

    try:
        store.record_usage({
            'run_id': ctx.run_id,
            'job_type': JOB_CLASSIFY,
            'provider_id': provider['id'],
            'model': model_name,
            'profile_id': profile['id'],
            'prompt_tokens': usage_totals['prompt_tokens'],
            'completion_tokens': usage_totals['completion_tokens'],
            'items': total,
        })
    except Exception:  # noqa: BLE001
        logger.warning('写入用量台账失败', exc_info=True)

    if counters['review']:
        try:
            store.mark_pending_review(ctx.run_id, JOB_CLASSIFY, counters['review'])
        except Exception:  # noqa: BLE001
            logger.debug('登记待审批作业失败', exc_info=True)


def _render_classify_prompt(profile, card, scheme, config, tag_library, usage_map,
                            keywords, budget_tokens=0):
    """渲染「给这张卡归类」的 messages。"""
    entries = _embedded_entries(card)
    excerpt = trimming.select_wi_excerpt(
        entries,
        max(MIN_WI_BUDGET, int((budget_tokens or DEFAULT_CONTEXT_BUDGET)
                               * float(config['wi_excerpt_ratio']))),
        keywords=keywords,
    )

    selector_spec = prompt_core.profile_selector(profile, config)
    variables = prompt_core.collect_variables(
        card=card, excerpt=excerpt, tag_library=tag_library, usage=usage_map,
        limits=prompt_core.build_limits(config, profile),
        model_name='', profile_id=str(profile.get('id') or ''),
        selector=selector_spec,
        tag_pool=prompt_core.profile_tag_pool(profile, config),
    )

    rendered = prompt_core.render_messages(profile, variables, task='classify')

    instruction = classify_core.build_classify_instruction(scheme)
    if instruction:
        for message in rendered['messages']:
            if message.get('role') == 'system':
                message['content'] = message['content'] + '\n\n' + instruction
                break
        else:
            rendered['messages'].insert(0, {'role': 'system', 'content': instruction})

    return rendered


def classify_one(*, api, store, config, ctx, card_id, ui_key, current_category,
                 profile, limits, model_name, provider, api_key, extra_headers,
                 tag_library, usage_map, keywords, scheme, allow_new,
                 confidence_min, apply_mode, dry_run, force=False) -> dict:
    """处理单张卡片的归类。"""
    started = time.monotonic()

    card = api.cards.read(card_id)
    if not isinstance(card, dict):
        raise AiAssistantError(CODE_CARD_UNREADABLE, f'无法读取卡片: {card_id}')

    rendered = _render_classify_prompt(
        profile, card, scheme, config, tag_library, usage_map, keywords,
    )

    if rendered['missing_required']:
        raise AiAssistantError(
            CODE_MISSING_VARIABLE,
            '提示词缺少必需变量: ' + '、'.join(rendered['missing_required']),
            detail={'variables': rendered['missing_required']},
        )

    result = llm.chat(
        rendered['messages'],
        provider=provider, api_key=api_key, extra_headers=extra_headers,
        model=model_name,
        temperature=profile.get('temperature'), top_p=profile.get('top_p'),
        max_tokens=profile.get('max_tokens'), stop=profile.get('stop'),
        extra_body=profile.get('extra_body'),
        cancel_check=ctx.should_cancel,
    )

    usage = result.get('usage') or {}
    duration_ms = int((time.monotonic() - started) * 1000)

    outcome = parsing.parse_llm_result(
        result.get('content'), profile.get('output_schema'),
        strict=bool(config.get('validation_strict')),
        prefill=str(profile.get('assistant_prefill') or ''),
    )

    if not outcome['ok']:
        message = '；'.join(outcome['issues']) or '输出校验未通过'
        payload = {
            'card_id': card_id, 'ui_key': ui_key, 'error': 'validation_failed',
            'message': message, 'detail': {'issues': outcome['issues'],
                                          'excerpt': outcome['raw_excerpt']},
            'duration_ms': duration_ms, 'model': model_name,
        }
        raise AiAssistantError(
            CODE_VALIDATION_FAILED, message,
            detail=payload['detail'],
        )

    parsed = outcome['result'] or {}
    decision = classify_core.normalize_result_category(
        parsed, scheme, allow_new=allow_new, confidence_min=confidence_min,
    )

    warnings = list(outcome['warnings']) + list(decision['warnings'])

    result_payload = {
        'card_id': card_id,
        'ui_key': ui_key,
        'current_category': current_category,
        'category': decision['category'],
        'raw_category': decision['raw_category'],
        'confidence': decision['confidence'],
        'reason': decision['reason'],
        'low_confidence': decision['low_confidence'],
        'issues': decision['issues'],
        'warnings': warnings,
        'duration_ms': duration_ms,
        'model': model_name,
        'profile_id': profile.get('id', ''),
        'usage': usage,
        'applied': False,
    }

    # 目标分类不合法 / 不在方案内 → 不移动
    if decision['issues']:
        result_payload['error'] = 'classify_failed'
        return {'status': 'failed', 'result': result_payload, 'usage': usage}

    # 已经在目标分类 → 幂等跳过
    if decision['category'] == current_category and not force:
        result_payload['skipped'] = 'unchanged'
        return {'status': 'done', 'result': result_payload, 'usage': usage,
                'applied': False}

    if dry_run:
        result_payload['dry_run'] = True
        return {'status': 'done', 'result': result_payload, 'usage': usage,
                'applied': False}

    if apply_mode == 'review':
        return {'status': 'awaiting_review', 'result': result_payload, 'usage': usage}

    result_payload['applied'] = True
    return {
        'status': 'done',
        'result': result_payload,
        'usage': usage,
        'applied': True,
        'move': {'category': decision['category'], 'card_id': card_id},
    }


# ------------------------------------------------------------------ 结构化提议


def collect_sample_rows(api, *, options=None, max_scan: int = 2000) -> dict:
    """从主程序读卡片并整理成采样行（只带最少字段）。

    **不在插件侧分页遍历全库**：`api.cards.list(limit=0)` 会一次拉回所有卡，
    上千张时既慢又占内存。这里用 `limit=max_scan` 设一个上界，
    并在结果里如实标注「扫描上限」——宁可样本有偏，也不要拖垮主程序。
    """
    spec = proposal_core.normalize_options(options)

    try:
        payload = api.cards.list(limit=max_scan)
    except Exception:  # noqa: BLE001 - 索引未就绪时降级为空
        logger.warning('读取卡片列表失败', exc_info=True)
        return {'rows': [], 'total': 0, 'scanned': 0, 'capped': False}

    items = payload.get('items') if isinstance(payload, dict) else None
    if not isinstance(items, list):
        items = []

    total = int(payload.get('total') or len(items)) if isinstance(payload, dict) else len(items)

    rows = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        card_id = str(entry.get('id') or entry.get('card_id') or '')
        if not card_id:
            continue

        brief = {
            'name': str(entry.get('char_name') or entry.get('name') or ''),
            'tags': [str(tag) for tag in (entry.get('tags') or []) if str(tag or '').strip()],
            'excerpt': str(entry.get('summary') or entry.get('description') or '')[
                :proposal_core.EXCERPT_CHARS
            ],
        }

        rows.append({
            'card_id': card_id,
            'category': str(entry.get('category') or entry.get('folder') or ''),
            'tags': brief['tags'],
            'brief': brief,
            'tokens': proposal_core._brief_tokens(brief),  # noqa: SLF001
        })

    return {
        'rows': rows,
        'total': total,
        'scanned': len(rows),
        'capped': total > len(rows),
    }


def build_proposal_messages(api, store, profile, config, *, options=None) -> dict:
    """构造「让 AI 提结构方案」的 messages（同时回传采样统计，供 UI 展示）。"""
    spec = proposal_core.normalize_options(options)

    scan = collect_sample_rows(api, options=spec)
    sample = proposal_core.stratified_sample(
        scan['rows'], options=spec,
    )

    categories = []
    if spec['include_existing_categories']:
        try:
            categories = [str(item) for item in (api.cards.list_categories() or [])]
        except Exception:  # noqa: BLE001
            logger.debug('读取分类失败', exc_info=True)

    tags = []
    if spec['include_existing_tags']:
        library = read_tag_library(api)
        tags = [str(item) for item in (library.get('tags') or [])]

    existing_context = proposal_core.format_existing_context(categories, tags)

    variables = {
        'proposal.sample': proposal_core.format_sample(sample['items']),
        'proposal.existing_context': existing_context,
    }

    rendered = prompt_core.render_messages(profile, variables, task='propose')

    instruction = proposal_core.build_proposal_instruction()
    if instruction:
        for message in rendered['messages']:
            if message.get('role') == 'system':
                message['content'] = message['content'] + '\n\n' + instruction
                break
        else:
            rendered['messages'].insert(0, {'role': 'system', 'content': instruction})

    return {
        'rendered': rendered,
        'sample': sample,
        'scan': scan,
        'categories': categories,
        'tags': tags,
        'existing_context': existing_context,
    }


def run_propose(ctx, *, api, store) -> None:
    """结构化提议作业（Agent 阶段一）。

    **整个作业只有一次 LLM 调用**（一个作业项 = 一次提议），
    因为它是元任务：输入是（采样后的）整个卡片库，输出是结构方案本身。
    """
    config = config_module.load_config(api)
    payload = ctx.payload if isinstance(ctx.payload, dict) else {}

    provider_id = str(payload.get('provider_id') or '')
    requested_model = str(payload.get('model') or '')
    profile_id = str(payload.get('profile_id') or '')
    options = payload.get('options') or {}

    total = len(ctx.items) or 1
    ctx.set_total(total)

    try:
        provider = store.resolve_provider(provider_id)
    except AiAssistantError as exc:
        _abort(ctx, exc.code, exc.message, total)
        return

    api_key = store.resolve_provider_key(provider['id'])
    if not api_key:
        _abort(
            ctx, CODE_SECRET_MISSING,
            f'provider「{provider["id"]}」未配置 API Key，请先在插件设置里填写', total,
        )
        return

    extra_headers = store.resolve_provider_headers(provider['id'])
    model_name = requested_model or str(provider.get('model') or '')

    profile = store.get_profile(profile_id) if str(profile_id or '').strip() else None
    if not isinstance(profile, dict):
        profile = store.get_profile('builtin-propose')
    if not isinstance(profile, dict):
        _abort(ctx, CODE_INVALID_PAYLOAD, '找不到结构提议任务规格', total)
        return

    # 作业项只用于承载「一次提议」；真正的输入是采样结果
    item = ctx.items[0] if ctx.items else {}
    key = str((item or {}).get('key') or 'proposal')
    ctx.set_message('正在采样卡片库并生成结构提议…')

    ctx.log(
        'info',
        f'开始结构提议 · provider={provider["id"]} · model={model_name} · '
        f'规格={profile.get("id")}',
    )

    started = time.monotonic()

    try:
        bundle = build_proposal_messages(
            api, store, profile, config, options=options,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception('采样失败')
        ctx.report(key, status='failed', error=f'{type(exc).__name__}: {exc}',
                   result={'error': 'sample_failed', 'message': str(exc)})
        ctx.set_summary({'items': total, 'failed': 1})
        raise AiAssistantError(CODE_INVALID_PAYLOAD, f'采样失败: {exc}') from exc

    sample = bundle['sample']
    if not sample['items']:
        message = '卡片库里没有可采样的卡片'
        ctx.report(key, status='failed', error='empty_library',
                   result={'error': 'empty_library', 'message': message})
        ctx.set_summary({'items': total, 'failed': 1, 'message': message})
        ctx.set_message(message)
        return

    ctx.log(
        'info',
        f'采样 {sample["sampled"]}/{sample["total"]} 张 · '
        f'{sample["tokens"]} token（预算 {sample["budget_tokens"]}）',
    )

    try:
        result = llm.chat(
            bundle['rendered']['messages'],
            provider=provider, api_key=api_key, extra_headers=extra_headers,
            model=model_name,
            temperature=profile.get('temperature'), top_p=profile.get('top_p'),
            max_tokens=profile.get('max_tokens'), stop=profile.get('stop'),
            extra_body=profile.get('extra_body'),
            cancel_check=ctx.should_cancel,
        )
    except AiAssistantError as exc:
        ctx.report(key, status='failed', error=exc.code,
                   result={'error': exc.code, 'message': exc.message})
        if exc.code in FATAL_CODES:
            _abort(ctx, exc.code, exc.message, total)
            return
        raise

    usage = result.get('usage') or {}
    duration_ms = int((time.monotonic() - started) * 1000)

    outcome = parsing.parse_llm_result(
        result.get('content'), profile.get('output_schema'),
        strict=False,
        prefill=str(profile.get('assistant_prefill') or ''),
    )

    if not outcome['ok']:
        message = '；'.join(outcome['issues']) or '输出校验未通过'
        ctx.report(key, status='failed', error=CODE_PARSE_FAILED,
                   result={
                       'error': 'parse_failed', 'message': message,
                       'issues': outcome['issues'],
                       'excerpt': outcome['raw_excerpt'],
                       'duration_ms': duration_ms,
                   })
        store.record_usage({
            'run_id': ctx.run_id, 'job_type': JOB_PROPOSE,
            'provider_id': provider['id'], 'model': model_name,
            'profile_id': profile['id'],
            'prompt_tokens': int(usage.get('prompt_tokens') or 0),
            'completion_tokens': int(usage.get('completion_tokens') or 0),
            'items': 1,
        })
        raise AiAssistantError(CODE_PARSE_FAILED, message) from None

    proposal = proposal_core.normalize_proposal(
        outcome['result'] or {},
        existing_categories=bundle['categories'],
        known_tags=bundle['tags'],
    )
    plan = proposal_core.build_apply_plan(proposal)

    # 采样统计要回传给前端：用户需要知道「方案是基于多少样本提的」
    proposal['sample_meta'] = {
        'sampled': sample['sampled'],
        'total': sample['total'],
        'scanned': bundle['scan']['scanned'],
        'capped': bundle['scan']['capped'],
        'tokens': sample['tokens'],
        'budget_tokens': sample['budget_tokens'],
        'category_coverage': sample['category_coverage'],
    }
    proposal['plan'] = plan
    proposal['warnings'] = list(proposal.get('warnings') or []) + list(outcome['warnings'])
    proposal['usage'] = usage
    proposal['duration_ms'] = duration_ms
    proposal['model'] = model_name
    proposal['profile_id'] = profile['id']

    status = 'failed' if proposal['issues'] else 'awaiting_review'
    ctx.report(key, status=status, result=proposal,
               error='; '.join(proposal['issues']))

    summary = {
        'items': total,
        'awaiting_review': 1 if status == 'awaiting_review' else 0,
        'failed': 1 if status == 'failed' else 0,
        'categories': len(proposal['categories']),
        'new_categories': len(plan['create_categories']),
        'suggested_tags': len(proposal['suggested_tags']),
        'new_tags': len(plan['create_tags']),
        'tag_merges': len(proposal['tag_merges']),
        'sampled': sample['sampled'],
        'sample_total': sample['total'],
        'prompt_tokens': int(usage.get('prompt_tokens') or 0),
        'completion_tokens': int(usage.get('completion_tokens') or 0),
        'provider_id': provider['id'],
        'model': model_name,
        'profile_id': profile['id'],
    }
    ctx.set_summary(summary)
    ctx.set_message(
        f'提议完成：{len(proposal["categories"])} 个分类 · '
        f'{len(proposal["suggested_tags"])} 个标签 · '
        f'{len(proposal["tag_merges"])} 条合并建议'
    )

    try:
        store.record_usage({
            'run_id': ctx.run_id, 'job_type': JOB_PROPOSE,
            'provider_id': provider['id'], 'model': model_name,
            'profile_id': profile['id'],
            'prompt_tokens': int(usage.get('prompt_tokens') or 0),
            'completion_tokens': int(usage.get('completion_tokens') or 0),
            'items': 1,
        })
    except Exception:  # noqa: BLE001
        logger.warning('写入用量台账失败', exc_info=True)

    if status == 'awaiting_review':
        try:
            store.mark_pending_review(ctx.run_id, JOB_PROPOSE, 1)
        except Exception:  # noqa: BLE001
            logger.debug('登记待审批作业失败', exc_info=True)


# ------------------------------------------------------------------ Agent 工具循环


def _read_card_brief(api, card_id, *, detail=agent_core.DETAIL_SUMMARY) -> dict:
    """读一张卡并整理成给模型看的摘要（绝不整卡带出）。"""
    card = api.cards.read(card_id)
    if not isinstance(card, dict):
        return {'name': card_id, 'tags': [], 'category': '',
                'description': '(无法读取)', 'readable': False}

    block = hashing.card_data_block(card)

    tags = block.get('tags') or []
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.split(',') if part.strip()]
    tags = [str(tag).strip() for tag in tags if str(tag or '').strip()]

    brief = {
        'name': hashing.normalize_text(block.get('name')),
        'tags': tags[:12],
        'category': '',
        'description': hashing.normalize_text(block.get('description'))[:400],
        'readable': True,
    }

    if detail == agent_core.DETAIL_FULL:
        brief['personality'] = hashing.normalize_text(block.get('personality'))[:400]
        brief['scenario'] = hashing.normalize_text(block.get('scenario'))[:400]
        brief['first_mes'] = hashing.normalize_text(block.get('first_mes'))[:400]

        names = []
        for entry in _embedded_entries(card):
            name = str(entry.get('comment') or entry.get('name') or '').strip()
            if name:
                names.append(name)
        brief['worldbook'] = names[:10]

    return brief


def run_agent(ctx, *, api, store) -> None:
    """Agent 工具循环：分批读取卡片库，最后产出结构提议。

    循环骨架照 shujuku 的 `WorldSimulationMainLoop_ACU`，但砍到最小：
    `render → invoke → parse → execute → push feedback → continue`，
    动作只有 3 种，没有子代理 / 派工 / 账本 / 事务。

    四条约束缺一不可（都是 shujuku 用真实事故换来的）：
    1. **轮次上限** —— 否则模型可以无限读下去
    2. **读取配额** —— 否则一口气要读 200 张，请求直接超长失败
    3. **读取门禁** —— 超预算时**回灌具体原因**，而不是静默截断
    4. **上下文压缩** —— 丢弃旧读取结果（可重放，所以不需要交接报告）
    """
    config = config_module.load_config(api)
    payload = ctx.payload if isinstance(ctx.payload, dict) else {}

    provider_id = str(payload.get('provider_id') or '')
    requested_model = str(payload.get('model') or '')
    profile_id = str(payload.get('profile_id') or '')
    budget = agent_core.normalize_budget(payload.get('budget'))

    total = len(ctx.items) or 1
    ctx.set_total(total)

    try:
        provider = store.resolve_provider(provider_id)
    except AiAssistantError as exc:
        _abort(ctx, exc.code, exc.message, total)
        return

    api_key = store.resolve_provider_key(provider['id'])
    if not api_key:
        _abort(
            ctx, CODE_SECRET_MISSING,
            f'provider「{provider["id"]}」未配置 API Key，请先在插件设置里填写', total,
        )
        return

    extra_headers = store.resolve_provider_headers(provider['id'])
    model_name = requested_model or str(provider.get('model') or '')

    profile = store.get_profile(profile_id) if str(profile_id or '').strip() else None
    if not isinstance(profile, dict):
        profile = store.get_profile('builtin-agent')
    if not isinstance(profile, dict):
        _abort(ctx, CODE_INVALID_PAYLOAD, '找不到 Agent 任务规格', total)
        return

    item = ctx.items[0] if ctx.items else {}
    key = str((item or {}).get('key') or 'agent')

    # ---- 目录：覆盖全库但便宜 ----
    scan = collect_sample_rows(api, options={'sample_size': 1})
    rows = scan['rows']

    categories, tags = [], []
    try:
        categories = [str(name) for name in (api.cards.list_categories() or [])]
    except Exception:  # noqa: BLE001
        logger.debug('读取分类失败', exc_info=True)
    try:
        library = read_tag_library(api)
        tags = [str(name) for name in (library.get('tags') or [])]
    except Exception:  # noqa: BLE001
        logger.debug('读取标签库失败', exc_info=True)

    catalog = agent_core.build_catalog(rows, budget=budget)
    existing_context = proposal_core.format_existing_context(categories, tags)

    if not catalog['lines']:
        message = '卡片库里没有可分析的卡片'
        ctx.report(key, status='failed', error='empty_library',
                   result={'error': 'empty_library', 'message': message})
        ctx.set_summary({'items': total, 'failed': 1, 'message': message})
        ctx.set_message(message)
        return

    ctx.log(
        'info',
        f'目录就绪：{catalog["stats"]["listed"]}/{catalog["stats"]["total"]} 张 · '
        f'{catalog["stats"]["tokens"]} token',
    )

    instruction = agent_core.build_agent_instruction(
        budget,
        catalog_summary=(
            f'目录规模：{catalog["stats"]["total"]} 张卡，'
            f'已列出 {catalog["stats"]["listed"]} 张。'
        ),
    )

    base_variables = {
        'agent.catalog': agent_core.format_catalog(catalog),
        'agent.existing_context': existing_context,
        'agent.budget_text': f'{budget["max_reads"]} 张卡',
    }

    # ---- 循环状态 ----
    transcript = []
    read_state = {'granted_tokens': 0, 'cards_read': 0}
    repair = agent_core.RepairState(budget['max_protocol_retries'])
    usage_totals = {'prompt_tokens': 0, 'completion_tokens': 0}
    iterations = 0
    finalize_data = None
    blocked_reason = ''
    stop_reason = ''

    ctx.set_message('Agent 正在规划…')

    for iterations in range(1, budget['max_iterations'] + 1):
        if ctx.should_cancel():
            stop_reason = 'cancelled'
            ctx.log('warning', f'收到取消/暂停请求，已在第 {iterations - 1} 轮停下')
            break

        ctx.set_message(
            f'Agent 第 {iterations}/{budget["max_iterations"]} 轮'
            f'（已读 {read_state["cards_read"]}/{budget["max_reads"]} 张）',
        )

        rendered = prompt_core.render_messages(profile, base_variables, task='agent')

        messages = list(rendered['messages'])
        for message in messages:
            if message.get('role') == 'system':
                message['content'] = message['content'] + '\n\n' + instruction
                break
        else:
            messages.insert(0, {'role': 'system', 'content': instruction})

        messages.extend(transcript)

        # ---- 上下文压缩 ----
        reserved = agent_core.measure_messages(messages) - agent_core.measure_messages(transcript)
        compacted = agent_core.compact_transcript(
            transcript, budget, reserved_tokens=reserved,
        )
        if compacted['changed']:
            transcript = compacted['messages']
            messages = list(rendered['messages'])
            for message in messages:
                if message.get('role') == 'system':
                    message['content'] = message['content'] + '\n\n' + instruction
                    break
            else:
                messages.insert(0, {'role': 'system', 'content': instruction})
            messages.extend(transcript)
            ctx.log('info', f'上下文压缩：丢弃 {compacted["dropped"]} 组较早的读取结果')

        try:
            result = llm.chat(
                messages,
                provider=provider, api_key=api_key, extra_headers=extra_headers,
                model=model_name,
                temperature=profile.get('temperature'), top_p=profile.get('top_p'),
                max_tokens=profile.get('max_tokens'), stop=profile.get('stop'),
                extra_body=profile.get('extra_body'),
                cancel_check=ctx.should_cancel,
            )
        except AiAssistantError as exc:
            if exc.code in FATAL_CODES:
                _abort(ctx, exc.code, exc.message, total)
                return
            ctx.report(key, status='failed', error=exc.code,
                       result={'error': exc.code, 'message': exc.message,
                               'iterations': iterations})
            raise

        usage = result.get('usage') or {}
        usage_totals['prompt_tokens'] += int(usage.get('prompt_tokens') or 0)
        usage_totals['completion_tokens'] += int(usage.get('completion_tokens') or 0)

        raw = result.get('content')

        # ---- 解析动作 ----
        try:
            action = agent_core.parse_action(agent_core.extract_json(raw))
        except agent_core.ProtocolError as error:
            outcome = repair.record(error)
            ctx.log('warning', f'第 {iterations} 轮协议未通过：{error.reason_code}')
            if not outcome['retry']:
                ctx.report(key, status='failed', error='protocol_failed',
                           result={
                               'error': 'protocol_failed',
                               'message': str(outcome['issue']),
                               'iterations': iterations,
                               'excerpt': str(raw or '')[:500],
                           })
                ctx.set_summary({
                    'items': total, 'failed': 1, 'iterations': iterations,
                    'cards_read': read_state['cards_read'],
                    'prompt_tokens': usage_totals['prompt_tokens'],
                    'completion_tokens': usage_totals['completion_tokens'],
                })
                raise AiAssistantError(
                    CODE_PARSE_FAILED, f'协议连续未通过：{outcome["issue"]}',
                ) from None

            # 把具体原因回灌给模型，让它自己纠正
            transcript.append({'role': 'assistant', 'content': str(raw or '')})
            transcript.append({
                'role': 'user',
                'content': agent_core.render_rejection(outcome['issue'], budget),
            })
            continue

        ctx.log('info', f'第 {iterations} 轮动作：{action["kind"]}')

        # ---- 搜索 ----
        if action['kind'] == agent_core.ACTION_SEARCH:
            hits = agent_core.search_catalog(rows, action['query'], action['limit'])
            if hits:
                lines = []
                for entry in hits:
                    name = (entry.get('brief') or {}).get('name') or entry.get('card_id')
                    line = f'- {name} [id={entry.get("card_id")}]'
                    entry_tags = entry.get('tags') or []
                    if entry_tags:
                        line += ' 标签:' + ','.join(str(tag) for tag in entry_tags[:5])
                    if entry.get('category'):
                        line += f' 分类:{entry["category"]}'
                    lines.append(line)
                feedback = f'【搜索结果】"{action["query"]}" 命中 {len(hits)} 张：\n' + '\n'.join(lines)
            else:
                feedback = f'【搜索结果】"{action["query"]}" 没有命中任何卡片。'

            transcript.append({'role': 'assistant', 'content': str(raw or '')})
            transcript.append({'role': 'user', 'content': feedback[:agent_core.FEEDBACK_MAX_CHARS]})
            continue

        # ---- 读取 ----
        if action['kind'] == agent_core.ACTION_READ:
            remaining = budget['max_reads'] - read_state['cards_read']
            if remaining <= 0:
                transcript.append({'role': 'assistant', 'content': str(raw or '')})
                transcript.append({'role': 'user', 'content': (
                    f'【读取被拒绝】读取配额已用尽（共 {budget["max_reads"]} 张）。'
                    '请基于已经获得的信息直接 finalize。'
                )})
                continue

            wanted = action['ids'][:remaining]
            prepared = []
            for card_id in wanted:
                brief = _read_card_brief(api, card_id, detail=action['detail'])
                text = agent_core.format_card_text(card_id, brief, action['detail'])
                prepared.append({
                    'card_id': card_id,
                    'label': brief.get('name') or card_id,
                    'text': text,
                    'tokens': agent_core.estimate(text),
                })

            context_tokens = agent_core.measure_messages(messages)
            gate = agent_core.gate_read_batch(
                prepared, read_state, budget, context_tokens=context_tokens,
            )

            transcript.append({'role': 'assistant', 'content': str(raw or '')})

            if not gate['allowed']:
                ctx.log('warning', f'第 {iterations} 轮读取被拒：{gate["reason"]}')
                transcript.append({'role': 'user', 'content': gate['report']})
                continue

            read_state['granted_tokens'] += gate['batch_tokens']
            read_state['cards_read'] += len(prepared)

            body = '\n\n'.join(item['text'] for item in prepared)
            transcript.append({
                'role': 'user',
                'content': f'【读取结果】{len(prepared)} 张卡片：\n\n{body}'[
                    :agent_core.FEEDBACK_MAX_CHARS
                ],
            })
            continue

        # ---- finalize ----
        finalize_data = action['proposal']
        ctx.log('info', f'第 {iterations} 轮 finalize，结束循环')
        break
    else:
        stop_reason = 'max_iterations'
        iterations = budget['max_iterations']

    # ---- 落地 ----
    if finalize_data is None:
        message = (
            '已达轮次上限仍未提交提议' if stop_reason == 'max_iterations'
            else '运行被取消' if stop_reason == 'cancelled'
            else '没有产出提议'
        )
        ctx.report(key, status='failed', error='no_finalize',
                   result={'error': 'no_finalize', 'message': message,
                           'iterations': iterations,
                           'cards_read': read_state['cards_read']})
        ctx.set_summary({
            'items': total, 'failed': 1, 'iterations': iterations,
            'cards_read': read_state['cards_read'],
            'prompt_tokens': usage_totals['prompt_tokens'],
            'completion_tokens': usage_totals['completion_tokens'],
        })
        ctx.set_message(message)
        return

    proposal = proposal_core.normalize_proposal(
        finalize_data, existing_categories=categories, known_tags=tags,
    )
    plan = proposal_core.build_apply_plan(proposal)

    proposal['agent_meta'] = {
        'iterations': iterations,
        'cards_read': read_state['cards_read'],
        'max_reads': budget['max_reads'],
        'catalog_total': catalog['stats']['total'],
        'catalog_listed': catalog['stats']['listed'],
        'catalog_truncated': catalog['stats']['truncated'],
        'read_tokens': read_state['granted_tokens'],
        'stop_reason': stop_reason,
    }
    proposal['plan'] = plan
    proposal['usage'] = usage_totals
    proposal['model'] = model_name
    proposal['profile_id'] = profile['id']

    status = 'failed' if proposal['issues'] else 'awaiting_review'
    ctx.report(key, status=status, result=proposal,
               error='; '.join(proposal['issues']))

    ctx.set_summary({
        'items': total,
        'awaiting_review': 1 if status == 'awaiting_review' else 0,
        'failed': 1 if status == 'failed' else 0,
        'categories': len(proposal['categories']),
        'new_categories': len(plan['create_categories']),
        'suggested_tags': len(proposal['suggested_tags']),
        'new_tags': len(plan['create_tags']),
        'tag_merges': len(proposal['tag_merges']),
        'iterations': iterations,
        'cards_read': read_state['cards_read'],
        'catalog_total': catalog['stats']['total'],
        'prompt_tokens': usage_totals['prompt_tokens'],
        'completion_tokens': usage_totals['completion_tokens'],
        'provider_id': provider['id'],
        'model': model_name,
        'profile_id': profile['id'],
    })
    ctx.set_message(
        f'Agent 完成：{iterations} 轮 · 读了 {read_state["cards_read"]} 张 · '
        f'{len(proposal["categories"])} 个分类 · '
        f'{len(proposal["suggested_tags"])} 个标签'
    )

    try:
        store.record_usage({
            'run_id': ctx.run_id, 'job_type': JOB_AGENT,
            'provider_id': provider['id'], 'model': model_name,
            'profile_id': profile['id'],
            'prompt_tokens': usage_totals['prompt_tokens'],
            'completion_tokens': usage_totals['completion_tokens'],
            'items': 1,
        })
    except Exception:  # noqa: BLE001
        logger.warning('写入用量台账失败', exc_info=True)

    if status == 'awaiting_review':
        try:
            store.mark_pending_review(ctx.run_id, JOB_AGENT, 1)
        except Exception:  # noqa: BLE001
            logger.debug('登记待审批作业失败', exc_info=True)
