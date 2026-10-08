"""插件 HTTP 路由端到端：用 Flask test client 装配 `register_routes` 收集到的视图。"""

import json

import pytest

flask = pytest.importorskip('flask', reason='路由测试需要 flask（主程序运行环境必备）')

from conftest import load_backend_submodule  # noqa: E402
from fake_api import FakeApi  # noqa: E402

routes_module = load_backend_submodule('routes')
store_module = load_backend_submodule('store')
errors = load_backend_submodule('core.errors')

CARD = {
    'name': 'Alice',
    'description': '北境骑士。',
    'first_mes': '你好。',
    'character_book': {'entries': [{'comment': '世界观', 'content': '常年积雪。'}]},
}


def build_app(*, cards=None, secrets=None, tag_library=None, db_rows=None, tmp_path=None):
    """装配一个只含插件路由的 Flask 测试客户端。"""
    api = FakeApi(
        data_dir=str(tmp_path / 'data'),
        cards=cards if cards is not None else {'Alice.png': dict(CARD)},
        secrets=secrets if secrets is not None else {'api_key': 'sk-test'},
        tag_library=tag_library or {'tags': ['奇幻'], 'taxonomy': {}, 'prefs': {}},
        db_rows=db_rows,
    )
    store = store_module.PluginStore(api)
    routes_module.register_routes(api, store)

    app = flask.Flask(__name__)
    app.config['TESTING'] = True
    for rule, methods, func in api.routes:
        app.add_url_rule(rule, endpoint=func.__name__, view_func=func, methods=list(methods))

    return app.test_client(), api, store


@pytest.fixture
def env(tmp_path):
    return build_app(tmp_path=tmp_path)


# ------------------------------------------------------------------ 状态


def test_status_reports_configuration(env):
    client, _api, _store = env
    response = client.get('/status')
    payload = response.get_json()

    assert response.status_code == 200
    assert payload['success'] is True
    assert payload['profiles']['count'] == 5
    assert payload['providers']['count'] >= 1
    assert payload['config']['apply_mode'] == 'review'


def test_health_reports_bridge_and_provider(env):
    client, _api, _store = env
    payload = client.get('/health').get_json()

    assert payload['success'] is True
    checks = payload['checks']
    assert 'summary_bridge' in checks
    assert checks['provider']['ok'] is True
    assert checks['api_key']['ok'] is True


def test_health_reports_missing_api_key(tmp_path):
    client, _api, _store = build_app(secrets={}, tmp_path=tmp_path)
    payload = client.get('/health').get_json()
    assert payload['checks']['api_key']['ok'] is False


def test_cards_lists_from_database(tmp_path):
    rows = [{'id': 'Alice.png', 'char_name': 'Alice', 'tags': '["奇幻"]'}]
    client, _api, _store = build_app(db_rows=rows, tmp_path=tmp_path)

    payload = client.get('/cards').get_json()
    assert payload['items'] == [{'card_id': 'Alice.png', 'name': 'Alice', 'tags': ['奇幻']}]


def test_cards_filters_by_keyword(tmp_path):
    rows = [
        {'id': 'Alice.png', 'char_name': 'Alice', 'tags': '[]'},
        {'id': 'Bob.png', 'char_name': 'Bob', 'tags': '[]'},
    ]
    client, _api, _store = build_app(db_rows=rows, tmp_path=tmp_path)
    payload = client.get('/cards?q=bob').get_json()
    assert [item['card_id'] for item in payload['items']] == ['Bob.png']


# ------------------------------------------------------------------ 档案


def test_profiles_list_and_detail(env):
    client, _api, _store = env
    listing = client.get('/profiles').get_json()
    assert listing['success'] is True
    assert len(listing['items']) == 5

    detail = client.get('/profiles/builtin-gpt').get_json()
    assert detail['profile']['id'] == 'builtin-gpt'
    assert detail['profile']['system']


def test_profiles_detail_404(env):
    client, _api, _store = env
    response = client.get('/profiles/nope')
    assert response.status_code == 404
    assert response.get_json()['error'] == 'profile_not_found'


def test_profiles_save_and_validate(env):
    client, _api, _store = env
    profile = client.get('/profiles/builtin-default').get_json()['profile']
    profile['id'] = 'my-profile'
    profile['name'] = '我的'
    profile['builtin'] = False

    saved = client.post('/profiles', json={'profile': profile}).get_json()
    assert saved['success'] is True
    assert saved['profile']['version'] == 1

    profile['user_template'] = '{{ card.nickname }}'
    failed = client.post('/profiles', json={'profile': profile})
    assert failed.status_code == 400
    assert failed.get_json()['error'] == 'profile_invalid'


def test_profiles_delete_and_duplicate(env):
    client, _api, _store = env
    duplicated = client.post('/profiles/builtin-gpt/duplicate', json={}).get_json()
    assert duplicated['profile']['id'] == 'builtin-gpt-copy'

    deleted = client.delete('/profiles/builtin-gpt-copy').get_json()
    assert deleted['deleted'] is True


def test_profiles_export_and_import(env):
    client, _api, _store = env
    exported = client.get('/profiles/export').get_json()
    assert len(exported['profiles']) == 5

    imported = client.post('/profiles/import', json={'payload': exported}).get_json()
    assert imported['success'] is True
    assert len(imported['imported']) == 5


def test_profiles_reset(env):
    client, _api, _store = env
    profile = client.get('/profiles/builtin-default').get_json()['profile']
    profile['name'] = '改过'
    client.post('/profiles', json={'profile': profile})

    result = client.post('/profiles/reset').get_json()
    assert 'builtin-default' in result['removed']


def test_prompt_validate_reports_unknown_variables(env):
    client, _api, _store = env
    payload = client.post('/prompt/validate', json={
        'templates': ['{{ card.name }}', '{{ card.nickname }}'],
    }).get_json()

    assert payload['unknown'] == ['card.nickname']
    assert 'card.name' in payload['variables']
    assert 'card.name' in payload['known']


# ------------------------------------------------------------------ provider


def test_providers_list_masks_secrets(env):
    client, api, _store = env
    api.set_secret('provider__openai__api_key', 'sk-super-secret')

    payload = client.get('/providers').get_json()
    dumped = json.dumps(payload, ensure_ascii=False)

    assert 'sk-super-secret' not in dumped
    assert payload['items'][0]['has_api_key'] is True


def test_provider_save_and_delete(env):
    client, _api, _store = env
    created = client.post('/providers', json={'provider': {
        'id': 'deepseek', 'name': 'DS', 'base_url': 'https://api.deepseek.com/v1',
    }}).get_json()
    assert created['provider']['id'] == 'deepseek'

    deleted = client.delete('/providers/deepseek').get_json()
    assert deleted['remaining'] == 1


def test_provider_save_rejects_bad_payload(env):
    client, _api, _store = env
    response = client.post('/providers', json={'provider': {'id': 'x', 'base_url': 'nope'}})
    assert response.status_code == 400
    assert response.get_json()['error'] == 'invalid_payload'


def test_provider_secrets_endpoint(env):
    client, _api, _store = env
    payload = client.post('/providers/openai/secrets', json={'api_key': 'sk-new'}).get_json()
    assert payload['result']['api_key'] is True

    cleared = client.delete('/providers/openai/secrets').get_json()
    assert cleared['cleared'] is True


def test_provider_secrets_404_for_unknown(env):
    client, _api, _store = env
    response = client.post('/providers/nope/secrets', json={'api_key': 'x'})
    assert response.status_code == 404


def test_provider_test_reports_error_without_key(env):
    client, api, _store = env
    api.set_secret('provider__openai__api_key', None)
    api.set_secret('api_key', None)

    response = client.post('/providers/openai/test', json={})
    assert response.status_code == 409
    assert response.get_json()['error'] == errors.CODE_SECRET_MISSING


def test_provider_test_uses_injected_transport(env, monkeypatch):
    client, _api, _store = env
    probe_calls = []

    def fake_probe(provider, api_key, *, model='', transport=None, sleep=None):
        probe_calls.append({'provider': provider, 'model': model})
        return {'ok': True, 'latency_ms': 12, 'model': model or provider['model'],
                'preview': 'pong', 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}

    monkeypatch.setattr(routes_module.llm, 'probe', fake_probe)
    payload = client.post('/providers/openai/test', json={'model': 'gpt-4o-mini'}).get_json()

    assert payload['ok'] is True
    assert payload['latency_ms'] == 12
    assert probe_calls[0]['model'] == 'gpt-4o-mini'


# ------------------------------------------------------------------ 预览


def test_preview_returns_messages_and_tokens(env):
    client, _api, _store = env
    payload = client.post('/prompt/preview', json={'card_id': 'Alice.png'}).get_json()

    assert payload['success'] is True
    assert payload['profile']['id'] == 'builtin-default'
    assert payload['messages'][0]['role'] == 'system'
    assert payload['tokens']['total'] > 0
    assert payload['variables']['card.name'] == 'Alice'
    assert payload['worldbook']['count'] == 1


def test_preview_missing_card_returns_404(env):
    client, _api, _store = env
    response = client.post('/prompt/preview', json={'card_id': 'Nope.png'})
    assert response.status_code == 404


def test_preview_requires_card_id(env):
    client, _api, _store = env
    response = client.post('/prompt/preview', json={})
    assert response.status_code == 400


# ------------------------------------------------------------------ 作业提交


def test_submit_analyze_without_api_key_is_rejected(tmp_path):
    client, _api, _store = build_app(secrets={}, tmp_path=tmp_path)
    response = client.post('/jobs/analyze', json={'card_ids': ['Alice.png']})

    assert response.status_code == 409
    payload = response.get_json()
    assert payload['error'] == errors.CODE_SECRET_MISSING
    assert payload['detail']['secret_key'] == 'provider__openai__api_key'


def test_submit_analyze_creates_job(env):
    client, api, _store = env
    payload = client.post('/jobs/analyze', json={'card_ids': ['Alice.png']}).get_json()

    assert payload['run_id'] == 'run-test-0001'
    assert payload['total'] == 1
    assert api.submitted['job_type'] == 'ai.analyze'
    assert api.submitted['items'][0]['card_id'] == 'Alice.png'
    assert api.submitted['payload']['secret_ref'].startswith('provider__')
    assert 'sk-test' not in json.dumps(api.submitted['payload'])


def test_submit_analyze_validates_payload(env):
    client, _api, _store = env
    assert client.post('/jobs/analyze', json={'card_ids': []}).status_code == 400
    assert client.post('/jobs/analyze', json={'card_ids': ['Alice.png'], 'mode': 'x'}).status_code == 400


def test_submit_analyze_skips_unchanged_cards(env):
    client, _api, store = env
    card = dict(CARD)
    from conftest import load_backend_submodule as _load  # noqa: PLC0415

    hashing = _load('core.hashing')
    store.update_card_meta('Alice.png', {
        'content_hash': hashing.card_content_hash(card),
        'profile_id': 'builtin-default',
        'model': '',
    })

    payload = client.post('/jobs/analyze', json={'card_ids': ['Alice.png']}).get_json()
    assert payload['run_id'] == ''
    assert payload['skipped'] == 1
    assert payload['details'][0]['reason'] == 'unchanged'


def test_submit_analyze_force_bypasses_idempotency(env):
    client, _api, store = env
    store.update_card_meta('Alice.png', {'content_hash': 'anything'})
    payload = client.post('/jobs/analyze', json={'card_ids': ['Alice.png'], 'force': True}).get_json()
    assert payload['total'] == 1


def test_submit_analyze_reports_unreadable_card(env):
    client, _api, _store = env
    payload = client.post('/jobs/analyze', json={'card_ids': ['Missing.png']}).get_json()
    assert payload['total'] == 0
    assert payload['details'][0]['reason'] == 'card_unreadable'


def test_submit_analyze_estimates_usage(env):
    client, _api, _store = env
    payload = client.post('/jobs/analyze', json={'card_ids': ['Alice.png']}).get_json()
    assert payload['estimate']['prompt_tokens'] > 0
    assert payload['estimate']['cost']['priced'] is False


# ------------------------------------------------------------------ 作业读取与应用


hashing = load_backend_submodule('core.hashing')


def job_item_row(*, status='done', item_key='Alice.png', result_overrides=None, **row_overrides):
    """构造 `plugin_job_items` 的一行。

    `status` / `item_key` 等是**行字段**，`result_overrides` 才是插件结果结构里的字段。
    """
    result = {
        'card_id': 'Alice.png',
        'ui_key': 'Alice.png',
        'summary': 'AI 生成的概览',
        'tags': [{'name': '奇幻', 'confidence': 0.9}],
        'content_hash': hashing.card_content_hash(CARD),
    }
    result.update(result_overrides or {})

    row = {
        'item_key': item_key,
        'status': status,
        'seq': 0,
        'payload_json': '{}',
        'result_json': json.dumps(result, ensure_ascii=False),
        'error': '',
        'updated_at': 0,
    }
    row.update(row_overrides)
    return row


def test_get_job_returns_items(tmp_path):
    client, api, _store = build_app(db_rows=[job_item_row()], tmp_path=tmp_path)
    api.job_detail = {'run_id': 'run-1', 'job_type': 'ai.analyze', 'status': 'completed',
                      'total': 1, 'completed': 1, 'failed': 0, 'skipped': 0,
                      'summary': {'prompt_tokens': 10}}

    payload = client.get('/jobs/run-1').get_json()
    assert payload['job']['run_id'] == 'run-1'
    assert payload['items'][0]['result']['summary'] == 'AI 生成的概览'


def test_get_job_404(tmp_path):
    client, _api, _store = build_app(tmp_path=tmp_path)
    response = client.get('/jobs/unknown')
    assert response.status_code == 404
    assert response.get_json()['error'] == 'job_not_found'


def test_apply_writes_summary_and_tags(tmp_path, monkeypatch):
    client, api, store = build_app(db_rows=[job_item_row()], tmp_path=tmp_path)
    api.job_detail = {'run_id': 'run-1', 'job_type': 'ai.analyze', 'status': 'completed'}

    written = {}
    monkeypatch.setattr(routes_module.summary_bridge, 'write_summary',
                        lambda store_obj, card_id, summary, ui_key='': written.update(
                            {card_id: summary}) or {'ok': True, 'origin': 'ui_data',
                                                    'ui_key': ui_key, 'message': ''})

    payload = client.post('/jobs/run-1/apply', json={}).get_json()

    assert payload['applied'] == 1
    assert written['Alice.png'] == 'AI 生成的概览'
    assert api.applied_tags[0]['add'] == ['奇幻']

    meta = store.get_card_meta('Alice.png')
    assert meta['applied_runs'] == ['run-1']
    assert meta['applied_at'] > 0


def test_apply_is_idempotent(tmp_path, monkeypatch):
    client, api, _store = build_app(db_rows=[job_item_row()], tmp_path=tmp_path)
    api.job_detail = {'run_id': 'run-1', 'job_type': 'ai.analyze', 'status': 'completed'}
    monkeypatch.setattr(routes_module.summary_bridge, 'write_summary',
                        lambda *a, **k: {'ok': True, 'origin': 'ui_data', 'ui_key': 'x', 'message': ''})

    client.post('/jobs/run-1/apply', json={})
    second = client.post('/jobs/run-1/apply', json={}).get_json()

    assert second['applied'] == 0
    assert second['skipped'] == 1


def test_apply_skips_stale_items(tmp_path, monkeypatch):
    client, api, _store = build_app(
        db_rows=[job_item_row(result_overrides={'content_hash': 'sha256:outdated'})],
        tmp_path=tmp_path,
    )
    api.job_detail = {'run_id': 'run-1', 'job_type': 'ai.analyze', 'status': 'completed'}
    monkeypatch.setattr(routes_module.summary_bridge, 'write_summary',
                        lambda *a, **k: {'ok': True, 'origin': 'ui_data', 'ui_key': 'x', 'message': ''})

    payload = client.post('/jobs/run-1/apply', json={}).get_json()

    assert payload['applied'] == 0
    assert payload['stale'] == 1
    assert payload['details'][0]['error'] == 'apply_stale'


def test_apply_force_overrides_stale_check(tmp_path, monkeypatch):
    client, api, _store = build_app(
        db_rows=[job_item_row(result_overrides={'content_hash': 'sha256:outdated'})],
        tmp_path=tmp_path,
    )
    api.job_detail = {'run_id': 'run-1', 'job_type': 'ai.analyze', 'status': 'completed'}
    monkeypatch.setattr(routes_module.summary_bridge, 'write_summary',
                        lambda *a, **k: {'ok': True, 'origin': 'ui_data', 'ui_key': 'x', 'message': ''})

    payload = client.post('/jobs/run-1/apply', json={'force': True}).get_json()
    assert payload['applied'] == 1


def test_apply_ignores_pending_items(tmp_path):
    client, api, _store = build_app(db_rows=[job_item_row(status='awaiting_review')],
                                    tmp_path=tmp_path)
    api.job_detail = {'run_id': 'run-1', 'job_type': 'ai.analyze', 'status': 'running'}

    payload = client.post('/jobs/run-1/apply', json={}).get_json()
    assert payload['applied'] == 0
    assert payload['total'] == 1


def test_apply_rejects_other_job_types(tmp_path):
    client, api, _store = build_app(db_rows=[], tmp_path=tmp_path)
    api.job_detail = {'run_id': 'run-1', 'job_type': 'demo.count', 'status': 'completed'}

    response = client.post('/jobs/run-1/apply', json={})
    assert response.status_code == 400


def test_cancel_and_retry_endpoints(tmp_path):
    client, api, _store = build_app(tmp_path=tmp_path)
    api.job_detail = {'run_id': 'run-1', 'job_type': 'ai.analyze', 'status': 'running'}

    assert client.post('/jobs/run-1/cancel').get_json()['cancelled'] is True
    assert api.cancelled == ['run-1']

    assert client.post('/jobs/run-1/retry', json={'only_failed': True}).get_json()['run_id'] == 'run-1'


# ------------------------------------------------------------------ 建议与用量


def test_suggestions_and_clear(env):
    client, _api, store = env
    store.record_tag_suggestions([{'name': '赛博朋克', 'reason': '新词'}], card_key='Alice.png')

    payload = client.get('/suggestions').get_json()
    assert payload['items'][0]['name'] == '赛博朋克'

    cleared = client.post('/suggestions/clear', json={'names': ['赛博朋克']}).get_json()
    assert cleared['removed'] == ['赛博朋克']


def test_usage_endpoint(env):
    client, _api, store = env
    store.record_usage({'prompt_tokens': 10, 'completion_tokens': 5, 'model': 'm'})

    payload = client.get('/usage').get_json()
    assert payload['ledger']['total_prompt_tokens'] == 10
    assert payload['recent'][-1]['model'] == 'm'


def test_outbound_ack_endpoint(env):
    client, _api, store = env
    assert client.post('/outbound/ack').get_json()['acknowledged'] is True
    assert store.outbound_acknowledged() is True
