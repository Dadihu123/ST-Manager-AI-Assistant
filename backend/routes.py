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
from . import jobs as jobs_module
from . import llm
from . import preview as preview_module
from . import summary_bridge
from .core import agent as agent_core
from .core import classify as classify_core
from .core import hashing
from .core import proposal as proposal_core
from .core import prompt as prompt_core
from .core import wicheck as wicheck_core
from .core import tokens as tokens_core
from .core.errors import (
    CODE_INVALID_PAYLOAD,
    CODE_JOB_NOT_FOUND,
    CODE_SECRET_MISSING,
    AiAssistantError,
)
from .jobs import (
    JOB_ANALYZE,
    JOB_AGENT,
    JOB_CLASSIFY,
    JOB_PROPOSE,
    JOB_WI_REWRITE,
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


def _load_job_items(api, run_id: str, *, limit: int = APPLY_ITEM_LIMIT) -> list:
    """读取作业的全部条目。

    `api.jobs.get()` 的条目是分页的，审批写回需要完整列表，
    因此这里走 `api.jobs.items()`（`limit=0` 即不设上限，内部仍受单作业上限保护）。
    """
    try:
        items = api.jobs.items(run_id, limit=limit)
    except Exception:  # noqa: BLE001 - 作业不存在/越权时按空列表处理
        logger.debug('读取作业条目失败: %s', run_id, exc_info=True)
        return []

    return [item for item in (items or []) if isinstance(item, dict)]


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

    @api.http.route('/status', ['GET'])
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

    @api.http.route('/health', ['GET'])
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
        bridge = summary_bridge.read_summary(api, '__probe__')

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

    @api.http.route('/cards', ['GET'])
    @_handle_errors
    def list_cards():
        """卡片候选列表（前端未提供选中集合时的兜底）。

        走 `api.cards.list()`（宿主索引查询），不再裸查 `card_metadata`。
        """
        limit = max(1, min(int(request.args.get('limit', CARD_LIST_LIMIT) or CARD_LIST_LIMIT),
                           CARD_LIST_LIMIT))
        keyword = str(request.args.get('q') or '').strip()

        try:
            result = api.cards.list(query=keyword, limit=limit)
        except Exception:  # noqa: BLE001 - 索引未就绪时降级为空列表
            logger.warning('读取卡片候选列表失败', exc_info=True)
            return _ok(items=[], total=0)

        items = []
        for entry in (result.get('items') or []):
            if not isinstance(entry, dict):
                continue
            card_id = str(entry.get('id') or entry.get('card_id') or '')
            if not card_id:
                continue
            name = str(entry.get('char_name') or entry.get('name') or card_id)
            tags = entry.get('tags')
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags or '[]')
                except (TypeError, ValueError):
                    tags = []
            items.append({
                'card_id': card_id,
                'name': name,
                'tags': tags if isinstance(tags, list) else [],
            })

        return _ok(items=items, total=int(result.get('total') or len(items)))

    # ---------------------------------------------------------- 提示词档案

    @api.http.route('/profiles', ['GET'])
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

    @api.http.route('/profiles/<profile_id>', ['GET'])
    @_handle_errors
    def get_profile(profile_id):
        profile = store.get_profile(profile_id)
        if profile is None:
            return _fail('profile_not_found', f'提示词档案不存在: {profile_id}', http_status=404)
        return _ok(profile=profile)

    @api.http.route('/profiles', ['POST'])
    @_handle_errors
    def save_profile():
        payload = _body()
        profile = store.save_profile(payload.get('profile', payload))
        return _ok(profile=profile)

    @api.http.route('/profiles/<profile_id>', ['DELETE'])
    @_handle_errors
    def delete_profile(profile_id):
        return _ok(**store.delete_profile(profile_id))

    @api.http.route('/profiles/<profile_id>/duplicate', ['POST'])
    @_handle_errors
    def duplicate_profile(profile_id):
        payload = _body()
        profile = store.duplicate_profile(
            profile_id, new_id=str(payload.get('id') or ''),
            new_name=str(payload.get('name') or ''),
        )
        return _ok(profile=profile)

    @api.http.route('/profiles/active', ['POST'])
    @_handle_errors
    def set_active_profile():
        payload = _body()
        profile_id = str(payload.get('profile_id') or '')
        if profile_id and store.get_profile(profile_id) is None:
            return _fail('profile_not_found', f'提示词档案不存在: {profile_id}', http_status=404)
        store.set_active_profile(profile_id)
        return _ok(active=profile_id)

    @api.http.route('/profiles/reset', ['POST'])
    @_handle_errors
    def reset_profiles():
        return _ok(**store.reset_profiles())

    @api.http.route('/profiles/export', ['GET'])
    @_handle_errors
    def export_profiles():
        return _ok(**store.export_profiles(str(request.args.get('profile_id') or '')))

    @api.http.route('/profiles/import', ['POST'])
    @_handle_errors
    def import_profiles():
        payload = _body()
        return _ok(**store.import_profiles(payload.get('payload', payload)))

    # ---------------------------------------------------------- provider

    @api.http.route('/providers', ['GET'])
    @_handle_errors
    def list_providers():
        return _ok(items=store.describe_providers())

    @api.http.route('/providers', ['POST'])
    @_handle_errors
    def save_provider():
        payload = _body()
        provider = store.save_provider(payload.get('provider', payload))
        return _ok(provider=provider)

    @api.http.route('/providers/<provider_id>', ['DELETE'])
    @_handle_errors
    def delete_provider(provider_id):
        return _ok(**store.delete_provider(provider_id))

    @api.http.route('/providers/<provider_id>/default', ['POST'])
    @_handle_errors
    def set_default_provider(provider_id):
        return _ok(**store.set_default_provider(provider_id))

    @api.http.route('/providers/<provider_id>/secrets', ['POST'])
    @_handle_errors
    def save_provider_secrets(provider_id):
        payload = _body()
        result = store.set_provider_secrets(
            provider_id,
            api_key=payload.get('api_key'),
            extra_headers=payload.get('extra_headers'),
        )
        return _ok(result=result)

    @api.http.route('/providers/<provider_id>/secrets', ['DELETE'])
    @_handle_errors
    def clear_provider_secrets(provider_id):
        if store.get_provider(provider_id) is None:
            return _fail('provider_not_found', f'provider 不存在: {provider_id}', http_status=404)
        store.clear_provider_secrets(provider_id)
        return _ok(cleared=True)

    @api.http.route('/providers/<provider_id>/test', ['POST'])
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

    @api.http.route('/providers/<provider_id>/models', ['POST'])
    @_handle_errors
    def list_provider_models(provider_id):
        """拉取该 provider 的模型列表（`GET /models`）。

        **永不因拉取失败而返回 4xx**：很多第三方网关不实现 `/models`，
        拉取失败只是让 UI 退回手填，不该阻塞配置。
        """
        provider = store.get_provider(provider_id)
        if provider is None:
            return _fail('provider_not_found', f'provider 不存在: {provider_id}', http_status=404)

        api_key = store.resolve_provider_key(provider['id'])
        if not api_key:
            return _ok(
                ok=False, models=[], protocol=provider.get('api_style', ''),
                message=f'provider「{provider["id"]}」未配置 API Key',
            )

        payload = _body()
        base_url = str(payload.get('base_url') or provider.get('base_url') or '')
        candidate = dict(provider)
        if base_url:
            candidate['base_url'] = base_url
        if payload.get('api_style'):
            candidate['api_style'] = payload['api_style']

        result = llm.list_models(
            candidate, api_key,
            extra_headers=store.resolve_provider_headers(provider['id']),
            transport=_injected_get(),
        )
        return _ok(**result)

    # ---------------------------------------------------------- 预览与提交

    @api.http.route('/prompt/preview', ['POST'])
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

    @api.http.route('/jobs/analyze', ['POST'])
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
            card = api.cards.read(card_id)
            if not isinstance(card, dict):
                skipped.append({'card_id': card_id, 'reason': 'card_unreadable'})
                continue

            ui_key = summary_bridge.resolve_key(api, card_id)
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
            run_id = api.jobs.submit(JOB_ANALYZE, items=items, payload=job_payload)
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

    # ---------------------------------------------------------- 世界书改写

    @api.http.route('/wi/entries', ['POST'])
    @_handle_errors
    def list_wi_entries():
        """列出某个世界书来源的条目（供前端做条目选择）。"""
        payload = _body()
        card_id = str(payload.get('card_id') or '')
        source = jobs_module.resolve_wi_source(card_id, payload.get('worldbook_source'))

        book = api.worldbook.read(source)
        if not isinstance(book, dict):
            return _fail('worldbook_unreadable', '无法读取世界书', http_status=404)

        entries = book.get('entries')
        if isinstance(entries, dict):
            entries = list(entries.values())
        if not isinstance(entries, list):
            return _fail('worldbook_unreadable', '世界书结构异常（无 entries）')

        items = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            content = str(entry.get('content') or '')
            items.append({
                'uid': str(entry.get('uid') or entry.get('id') or ''),
                'name': str(entry.get('comment') or entry.get('name') or ''),
                'keys': [str(item) for item in (entry.get('keys') or [])],
                'tokens': tokens_core.estimate(content),
                'enabled': not bool(entry.get('disable')),
                'constant': bool(entry.get('constant')),
                'content_preview': content[:200],
            })

        return _ok(
            items=items,
            total=len(items),
            source_revision=str(book.get('source_revision') or ''),
        )

    @api.http.route('/jobs/wi-rewrite', ['POST'])
    @_handle_errors
    def submit_wi_rewrite():
        """提交世界书改写作业（一个作业项 = 一条条目）。"""
        payload = _body()
        config = config_module.load_config(api)

        card_id = str(payload.get('card_id') or '')
        source = jobs_module.resolve_wi_source(card_id, payload.get('worldbook_source'))
        if not card_id and not source.get('file_path'):
            return _fail(CODE_INVALID_PAYLOAD, '需要 card_id 或 worldbook_source')

        entry_uids = payload.get('entry_uids')
        if isinstance(entry_uids, str):
            entry_uids = [entry_uids]
        entry_uids = [str(item) for item in (entry_uids or []) if str(item or '').strip()]

        provider = store.resolve_provider(str(payload.get('provider_id') or ''))
        api_key = store.resolve_provider_key(provider['id'])
        if not api_key:
            return _fail(
                CODE_SECRET_MISSING,
                f'provider「{provider["id"]}」未配置 API Key，请先在插件设置里填写',
                http_status=409,
                detail={'provider_id': provider['id'],
                        'secret_key': api_key_secret_name(provider['id'])},
            )

        model_name = str(payload.get('model') or provider.get('model') or '')
        profile = store.resolve_profile(str(payload.get('profile_id') or ''), model_name)

        apply_mode = str(payload.get('apply_mode') or config['apply_mode']).strip().lower()
        if apply_mode not in ('auto', 'review'):
            apply_mode = config['apply_mode']

        # 读一次世界书，把条目名 / token 一并带上（前端与结果页要用）
        book = api.worldbook.read(source)
        if not isinstance(book, dict):
            return _fail('worldbook_unreadable', '无法读取世界书', http_status=404)

        entries = book.get('entries')
        if isinstance(entries, dict):
            entries = list(entries.values())
        if not isinstance(entries, list):
            return _fail('worldbook_unreadable', '世界书结构异常（无 entries）')

        wanted = set(entry_uids)
        items = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            uid = str(entry.get('uid') or entry.get('id') or '')
            if not uid:
                continue
            if wanted and uid not in wanted:
                continue
            if entry.get('disable') is True:
                continue
            content = str(entry.get('content') or '')
            if not content.strip():
                continue
            items.append({
                'key': uid,
                'card_id': card_id,
                'entry_uid': uid,
                'entry_name': str(entry.get('comment') or entry.get('name') or ''),
                'tokens': tokens_core.estimate(content),
            })

        if not items:
            return _fail(CODE_INVALID_PAYLOAD,
                         '没有可改写的条目（未选、已停用或内容为空）', http_status=400)

        job_payload = {
            'provider_id': provider['id'],
            'model': model_name,
            'profile_id': str(profile.get('id') or ''),
            'apply_mode': apply_mode,
            'dry_run': bool(payload.get('dry_run')),
            'force': bool(payload.get('force')),
            'target_format': str(payload.get('target_format') or 'yaml'),
            'worldbook_source': source,
            'secret_ref': SECRET_REF_TEMPLATE.format(provider_id=provider['id']),
        }

        try:
            run_id = api.jobs.submit(JOB_WI_REWRITE, items=items, payload=job_payload)
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if '已在运行' in message or 'already' in message.lower():
                return _fail('job_already_running', message, http_status=409)
            raise

        return _ok(
            run_id=run_id,
            total=len(items),
            profile={'id': profile.get('id'), 'name': profile.get('name')},
            provider={'id': provider.get('id'), 'model': model_name},
            apply_mode=apply_mode,
            target_format=job_payload['target_format'],
        )

    # ---------------------------------------------------------- 自动归类

    @api.http.route('/categories', ['GET'])
    @_handle_errors
    def list_categories():
        """列出主程序现有的分类（供归类方案做初始值）。"""
        try:
            items = api.cards.list_categories()
        except Exception:  # noqa: BLE001 - 索引不可用时降级为空
            logger.warning('读取分类列表失败', exc_info=True)
            items = []
        return _ok(items=[str(item) for item in (items or []) if str(item or '').strip()])

    @api.http.route('/jobs/classify', ['POST'])
    @_handle_errors
    def submit_classify():
        """提交自动归类作业。"""
        payload = _body()
        config = config_module.load_config(api)

        card_ids = _normalize_card_ids(payload.get('card_ids'))
        if not card_ids:
            return _fail(CODE_INVALID_PAYLOAD, 'card_ids 不能为空')

        scheme = classify_core.normalize_scheme(payload.get('scheme'))
        allow_new = bool(payload.get('allow_new_category'))
        if not scheme and not allow_new:
            return _fail(
                CODE_INVALID_PAYLOAD,
                '分类方案为空；请先选择分类，或开启「允许模型新建分类」',
            )

        provider = store.resolve_provider(str(payload.get('provider_id') or ''))
        api_key = store.resolve_provider_key(provider['id'])
        if not api_key:
            return _fail(
                CODE_SECRET_MISSING,
                f'provider「{provider["id"]}」未配置 API Key，请先在插件设置里填写',
                http_status=409,
                detail={'provider_id': provider['id'],
                        'secret_key': api_key_secret_name(provider['id'])},
            )

        model_name = str(payload.get('model') or provider.get('model') or '')
        profile = store.resolve_profile(str(payload.get('profile_id') or ''), model_name)

        apply_mode = str(payload.get('apply_mode') or config['apply_mode']).strip().lower()
        if apply_mode not in ('auto', 'review'):
            apply_mode = config['apply_mode']

        items = []
        skipped = []
        for card_id in card_ids:
            card = api.cards.read(card_id)
            if not isinstance(card, dict):
                skipped.append({'card_id': card_id, 'reason': 'card_unreadable'})
                continue
            items.append({
                'key': summary_bridge.resolve_key(api, card_id),
                'card_id': card_id,
                'name': str((hashing.card_data_block(card) or {}).get('name') or ''),
                'category': str(payload.get('current_category') or ''),
            })

        if not items:
            return _ok(run_id='', total=0, skipped=len(skipped), details=skipped,
                       message='没有可归类的卡片')

        job_payload = {
            'provider_id': provider['id'],
            'model': model_name,
            'profile_id': str(profile.get('id') or ''),
            'apply_mode': apply_mode,
            'dry_run': bool(payload.get('dry_run')),
            'force': bool(payload.get('force')),
            'scheme': scheme,
            'allow_new_category': allow_new,
            'confidence_min': payload.get('confidence_min'),
            'secret_ref': SECRET_REF_TEMPLATE.format(provider_id=provider['id']),
        }

        try:
            run_id = api.jobs.submit(JOB_CLASSIFY, items=items, payload=job_payload)
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if '已在运行' in message or 'already' in message.lower():
                return _fail('job_already_running', message, http_status=409)
            raise

        return _ok(
            run_id=run_id,
            total=len(items),
            skipped=len(skipped),
            details=skipped[:50],
            scheme_size=len(scheme),
            profile={'id': profile.get('id'), 'name': profile.get('name')},
            provider={'id': provider.get('id'), 'model': model_name},
            apply_mode=apply_mode,
        )

    # ---------------------------------------------------------- 结构化提议

    @api.http.route('/proposal/sample', ['POST'])
    @_handle_errors
    def preview_proposal_sample():
        """预览采样结果（不调 LLM、不写任何状态）。"""
        payload = _body()
        options = payload.get('options') or {}
        config = config_module.load_config(api)

        profile = store.get_profile('builtin-propose')
        if not isinstance(profile, dict):
            return _fail(CODE_INVALID_PAYLOAD, '找不到结构提议任务规格')

        bundle = jobs_module.build_proposal_messages(
            api, store, profile, config, options=options,
        )
        sample = bundle['sample']

        return _ok(
            sample={
                'sampled': sample['sampled'],
                'total': sample['total'],
                'scanned': bundle['scan']['scanned'],
                'capped': bundle['scan']['capped'],
                'tokens': sample['tokens'],
                'budget_tokens': sample['budget_tokens'],
                'category_coverage': sample['category_coverage'],
            },
            items=[
                {
                    'card_id': item.get('card_id'),
                    'name': (item.get('brief') or {}).get('name'),
                    'tags': (item.get('brief') or {}).get('tags') or [],
                    'category': item.get('category') or '',
                }
                for item in sample['items']
            ],
            existing={
                'categories': bundle['categories'],
                'tag_count': len(bundle['tags']),
            },
            # 采样清单本身也给出来，方便用户核对 AI 到底看到了什么
            sample_text=proposal_core.format_sample(sample['items']),
        )

    @api.http.route('/jobs/propose', ['POST'])
    @_handle_errors
    def submit_propose():
        """提交结构化提议作业（一次 LLM 调用，产出分类体系与标签池）。"""
        payload = _body()

        provider = store.resolve_provider(str(payload.get('provider_id') or ''))
        api_key = store.resolve_provider_key(provider['id'])
        if not api_key:
            return _fail(
                CODE_SECRET_MISSING,
                f'provider「{provider["id"]}」未配置 API Key，请先在插件设置里填写',
                http_status=409,
                detail={'provider_id': provider['id'],
                        'secret_key': api_key_secret_name(provider['id'])},
            )

        model_name = str(payload.get('model') or provider.get('model') or '')
        options = proposal_core.normalize_options(payload.get('options'))

        job_payload = {
            'provider_id': provider['id'],
            'model': model_name,
            'profile_id': str(payload.get('profile_id') or 'builtin-propose'),
            'options': options,
            'secret_ref': SECRET_REF_TEMPLATE.format(provider_id=provider['id']),
        }

        try:
            run_id = api.jobs.submit(
                JOB_PROPOSE,
                items=[{'key': 'proposal', 'payload': {}}],
                payload=job_payload,
            )
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if '已在运行' in message or 'already' in message.lower():
                return _fail('job_already_running', message, http_status=409)
            raise

        return _ok(
            run_id=run_id,
            total=1,
            provider={'id': provider.get('id'), 'model': model_name},
            options=options,
        )

    @api.http.route('/jobs/agent', ['POST'])
    @_handle_errors
    def submit_agent():
        """提交 Agent 作业（多轮：看目录 → 分批读 → 提交提议）。"""
        payload = _body()

        provider = store.resolve_provider(str(payload.get('provider_id') or ''))
        api_key = store.resolve_provider_key(provider['id'])
        if not api_key:
            return _fail(
                CODE_SECRET_MISSING,
                f'provider「{provider["id"]}」未配置 API Key，请先在插件设置里填写',
                http_status=409,
                detail={'provider_id': provider['id'],
                        'secret_key': api_key_secret_name(provider['id'])},
            )

        model_name = str(payload.get('model') or provider.get('model') or '')
        budget = agent_core.normalize_budget(payload.get('budget'))

        job_payload = {
            'provider_id': provider['id'],
            'model': model_name,
            'profile_id': str(payload.get('profile_id') or 'builtin-agent'),
            'budget': budget,
            'secret_ref': SECRET_REF_TEMPLATE.format(provider_id=provider['id']),
        }

        try:
            run_id = api.jobs.submit(
                JOB_AGENT,
                items=[{'key': 'agent', 'payload': {}}],
                payload=job_payload,
            )
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if '已在运行' in message or 'already' in message.lower():
                return _fail('job_already_running', message, http_status=409)
            raise

        return _ok(
            run_id=run_id,
            total=1,
            provider={'id': provider.get('id'), 'model': model_name},
            budget=budget,
        )

    # ---------------------------------------------------------- 审批与结果

    @api.http.route('/jobs/<run_id>', ['GET'])
    @_handle_errors
    def get_job(run_id):
        job = api.jobs.get(run_id)
        if job is None:
            return _fail(CODE_JOB_NOT_FOUND, f'作业不存在: {run_id}', http_status=404)

        items = _load_job_items(api, run_id)
        return _ok(job=_job_summary(job), items=items, items_total=len(items))

    @api.http.route('/jobs/<run_id>/apply', ['POST'])
    @_handle_errors
    def apply_job(run_id):
        """把已审批（`status=done`）的条目写回。"""
        job = api.jobs.get(run_id)
        if job is None:
            return _fail(CODE_JOB_NOT_FOUND, f'作业不存在: {run_id}', http_status=404)

        job_type = str(job.get('job_type') or '')
        if job_type == JOB_WI_REWRITE:
            return _apply_wi_rewrite(api, store, run_id, job, _body())
        if job_type == JOB_CLASSIFY:
            return _apply_classify(api, store, run_id, job, _body())
        if job_type == JOB_PROPOSE:
            return _apply_propose(api, store, run_id, job, _body())
        if job_type == JOB_AGENT:
            # Agent 的产出与 propose 完全同构 → 复用同一套落地逻辑
            return _apply_propose(api, store, run_id, job, _body())
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

            card = api.cards.read(card_id)
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
                    api, store, card_id, summary_text, ui_key=ui_key)
                if not bridge_result.get('ok'):
                    details.append({'card_id': card_id,
                                    'warning': f'概览降级写入: {bridge_result.get("message", "")}'})

            if tags and apply_tags_enabled:
                tag_library = read_tag_library(api)
                accepted, _suggested, _blocked = split_tags(tags, known_tag_set(tag_library))
                if accepted:
                    tag_result = api.cards.apply_tags([card_id], add=accepted, replace=False)
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

    @api.http.route('/jobs/<run_id>/cancel', ['POST'])
    @_handle_errors
    def cancel_job(run_id):
        if api.jobs.get(run_id) is None:
            return _fail(CODE_JOB_NOT_FOUND, f'作业不存在: {run_id}', http_status=404)
        return _ok(cancelled=bool(api.jobs.cancel(run_id)))

    @api.http.route('/jobs/<run_id>/retry', ['POST'])
    @_handle_errors
    def retry_job(run_id):
        if api.jobs.get(run_id) is None:
            return _fail(CODE_JOB_NOT_FOUND, f'作业不存在: {run_id}', http_status=404)
        payload = _body()
        new_run = api.jobs.retry(run_id, only_failed=bool(payload.get('only_failed', True)))
        return _ok(run_id=new_run)

    # ---------------------------------------------------------- 建议队列与用量

    @api.http.route('/suggestions', ['GET'])
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

    @api.http.route('/suggestions/clear', ['POST'])
    @_handle_errors
    def clear_suggestions():
        payload = _body()
        names = payload.get('names')
        return _ok(**store.clear_tag_suggestions(names if isinstance(names, list) else None))

    @api.http.route('/usage', ['GET'])
    @_handle_errors
    def get_usage():
        return _ok(
            ledger=store.usage_ledger(),
            recent=store.recent_usage(RECENT_USAGE_LIMIT),
        )

    @api.http.route('/outbound/ack', ['POST'])
    @_handle_errors
    def ack_outbound():
        store.acknowledge_outbound()
        return _ok(acknowledged=True)

    @api.http.route('/prompt/validate', ['POST'])
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


def _apply_wi_rewrite(api, store, run_id: str, job, payload) -> dict:
    """写回世界书改写作业中已审批的条目。

    与打标作业的差别：世界书是**整本写**，所以要把所有通过的条目
    合并进同一份 `book`，只写一次（`write` 有乐观锁，重复写会撞版本）。
    """
    items = _load_job_items(api, run_id)

    applied = 0
    skipped = 0
    failed = 0
    details = []

    # 按来源聚合：同一世界书只写一次
    buckets = {}

    for item in items:
        if str(item.get('status') or '') != 'done':
            continue

        result = item.get('result') if isinstance(item.get('result'), dict) else {}
        entry_uid = str(result.get('entry_uid') or item.get('key') or '')
        candidate = str(result.get('candidate') or '')
        if not entry_uid or not candidate:
            skipped += 1
            continue

        if result.get('applied') is True and not payload.get('force'):
            # 作业已在 auto 模式下写过 → 幂等跳过
            skipped += 1
            continue

        job_payload = job.get('payload') if isinstance(job.get('payload'), dict) else {}
        source = jobs_module.resolve_wi_source(
            str(result.get('card_id') or item.get('key') or ''),
            job_payload.get('worldbook_source'),
        )
        key = f"{source.get('source_type')}|{source.get('card_id')}|{source.get('file_path')}"
        buckets.setdefault(key, {'source': source, 'entries': []})['entries'].append(
            (entry_uid, candidate),
        )

    for bucket in buckets.values():
        source = bucket['source']
        book = api.worldbook.read(source)
        if not isinstance(book, dict):
            failed += len(bucket['entries'])
            details.append({'error': 'worldbook_unreadable'})
            continue

        entries = book.get('entries')
        if isinstance(entries, dict):
            entries = list(entries.values())
        if not isinstance(entries, list):
            failed += len(bucket['entries'])
            details.append({'error': 'worldbook_structure'})
            continue

        wanted = dict(bucket['entries'])
        updated_entries = []
        hit = 0
        for entry in entries:
            if not isinstance(entry, dict):
                updated_entries.append(entry)
                continue
            uid = str(entry.get('uid') or entry.get('id') or '')
            if uid in wanted:
                patched = dict(entry)
                patched['content'] = wanted[uid]
                updated_entries.append(patched)
                hit += 1
            else:
                updated_entries.append(entry)

        if source.get('source_type') == 'embedded' and source.get('card_id'):
            try:
                api.backups.snapshot_card(source['card_id'], remark='ai-wi-rewrite-apply')
            except Exception:  # noqa: BLE001
                logger.warning('世界书应用前快照失败', exc_info=True)

        updated = dict(book)
        updated['entries'] = updated_entries

        outcome = api.worldbook.write(
            source, updated,
            expected_revision=str(book.get('source_revision') or ''),
        )
        if outcome.get('success'):
            applied += hit
            failed += len(wanted) - hit
            if len(wanted) > hit:
                details.append({'error': 'entry_missing', 'count': len(wanted) - hit})
        else:
            failed += len(wanted)
            details.append({
                'error': str(outcome.get('error') or 'write_failed'),
                'message': str(outcome.get('msg') or ''),
            })

    if applied:
        try:
            store.clear_pending_review(run_id)
        except Exception:  # noqa: BLE001
            logger.debug('清理待审批登记失败', exc_info=True)

    return _ok(applied=applied, skipped=skipped, failed=failed, details=details)


def _apply_classify(api, store, run_id: str, job, payload) -> dict:
    """把已审批的归类结果落地为实际的目录移动。

    按目标分类**批量**移动（主程序 `move` 支持批量，比逐卡快得多）。
    """
    items = _load_job_items(api, run_id)
    force = bool(payload.get('force'))

    # 目标分类 → 卡片 id 列表
    plan = {}
    skipped = 0
    details = []

    for item in items:
        if str(item.get('status') or '') != 'done':
            continue

        result = item.get('result') if isinstance(item.get('result'), dict) else {}
        card_id = str(result.get('card_id') or item.get('key') or '')
        category = str(result.get('category') or '')

        if not card_id:
            skipped += 1
            continue

        if result.get('applied') is True and not force:
            # auto 模式已经移动过 → 幂等跳过
            skipped += 1
            continue

        if result.get('issues'):
            skipped += 1
            details.append({'card_id': card_id, 'error': 'invalid_category',
                            'issues': result['issues']})
            continue

        plan.setdefault(category, []).append(card_id)

    applied = 0
    failed = 0

    for category, card_ids in plan.items():
        # 目标分类要存在；根目录（空串）不需要创建
        if category:
            try:
                api.cards.create_category(category)
            except Exception:  # noqa: BLE001 - 已存在会返回 False，不算错误
                logger.debug('创建分类失败（可能已存在）: %s', category, exc_info=True)

        try:
            outcome = api.cards.move(card_ids, category)
        except Exception as exc:  # noqa: BLE001
            logger.exception('移动卡片失败: %s', category)
            failed += len(card_ids)
            details.append({'category': category, 'error': 'move_failed',
                            'message': str(exc)})
            continue

        applied += int(outcome.get('count') or 0)
        for failure in (outcome.get('failed') or []):
            failed += 1
            details.append({
                'card_id': str(failure.get('old_id') or ''),
                'error': 'move_failed',
                'message': str(failure.get('msg') or ''),
            })

    if applied:
        try:
            store.clear_pending_review(run_id)
        except Exception:  # noqa: BLE001
            logger.debug('清理待审批登记失败', exc_info=True)

    return _ok(applied=applied, skipped=skipped, failed=failed,
               categories=len(plan), details=details)


def _apply_propose(api, store, run_id: str, job, payload) -> dict:
    """落地结构化提议：**只创建结构**（目录 + 标签），不动任何卡片。

    刻意不执行 `tag_merges`：它要改卡片标签（破坏性），必须由用户单独确认。
    这里只把它原样回传给前端展示。
    """
    items = _load_job_items(api, run_id)
    if not items:
        return _fail(CODE_JOB_NOT_FOUND, f'作业没有结果: {run_id}', http_status=404)

    result = items[0].get('result') if isinstance(items[0].get('result'), dict) else {}
    plan = result.get('plan') if isinstance(result.get('plan'), dict) else {}

    create_categories = [
        str(item) for item in (plan.get('create_categories') or []) if str(item or '').strip()
    ]
    create_tags = [
        str(item) for item in (plan.get('create_tags') or []) if str(item or '').strip()
    ]

    # 用户可以在审批时裁剪（只创建其中一部分）
    selected_categories = payload.get('categories')
    if isinstance(selected_categories, list):
        wanted = {str(item) for item in selected_categories}
        create_categories = [item for item in create_categories if item in wanted]

    selected_tags = payload.get('tags')
    if isinstance(selected_tags, list):
        wanted = {str(item) for item in selected_tags}
        create_tags = [item for item in create_tags if item in wanted]

    created_categories = []
    failed_categories = []
    for name in create_categories:
        try:
            # 主程序内部已做白名单校验；这里再判一次返回值
            ok = bool(api.cards.create_category(name))
        except Exception:  # noqa: BLE001
            logger.warning('创建分类失败: %s', name, exc_info=True)
            ok = False
        (created_categories if ok else failed_categories).append(name)

    created_tags = []
    existing_tags = []
    failed_tags = []
    if create_tags:
        try:
            outcome = api.tags.create(create_tags)
            created_tags = [str(item) for item in (outcome.get('created') or [])]
            existing_tags = [str(item) for item in (outcome.get('existing') or [])]
            failed_tags = [str(item) for item in (outcome.get('failed') or [])]
        except Exception as exc:  # noqa: BLE001
            logger.warning('创建标签失败', exc_info=True)
            failed_tags = list(create_tags)
            detail = str(exc)
        else:
            detail = ''

    if created_categories or created_tags:
        try:
            store.clear_pending_review(run_id)
        except Exception:  # noqa: BLE001
            logger.debug('清理待审批登记失败', exc_info=True)

    return _ok(
        created_categories=created_categories,
        failed_categories=failed_categories,
        created_tags=created_tags,
        existing_tags=existing_tags,
        failed_tags=failed_tags,
        # 合并建议只回传、不执行 —— 它会改卡片标签
        tag_merges=plan.get('tag_merges') or [],
        requires_manual_review=bool(plan.get('tag_merges')),
    )


def _injected_transport():
    """测试注入点：通过模块属性替换。"""
    return getattr(llm, '_TEST_TRANSPORT', None)


def _injected_get():
    """`GET /models` 的测试注入点（与 `_injected_transport` 分开）。"""
    return getattr(llm, '_TEST_GET', None)


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



