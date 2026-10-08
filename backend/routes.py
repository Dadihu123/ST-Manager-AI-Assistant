"""插件 HTTP 路由（前缀由主程序强制为 `/api/plugins/stm-ai-assistant`）。

统一响应格式与主程序一致（`core/plugins/routes.py:31-45`）：
`{"success": true, ...}` / `{"success": false, "msg": "...", "error": "<code>"}`。

密钥纪律：任何响应都不回显 `api_key` / `extra_headers` 明文，只返回
「已设置 / 未设置 + 掩码」（掩码由主程序 `list_secrets()` 提供）。
"""

import json
import logging
import time

from flask import jsonify, request

from . import config as config_module
from . import llm
from . import preview as preview_module
from . import summary_bridge
from .core import hashing
from .core import prompt as prompt_core
from .core.errors import (
    CODE_INVALID_PAYLOAD,
    CODE_JOB_NOT_FOUND,
    CODE_SECRET_MISSING,
    AiAssistantError,
)
from .jobs import (
    JOB_ANALYZE,
    MODE_BOTH,
    SECRET_REF_TEMPLATE,
    VALID_MODES,
    known_tag_set,
    read_tag_library,
    split_tags,
)
from .store import api_key_secret_name

logger = logging.getLogger(__name__)

CARD_LIST_LIMIT = 500
APPLY_ITEM_LIMIT = 20000
RECENT_USAGE_LIMIT = 20
APPLIED_RUNS_KEEP = 20


def _ok(**payload):
    body = {'success': True}
    body.update(payload)
    return jsonify(body)


def _fail(code: str, message: str, *, http_status: int = 400, detail=None):
    body = {'success': False, 'msg': message, 'error': code}
    if detail:
        body['detail'] = detail
    return jsonify(body), http_status


def _handle_errors(func):
    """把 `AiAssistantError` 映射为统一 JSON；其余异常兜底为 500。"""
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except AiAssistantError as exc:
            logger.warning('插件接口 %s 失败: %s %s', func.__name__, exc.code, exc.message)
            return _fail(exc.code, exc.message, http_status=exc.http_status, detail=exc.detail)
        except Exception as exc:  # noqa: BLE001 - 不让 traceback 泄漏到响应
            logger.exception('插件接口 %s 未预期异常', func.__name__)
            return _fail('internal_error', str(exc), http_status=500)

    return wrapper


def _body() -> dict:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def _row_to_dict(row) -> dict:
    if row is None:
        return {}
    if hasattr(row, 'keys'):
        return {key: row[key] for key in row.keys()}
    return {}


def _load_job_items(api, run_id: str, *, limit: int = APPLY_ITEM_LIMIT) -> list:
    """读取作业的全部条目。

    `PluginAPI.get_job()` 固定分页（`items_limit=200`，见 `core/plugins/jobs.py:262-285`），
    审批写回需要完整列表，因此这里用只读 SQL 补齐（`api.db()` 为 Provisional，仅 SELECT）。
    """
    conn = api.db()
    try:
        rows = conn.execute(
            '''
            SELECT item_key, status, seq, payload_json, result_json, error, updated_at
            FROM plugin_job_items
            WHERE run_id = ?
            ORDER BY seq
            LIMIT ?
            ''',
            (str(run_id), int(limit)),
        ).fetchall()
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    items = []
    for row in rows:
        entry = _row_to_dict(row)
        try:
            entry['payload'] = json.loads(entry.pop('payload_json', '') or '{}')
        except (TypeError, ValueError):
            entry['payload'] = {}
        try:
            entry['result'] = json.loads(entry.pop('result_json', '') or '{}')
        except (TypeError, ValueError):
            entry['result'] = {}
        items.append(entry)
    return items


def _normalize_card_ids(raw) -> list:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []

    result = []
    seen = set()
    for item in raw:
        text = str(item or '').strip().replace('\\', '/')
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def register_routes(api, store) -> None:
    """注册全部插件路由。"""

    # ---------------------------------------------------------- 状态与诊断

    @api.route('/status', ['GET'])
    @_handle_errors
    def get_status():
        config = config_module.load_config(api)
        profiles = store.list_profiles()
        providers = store.describe_providers()
        default_id = ''
        for item in providers:
            if item.get('is_default'):
                default_id = item['id']

        return _ok(
            plugin={'id': api.plugin_id, 'version': api.manifest.get('version', ''),
                    'app_version': api.app_version},
            config=config,
            profiles={'count': len(profiles), 'active': store.active_profile_id()},
            providers={'count': len(providers), 'default_id': default_id},
            usage=store.usage_ledger(),
            pending_reviews=store.list_pending_reviews(),
            suggestions=len(store.list_tag_suggestions()),
            outbound_acknowledged=store.outbound_acknowledged(),
        )

    @api.route('/health', ['GET'])
    @_handle_errors
    def get_health():
        """自检：概览 bridge、密钥、provider 是否就绪。"""
        providers = store.describe_providers()
        default_provider = None
        for item in providers:
            if item.get('is_default'):
                default_provider = item

        key_ready = bool(
            default_provider and (default_provider.get('has_api_key') or default_provider.get('has_fallback_key'))
        )
        bridge = summary_bridge.read_summary('__probe__')

        return _ok(
            checks={
                'summary_bridge': {
                    'ok': bool(bridge.get('ok')),
                    'message': bridge.get('message', ''),
                },
                'provider': {
                    'ok': bool(default_provider),
                    'default_id': (default_provider or {}).get('id', ''),
                    'base_url': (default_provider or {}).get('base_url', ''),
                    'model': (default_provider or {}).get('model', ''),
                },
                'api_key': {'ok': key_ready},
            },
        )

    @api.route('/cards', ['GET'])
    @_handle_errors
    def list_cards():
        """卡片候选列表（前端未提供选中集合时的兜底）。"""
        limit = max(1, min(int(request.args.get('limit', CARD_LIST_LIMIT) or CARD_LIST_LIMIT),
                           CARD_LIST_LIMIT))
        keyword = str(request.args.get('q') or '').strip().lower()

        conn = api.db()
        try:
            rows = conn.execute(
                'SELECT id, char_name, tags FROM card_metadata ORDER BY char_name LIMIT ?',
                (CARD_LIST_LIMIT,),
            ).fetchall()
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

        items = []
        for row in rows:
            entry = _row_to_dict(row)
            card_id = str(entry.get('id') or '')
            name = str(entry.get('char_name') or card_id)
            if keyword and keyword not in name.lower() and keyword not in card_id.lower():
                continue
            try:
                tags = json.loads(entry.get('tags') or '[]')
            except (TypeError, ValueError):
                tags = []
            items.append({'card_id': card_id, 'name': name,
                          'tags': tags if isinstance(tags, list) else []})
            if len(items) >= limit:
                break

        return _ok(items=items, total=len(items))

    # ---------------------------------------------------------- 提示词档案

    @api.route('/profiles', ['GET'])
    @_handle_errors
    def list_profiles():
        profiles = store.list_profiles()
        index = store.profile_index()
        store.sync_profile_index()
        return _ok(
            items=[
                {
                    'index': entry,
                    'system_preview': str(
                        (next((item for item in profiles if item['id'] == entry['id']), {}) or {}).get('system', '')
                    )[:200],
                }
                for entry in index
            ],
            active=store.active_profile_id(),
        )

    @api.route('/profiles/<profile_id>', ['GET'])
    @_handle_errors
    def get_profile(profile_id):
        profile = store.get_profile(profile_id)
        if profile is None:
            return _fail('profile_not_found', f'提示词档案不存在: {profile_id}', http_status=404)
        return _ok(profile=profile)

    @api.route('/profiles', ['POST'])
    @_handle_errors
    def save_profile():
        payload = _body()
        profile = store.save_profile(payload.get('profile', payload))
        return _ok(profile=profile)

    @api.route('/profiles/<profile_id>', ['DELETE'])
    @_handle_errors
    def delete_profile(profile_id):
        return _ok(**store.delete_profile(profile_id))

    @api.route('/profiles/<profile_id>/duplicate', ['POST'])
    @_handle_errors
    def duplicate_profile(profile_id):
        payload = _body()
        profile = store.duplicate_profile(
            profile_id, new_id=str(payload.get('id') or ''),
            new_name=str(payload.get('name') or ''),
        )
        return _ok(profile=profile)

    @api.route('/profiles/active', ['POST'])
    @_handle_errors
    def set_active_profile():
        payload = _body()
        profile_id = str(payload.get('profile_id') or '')
        if profile_id and store.get_profile(profile_id) is None:
            return _fail('profile_not_found', f'提示词档案不存在: {profile_id}', http_status=404)
        store.set_active_profile(profile_id)
        return _ok(active=profile_id)

    @api.route('/profiles/reset', ['POST'])
    @_handle_errors
    def reset_profiles():
        return _ok(**store.reset_profiles())

    @api.route('/profiles/export', ['GET'])
    @_handle_errors
    def export_profiles():
        return _ok(**store.export_profiles(str(request.args.get('profile_id') or '')))

    @api.route('/profiles/import', ['POST'])
    @_handle_errors
    def import_profiles():
        payload = _body()
        return _ok(**store.import_profiles(payload.get('payload', payload)))

    # ---------------------------------------------------------- provider

    @api.route('/providers', ['GET'])
    @_handle_errors
    def list_providers():
        return _ok(items=store.describe_providers())

    @api.route('/providers', ['POST'])
    @_handle_errors
    def save_provider():
        payload = _body()
        provider = store.save_provider(payload.get('provider', payload))
        return _ok(provider=provider)

    @api.route('/providers/<provider_id>', ['DELETE'])
    @_handle_errors
    def delete_provider(provider_id):
        return _ok(**store.delete_provider(provider_id))

    @api.route('/providers/<provider_id>/default', ['POST'])
    @_handle_errors
    def set_default_provider(provider_id):
        return _ok(**store.set_default_provider(provider_id))

    @api.route('/providers/<provider_id>/secrets', ['POST'])
    @_handle_errors
    def save_provider_secrets(provider_id):
        payload = _body()
        result = store.set_provider_secrets(
            provider_id,
            api_key=payload.get('api_key'),
            extra_headers=payload.get('extra_headers'),
        )
        return _ok(result=result)

    @api.route('/providers/<provider_id>/secrets', ['DELETE'])
    @_handle_errors
    def clear_provider_secrets(provider_id):
        if store.get_provider(provider_id) is None:
            return _fail('provider_not_found', f'provider 不存在: {provider_id}', http_status=404)
        store.clear_provider_secrets(provider_id)
        return _ok(cleared=True)

    @api.route('/providers/<provider_id>/test', ['POST'])
    @_handle_errors
    def test_provider(provider_id):
        provider = store.get_provider(provider_id)
        if provider is None:
            return _fail('provider_not_found', f'provider 不存在: {provider_id}', http_status=404)

        api_key = store.resolve_provider_key(provider['id'])
        if not api_key:
            return _fail(CODE_SECRET_MISSING, f'provider「{provider["id"]}」未配置 API Key', http_status=409)

        payload = _body()
        model = str(payload.get('model') or provider.get('model') or '')
        started = time.monotonic()
        try:
            result = llm.probe(
                provider, api_key, model=model,
                transport=_injected_transport(), sleep=_injected_sleep(),
            )
        except AiAssistantError as exc:
            return _ok(ok=False, error=exc.code, message=exc.message,
                       latency_ms=int((time.monotonic() - started) * 1000))

        return _ok(**result)

    # ---------------------------------------------------------- 预览与提交

    @api.route('/prompt/preview', ['POST'])
    @_handle_errors
    def preview_prompt():
        payload = _body()
        config = config_module.load_config(api)
        result = preview_module.build_preview(
            api=api, store=store, config=config,
            card_id=str(payload.get('card_id') or ''),
            profile_id=str(payload.get('profile_id') or ''),
            provider_id=str(payload.get('provider_id') or ''),
            model=str(payload.get('model') or ''),
            budget_tokens=int(payload.get('budget_tokens') or 0),
        )
        return _ok(**result)

    @api.route('/jobs/analyze', ['POST'])
    @_handle_errors
    def submit_analyze():
        payload = _body()
        config = config_module.load_config(api)

        card_ids = _normalize_card_ids(payload.get('card_ids'))
        if not card_ids:
            return _fail(CODE_INVALID_PAYLOAD, 'card_ids 不能为空')

        mode = str(payload.get('mode') or MODE_BOTH).strip().lower()
        if mode not in VALID_MODES:
            return _fail(CODE_INVALID_PAYLOAD, f'mode 必须是 {list(VALID_MODES)} 之一')

        provider = store.resolve_provider(str(payload.get('provider_id') or ''))
        api_key = store.resolve_provider_key(provider['id'])
        if not api_key:
            return _fail(
                CODE_SECRET_MISSING,
                f'provider「{provider["id"]}」未配置 API Key，请先在插件设置里填写',
                http_status=409,
                detail={'provider_id': provider['id'], 'secret_key': api_key_secret_name(provider['id'])},
            )

        model_name = str(payload.get('model') or provider.get('model') or '')
        profile = store.resolve_profile(str(payload.get('profile_id') or ''), model_name)

        apply_mode = str(payload.get('apply_mode') or config['apply_mode']).strip().lower()
        if apply_mode not in ('auto', 'review'):
            apply_mode = config['apply_mode']

        force = bool(payload.get('force'))
        skip_unchanged = bool(payload.get('skip_unchanged', config['skip_unchanged']))

        items = []
        skipped = []
        sample_cards = []

        for card_id in card_ids:
            card = api.read_card(card_id)
            if not isinstance(card, dict):
                skipped.append({'card_id': card_id, 'reason': 'card_unreadable'})
                continue

            ui_key = summary_bridge.resolve_key(card_id)
            content_hash = hashing.card_content_hash(card)
            meta = store.get_card_meta(ui_key)

            unchanged = (
                meta.get('content_hash') == content_hash
                and str(meta.get('profile_id') or '') == str(profile.get('id') or '')
                and str(meta.get('model') or '') == str(model_name or '')
            )
            if skip_unchanged and not force and unchanged:
                skipped.append({'card_id': card_id, 'reason': 'unchanged'})
                continue

            items.append({
                'key': ui_key or card_id,
                'card_id': card_id,
                'ui_key': ui_key or card_id,
                'mode': mode,
                'content_hash': content_hash,
            })
            if len(sample_cards) < 3:
                sample_cards.append(card)

        if not items:
            return _ok(run_id='', total=0, skipped=len(skipped), details=skipped,
                       message='没有需要处理的卡片（内容未变化或无法读取）')

        estimate = preview_module.estimate_run(
            api=api, config=config, cards=sample_cards,
            profile=profile, total=len(items),
            budget_tokens=int(payload.get('budget_tokens') or 0),
        )

        job_payload = {
            'provider_id': provider['id'],
            'model': model_name,
            'profile_id': str(profile.get('id') or ''),
            'apply_mode': apply_mode,
            'dry_run': bool(payload.get('dry_run')),
            'force': force,
            'apply_tags': bool(payload.get('apply_tags', config['apply_tags'])),
            'strict': bool(payload.get('strict', config['validation_strict'])),
            'budget_tokens': int(payload.get('budget_tokens') or 0),
            'secret_ref': SECRET_REF_TEMPLATE.format(provider_id=provider['id']),
        }

        try:
            run_id = api.submit_job(JOB_ANALYZE, items=items, payload=job_payload)
        except Exception as exc:  # noqa: BLE001 - 主程序用 job_already_running 表达并发冲突
            message = str(exc)
            if '已在运行' in message or 'already' in message.lower():
                return _fail('job_already_running', message, http_status=409)
            raise

        return _ok(
            run_id=run_id,
            total=len(items),
            skipped=len(skipped),
            details=skipped[:50],
            estimate=estimate,
            profile={'id': profile.get('id'), 'name': profile.get('name')},
            provider={'id': provider.get('id'), 'model': model_name},
            apply_mode=apply_mode,
        )

    # ---------------------------------------------------------- 审批与结果

    @api.route('/jobs/<run_id>', ['GET'])
    @_handle_errors
    def get_job(run_id):
        job = api.get_job(run_id)
        if job is None:
            return _fail(CODE_JOB_NOT_FOUND, f'作业不存在: {run_id}', http_status=404)

        items = _load_job_items(api, run_id)
        return _ok(job=_job_summary(job), items=items, items_total=len(items))

    @api.route('/jobs/<run_id>/apply', ['POST'])
    @_handle_errors
    def apply_job(run_id):
        """把已审批（`status=done`）的条目写回卡片。"""
        job = api.get_job(run_id)
        if job is None:
            return _fail(CODE_JOB_NOT_FOUND, f'作业不存在: {run_id}', http_status=404)

        job_type = str(job.get('job_type') or '')
        if job_type != JOB_ANALYZE:
            return _fail(CODE_INVALID_PAYLOAD, f'该作业类型不支持应用: {job_type}')

        payload = _body()
        force = bool(payload.get('force'))
        apply_tags_enabled = bool(payload.get('apply_tags', True))

        items = _load_job_items(api, run_id)
        applied = 0
        skipped = 0
        stale = 0
        failed = 0
        details = []

        for item in items:
            if str(item.get('status') or '') != 'done':
                continue

            result = item.get('result') if isinstance(item.get('result'), dict) else {}
            ui_key = str(result.get('ui_key') or item.get('item_key') or '')
            card_id = str(result.get('card_id') or item.get('item_key') or '')
            if not card_id:
                continue

            meta = store.get_card_meta(ui_key)
            applied_runs = meta.get('applied_runs') if isinstance(meta.get('applied_runs'), list) else []
            if run_id in applied_runs:
                skipped += 1
                continue

            card = api.read_card(card_id)
            if not isinstance(card, dict):
                failed += 1
                details.append({'card_id': card_id, 'error': 'card_unreadable'})
                continue

            if not force:
                current_hash = hashing.card_content_hash(card)
                expected = str(result.get('content_hash') or '')
                if expected and current_hash != expected:
                    stale += 1
                    details.append({'card_id': card_id, 'error': 'apply_stale'})
                    continue

            summary_text = str(result.get('summary') or '').strip()
            tags = result.get('tags') if isinstance(result.get('tags'), list) else []

            if summary_text:
                bridge_result = summary_bridge.write_summary(
                    store, card_id, summary_text, ui_key=ui_key)
                if not bridge_result.get('ok'):
                    details.append({'card_id': card_id,
                                    'warning': f'概览降级写入: {bridge_result.get("message", "")}'})

            if tags and apply_tags_enabled:
                tag_library = read_tag_library(api)
                accepted, _suggested = split_tags(tags, known_tag_set(tag_library))
                if accepted:
                    tag_result = api.apply_tags([card_id], add=accepted, replace=False)
                    if tag_result.get('failed'):
                        failed += 1
                        details.append({'card_id': card_id, 'error': 'write_failed'})
                        continue

            applied_runs.append(run_id)
            store.update_card_meta(ui_key, {
                'applied_runs': applied_runs[-APPLIED_RUNS_KEEP:],
                'applied_at': int(time.time()),
                'summary': summary_text,
                'tags_applied': [str((tag or {}).get('name') or '') for tag in tags],
                'last_run_id': run_id,
            })
            applied += 1

        if applied:
            store.clear_pending_review(run_id)

        return _ok(applied=applied, skipped=skipped, stale=stale, failed=failed,
                   details=details[:50], total=len(items))

    @api.route('/jobs/<run_id>/cancel', ['POST'])
    @_handle_errors
    def cancel_job(run_id):
        if api.get_job(run_id) is None:
            return _fail(CODE_JOB_NOT_FOUND, f'作业不存在: {run_id}', http_status=404)
        return _ok(cancelled=bool(api.cancel_job(run_id)))

    @api.route('/jobs/<run_id>/retry', ['POST'])
    @_handle_errors
    def retry_job(run_id):
        if api.get_job(run_id) is None:
            return _fail(CODE_JOB_NOT_FOUND, f'作业不存在: {run_id}', http_status=404)
        payload = _body()
        new_run = api.retry_job(run_id, only_failed=bool(payload.get('only_failed', True)))
        return _ok(run_id=new_run)

    # ---------------------------------------------------------- 建议队列与用量

    @api.route('/suggestions', ['GET'])
    @_handle_errors
    def list_suggestions():
        queue = store.list_tag_suggestions()
        items = []
        for name, record in queue.items():
            items.append({
                'name': name,
                'count': int((record or {}).get('count') or 0),
                'cards': list((record or {}).get('cards') or []),
                'reason': str((record or {}).get('reason') or ''),
                'first_seen': int((record or {}).get('first_seen') or 0),
                'last_seen': int((record or {}).get('last_seen') or 0),
            })
        items.sort(key=lambda item: (-item['count'], item['name']))
        return _ok(items=items, total=len(items))

    @api.route('/suggestions/clear', ['POST'])
    @_handle_errors
    def clear_suggestions():
        payload = _body()
        names = payload.get('names')
        return _ok(**store.clear_tag_suggestions(names if isinstance(names, list) else None))

    @api.route('/usage', ['GET'])
    @_handle_errors
    def get_usage():
        return _ok(
            ledger=store.usage_ledger(),
            recent=store.recent_usage(RECENT_USAGE_LIMIT),
        )

    @api.route('/outbound/ack', ['POST'])
    @_handle_errors
    def ack_outbound():
        store.acknowledge_outbound()
        return _ok(acknowledged=True)

    @api.route('/prompt/validate', ['POST'])
    @_handle_errors
    def validate_template():
        """校验模板片段（保存档案前的即时反馈）。"""
        payload = _body()
        templates = payload.get('templates')
        if isinstance(templates, str):
            templates = [templates]
        if not isinstance(templates, list):
            return _fail(CODE_INVALID_PAYLOAD, 'templates 必须是字符串数组')

        unknown = prompt_core.validate_templates([str(item) for item in templates])
        used = []
        for item in templates:
            for name in prompt_core.scan_template(str(item)):
                if name not in used:
                    used.append(name)

        return _ok(unknown=unknown, variables=used,
                   known=sorted(prompt_core.KNOWN_VARIABLES))


def _injected_transport():
    """测试注入点：通过模块属性替换。"""
    return getattr(llm, '_TEST_TRANSPORT', None)


def _injected_sleep():
    return getattr(llm, '_TEST_SLEEP', None)


def _job_summary(job) -> dict:
    """只回传前端需要的作业字段（不含 payload，避免密钥引用等细节外泄）。"""
    return {
        'run_id': str(job.get('run_id') or ''),
        'job_type': str(job.get('job_type') or ''),
        'status': str(job.get('status') or ''),
        'total': int(job.get('total') or 0),
        'completed': int(job.get('completed') or 0),
        'failed': int(job.get('failed') or 0),
        'skipped': int(job.get('skipped') or 0),
        'current_item': str(job.get('current_item') or ''),
        'message': str(job.get('message') or ''),
        'summary': job.get('summary') if isinstance(job.get('summary'), dict) else {},
        'created_at': float(job.get('created_at') or 0),
        'finished_at': float(job.get('finished_at') or 0),
    }



