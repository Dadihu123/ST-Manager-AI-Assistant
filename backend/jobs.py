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

from . import config as config_module
from . import llm
from . import summary_bridge
from .core import hashing
from .core import parsing
from .core import prompt as prompt_core
from .core import trimming
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
)

logger = logging.getLogger(__name__)

JOB_ANALYZE = 'ai.analyze'

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

    api.register_job(JOB_ANALYZE, analyze)


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
        payload = api.read_tag_library()
    except Exception:  # noqa: BLE001 - 标签库不可用不应中断分析
        logger.warning('读取标签库失败', exc_info=True)
        return {'tags': [], 'taxonomy': {}, 'prefs': {}}
    return payload if isinstance(payload, dict) else {'tags': [], 'taxonomy': {}, 'prefs': {}}


def tag_usage(api) -> dict:
    """统计标签使用频次（`api.db()` 是 Provisional，失败即返回空表）。"""
    try:
        conn = api.db()
    except Exception:  # noqa: BLE001
        return {}

    try:
        rows = conn.execute('SELECT tags FROM card_metadata').fetchall()
    except Exception:  # noqa: BLE001
        logger.debug('统计标签频次失败', exc_info=True)
        return {}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    counts = {}
    for row in rows:
        try:
            raw = row['tags'] if hasattr(row, 'keys') else row[0]
        except Exception:  # noqa: BLE001
            continue
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


def split_tags(tags, known: set) -> tuple:
    """把 AI 标签拆成「可写入」与「新标签建议」两部分。"""
    accepted = []
    suggested = []
    for item in tags or []:
        name = str((item or {}).get('name') or '').strip()
        if not name:
            continue
        if name in known:
            accepted.append(name)
        else:
            suggested.append({'name': name, 'reason': '标签库中不存在'})
    return accepted, suggested


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

    card = api.read_card(card_id)
    if not isinstance(card, dict):
        raise AiAssistantError(CODE_CARD_UNREADABLE, f'无法读取卡片: {card_id}')

    # ---- 世界书分层取样 ----
    entries = _embedded_entries(card)
    excerpt = trimming.select_wi_excerpt(entries, wi_budget, keywords=keywords)

    # ---- 渲染 ----
    variables = prompt_core.collect_variables(
        card=card, excerpt=excerpt, tag_library=tag_library, usage=usage_map,
        limits=limits, model_name=model_name, profile_id=str(profile.get('id') or ''),
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

    # ---- 解析与校验 ----
    outcome = parsing.parse_llm_result(
        result.get('content'), profile.get('format_rules'), limits, strict=strict,
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
    tags = parsed.get('tags') or []
    accepted_tags, suggested_from_tags = split_tags(tags, known_tags)
    suggestions = merge_suggestions(parsed.get('suggested_new_tags'), suggested_from_tags)

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

    # ---- auto：直接写回 ----
    applied = False
    if mode in (MODE_BOTH, MODE_SUMMARY) and summary_text:
        bridge_result = summary_bridge.write_summary(store, card_id, summary_text, ui_key=ui_key)
        result_payload['summary_origin'] = bridge_result.get('origin', '')
        if not bridge_result.get('ok'):
            warnings.append(f'概览未写入主程序：{bridge_result.get("message", "")}')
        else:
            applied = True

    if mode in (MODE_BOTH, MODE_TAGS) and accepted_tags:
        if not apply_tags_enabled:
            warnings.append('已按配置跳过标签写入')
        else:
            tag_result = api.apply_tags([card_id], add=accepted_tags, replace=False)
            result_payload['tags_applied'] = list(tag_result.get('added') or [])
            if tag_result.get('skipped_blacklist'):
                warnings.append('被黑名单拦截的标签: ' + '、'.join(tag_result['skipped_blacklist']))
            if tag_result.get('skipped_unknown'):
                warnings.append('因标签库锁定被跳过的标签: ' + '、'.join(tag_result['skipped_unknown']))
            if tag_result.get('failed'):
                warnings.append(f'标签写入失败: {len(tag_result["failed"])} 张卡片')
                raise AiAssistantError(CODE_WRITE_FAILED, f'标签写入失败: {card_id}')
            if tag_result.get('updated'):
                applied = True

    if suggestions:
        try:
            store.record_tag_suggestions(suggestions, card_key=ui_key)
        except Exception:  # noqa: BLE001
            logger.warning('记录新标签建议失败', exc_info=True)

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
