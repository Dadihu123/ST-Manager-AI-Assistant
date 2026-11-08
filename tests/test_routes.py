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
    assert payload['profiles']['count'] == 9
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
    # `id` / `category` / `token_count` 是给**前端作用域解析**用的：
    # `scope.js` 用它们判断「这个分类下有几张卡」，不再自己猜字段名。
    assert payload['items'] == [{
        'card_id': 'Alice.png',
        'id': 'Alice.png',
        'name': 'Alice',
        'category': '',
        'token_count': 0,
        'tags': ['奇幻'],
    }]


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
    assert len(listing['items']) == 9

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
    assert len(exported['profiles']) == 9

    imported = client.post('/profiles/import', json={'payload': exported}).get_json()
    assert imported['success'] is True
    assert len(imported['imported']) == 9


def test_profiles_export_single_profile(env):
    """回归：`/profiles/export?profile_id=` 必须只导出指定的那一个档案。"""
    client, _api, _store = env
    exported = client.get('/profiles/export?profile_id=builtin-gpt').get_json()

    assert exported['success'] is True
    assert len(exported['profiles']) == 1
    assert exported['profiles'][0]['id'] == 'builtin-gpt'


def test_profiles_export_unknown_profile_404(env):
    client, _api, _store = env
    response = client.get('/profiles/export?profile_id=does-not-exist')
    assert response.status_code == 404


def test_profiles_active_roundtrip(env):
    """回归：切换「当前档案」后 `/profiles` 的 active 字段要跟着变。"""
    client, _api, _store = env
    assert client.get('/profiles').get_json()['active'] == ''

    saved = client.post('/profiles/active', json={'profile_id': 'builtin-gpt'}).get_json()
    assert saved['active'] == 'builtin-gpt'
    assert client.get('/profiles').get_json()['active'] == 'builtin-gpt'


def test_profiles_active_rejects_unknown_profile(env):
    client, _api, _store = env
    response = client.post('/profiles/active', json={'profile_id': 'nope'})
    assert response.status_code == 404


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
    api.secrets.set('provider__openai__api_key', 'sk-super-secret')

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
    api.secrets.set('provider__openai__api_key', None)
    api.secrets.set('api_key', None)

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


# ------------------------------------------------------------------ 模型列表


def test_models_route_returns_list_from_injected_get(env, monkeypatch):
    client, _api, store = env
    captured = []

    def fake_list_models(provider, api_key, *, extra_headers=None, transport=None):
        captured.append({'provider': provider, 'api_key': api_key})
        return {'ok': True, 'models': ['gpt-4o', 'gpt-4o-mini'],
                'protocol': 'openai_chat', 'message': ''}

    monkeypatch.setattr(routes_module.llm, 'list_models', fake_list_models)
    payload = client.post('/providers/openai/models', json={}).get_json()

    assert payload['success'] is True
    assert payload['ok'] is True
    assert payload['models'] == ['gpt-4o', 'gpt-4o-mini']
    assert captured[0]['api_key'], '应解析出 provider 的密钥'


def test_models_route_never_blocks_on_failure(env, monkeypatch):
    """拉取失败必须返回 200 + ok=False，让 UI 退回手填模型名。"""
    client, _api, _store = env
    monkeypatch.setattr(
        routes_module.llm, 'list_models',
        lambda *a, **k: {'ok': False, 'models': [], 'protocol': 'openai_chat',
                         'message': 'HTTP 404'},
    )

    response = client.post('/providers/openai/models', json={})
    payload = response.get_json()

    assert response.status_code == 200
    assert payload['success'] is True
    assert payload['ok'] is False
    assert payload['models'] == []
    assert payload['message'] == 'HTTP 404'


def test_models_route_reports_missing_key_without_4xx(env):
    client, api, _store = env
    api.secrets.set('provider__openai__api_key', None)
    api.secrets.set('api_key', None)

    response = client.post('/providers/openai/models', json={})
    payload = response.get_json()

    assert response.status_code == 200
    assert payload['ok'] is False
    assert 'API Key' in payload['message']


def test_models_route_unknown_provider_falls_back_to_draft(env, monkeypatch):
    """未保存的 provider 不再 404：请求体带全地址 + Key 时照样能拉。"""
    client, _api, _store = env
    captured = []

    def fake_list_models(provider, api_key, *, extra_headers=None, transport=None):
        captured.append({'provider': provider, 'api_key': api_key})
        return {'ok': True, 'models': ['m-1'], 'protocol': 'openai_chat', 'message': ''}

    monkeypatch.setattr(routes_module.llm, 'list_models', fake_list_models)
    response = client.post('/providers/nope/models', json={
        'base_url': 'https://api.example.com/v1', 'api_key': 'sk-draft',
    })
    payload = response.get_json()

    assert response.status_code == 200
    assert payload['success'] is True
    assert payload['ok'] is True
    assert captured[0]['api_key'] == 'sk-draft'
    assert captured[0]['provider']['base_url'] == 'https://api.example.com/v1'


def test_models_route_unknown_provider_missing_key_is_not_404(tmp_path):
    """旧路径对未保存 provider 缺 Key 时也只是 ok=False，不再是 404。"""
    client, _api, _store = build_app(secrets={}, tmp_path=tmp_path)
    response = client.post('/providers/nope/models', json={
        'base_url': 'https://api.example.com/v1',
    })
    payload = response.get_json()

    assert response.status_code == 200
    assert payload['success'] is True
    assert payload['ok'] is False
    assert 'API Key' in payload['message']


def test_models_draft_route_uses_form_key_and_base_url(env, monkeypatch):
    """`POST /models`：未保存 provider + 表单里的地址/Key 即可拉取。"""
    client, _api, _store = env
    captured = []

    def fake_list_models(provider, api_key, *, extra_headers=None, transport=None):
        captured.append({'provider': provider, 'api_key': api_key,
                         'extra_headers': extra_headers})
        return {'ok': True, 'models': ['deepseek-chat'], 'protocol': 'openai_chat',
                'message': ''}

    monkeypatch.setattr(routes_module.llm, 'list_models', fake_list_models)
    payload = client.post('/models', json={
        'api_key': 'sk-form-secret',
        'base_url': 'https://api.deepseek.com/v1',
        'api_style': 'openai_chat',
        'allow_local': False,
        'connect_timeout': 3,
        'request_timeout': 30,
    }).get_json()

    assert payload['success'] is True
    assert payload['ok'] is True
    assert payload['models'] == ['deepseek-chat']
    provider = captured[0]['provider']
    assert provider['base_url'] == 'https://api.deepseek.com/v1'
    assert provider['connect_timeout'] == 3
    assert provider['request_timeout'] == 30
    assert captured[0]['api_key'] == 'sk-form-secret'
    # 密钥纪律：响应里绝不回显明文 Key
    assert 'sk-form-secret' not in json.dumps(payload, ensure_ascii=False)


def test_models_draft_route_overrides_saved_key(env, monkeypatch):
    """已保存 provider + 请求体带新 Key → 新 Key 覆盖已保存密钥。"""
    client, api, _store = env
    api.secrets.set('provider__openai__api_key', 'sk-saved')
    captured = []

    def fake_list_models(provider, api_key, *, extra_headers=None, transport=None):
        captured.append({'provider': provider, 'api_key': api_key})
        return {'ok': True, 'models': ['gpt-4o'], 'protocol': 'openai_chat', 'message': ''}

    monkeypatch.setattr(routes_module.llm, 'list_models', fake_list_models)
    payload = client.post('/models', json={
        'provider_id': 'openai', 'api_key': 'sk-from-form',
    }).get_json()

    assert payload['ok'] is True
    assert captured[0]['api_key'] == 'sk-from-form'
    # 未传 base_url 时沿用已保存 provider 的地址
    assert captured[0]['provider']['base_url'] == 'https://api.openai.com/v1'


def test_models_draft_route_falls_back_to_saved_key(env, monkeypatch):
    """回归：请求体不带 api_key 时仍用已保存的密钥。"""
    client, api, _store = env
    api.secrets.set('provider__openai__api_key', 'sk-saved')
    captured = []

    def fake_list_models(provider, api_key, *, extra_headers=None, transport=None):
        captured.append({'api_key': api_key})
        return {'ok': True, 'models': ['gpt-4o'], 'protocol': 'openai_chat', 'message': ''}

    monkeypatch.setattr(routes_module.llm, 'list_models', fake_list_models)
    payload = client.post('/models', json={'provider_id': 'openai'}).get_json()

    assert payload['ok'] is True
    assert captured[0]['api_key'] == 'sk-saved'


def test_models_draft_route_overrides_saved_headers(env, monkeypatch):
    """表单里的 extra_headers 覆盖已保存请求头；不传时回退已保存值。"""
    client, api, _store = env
    api.secrets.set('provider__openai__extra_headers', '{"X-Old": "old"}')
    captured = []

    def fake_list_models(provider, api_key, *, extra_headers=None, transport=None):
        captured.append({'extra_headers': extra_headers})
        return {'ok': True, 'models': ['gpt-4o'], 'protocol': 'openai_chat', 'message': ''}

    monkeypatch.setattr(routes_module.llm, 'list_models', fake_list_models)

    client.post('/models', json={'provider_id': 'openai',
                                 'extra_headers': '{"X-Title": "ST-Manager"}'})
    assert captured[-1]['extra_headers'] == {'X-Title': 'ST-Manager'}

    client.post('/models', json={'provider_id': 'openai'})
    assert captured[-1]['extra_headers'] == {'X-Old': 'old'}


def test_models_draft_route_requires_api_key(tmp_path):
    """未保存 provider 且不带 Key → 200 + ok=False（不阻塞配置）。"""
    client, _api, _store = build_app(secrets={}, tmp_path=tmp_path)
    response = client.post('/models', json={'base_url': 'https://api.example.com/v1'})
    payload = response.get_json()

    assert response.status_code == 200
    assert payload['success'] is True
    assert payload['ok'] is False
    assert payload['models'] == []
    assert 'API Key' in payload['message']


def test_models_draft_route_requires_base_url(env):
    client, _api, _store = env
    response = client.post('/models', json={'api_key': 'sk-x'})
    payload = response.get_json()

    assert response.status_code == 200
    assert payload['ok'] is False
    assert 'base_url' in payload['message']


def test_models_draft_route_rejects_bad_base_url(env):
    client, _api, _store = env
    payload = client.post('/models', json={
        'base_url': 'file:///etc/passwd', 'api_key': 'sk-x',
    }).get_json()

    assert payload['ok'] is False
    assert payload['models'] == []


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


def test_preview_returns_selector_summary_and_by_block(env):
    client, _api, _store = env
    payload = client.post('/prompt/preview', json={'card_id': 'Alice.png'}).get_json()

    # 选择器摘要
    assert 'selector' in payload
    assert payload['selector']['spec']['worldbook']['mode'] == 'auto'
    assert isinstance(payload['selector']['gaps'], list)

    # token 明细：总账与骨架拆分
    by_block = payload['tokens']['by_block']
    assert by_block['total'] == payload['tokens']['total']
    assert by_block['variables_total'] >= 0
    assert by_block['template_overhead'] >= 0
    # 至少 card.name 出现在明细里（默认内置档案引用了它）
    names = [item['name'] for item in by_block['items']]
    assert 'card.name' in names


def test_preview_by_block_is_sorted_by_cost(env):
    client, _api, _store = env
    payload = client.post('/prompt/preview', json={'card_id': 'Alice.png'}).get_json()
    costs = [item['tokens'] for item in payload['tokens']['by_block']['items']]
    assert costs == sorted(costs, reverse=True)


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
    """构造一条**已解析**的作业条目。

    形状与主程序 `api.jobs.items()` 的返回一致（`job_store._row_to_item`）：
    不是数据库原始行，`payload` / `result` 已是对象。

    `status` / `item_key` 等是**条目字段**，`result_overrides` 才是插件结果结构里的字段。
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
        'run_id': 'run-1',
        'key': item_key,
        'status': status,
        'seq': 0,
        'attempt': 1,
        'payload': {},
        'result': result,
        'error': '',
        'created_at': 0.0,
        'updated_at': 0.0,
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
                        lambda api_obj, store_obj, card_id, summary, **kwargs: written.update(
                            {card_id: summary}) or {'ok': True, 'origin': 'card_note',
                                                    'ui_key': kwargs.get('ui_key') or card_id,
                                                    'message': ''})

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


# ------------------------------------------------------------------ C13 删标签落地
#
# 「只加不减」：模型指出的 tag_removals 以前没有任何落地通道。


def _removal_row():
    return job_item_row(result_overrides={
        'tag_removals': [{'name': '奇幻', 'reason': '与卡片无关'}],
        'remove_tags': ['奇幻'],
    })


def test_apply_never_removes_frozen_category_tags(tmp_path):
    """历史作业的删除建议里若含「排除/隔离分类」的标签，落地时也必须跳过。

    分析阶段已经过滤过一次；这条兜底针对修复前生成的老作业。
    """
    library = {
        'tags': ['奇幻', '关系标签'],
        'taxonomy': {
            'tag_to_category': {'奇幻': '题材', '关系标签': '关系'},
            'categories': {'题材': [], '关系': []},
        },
        'prefs': {},
    }
    row = job_item_row(result_overrides={
        'tag_removals': [
            {'name': '奇幻', 'reason': 'r'},
            {'name': '关系标签', 'reason': 'r'},
        ],
        'remove_tags': ['奇幻', '关系标签'],
    })
    client, api, _store = build_app(
        cards={'Alice.png': dict(CARD, tags=['奇幻', '关系标签'])},
        tag_library=library, db_rows=[row], tmp_path=tmp_path,
    )
    api._config.update({'tag_pool_categories_exclude': '关系'})
    api.job_detail = {'run_id': 'run-1', 'job_type': 'ai.analyze', 'status': 'completed'}

    payload = client.post(
        '/jobs/run-1/apply', json={'remove_tags': ['奇幻', '关系标签']},
    ).get_json()

    removes = [entry['remove'] for entry in api.applied_tags if entry['remove']]
    assert removes == [['奇幻']], '冻结分类的标签不得被删除'
    assert any(
        '冻结分类' in str(item.get('warning', '')) for item in payload['details']
    ), payload['details']


def test_wi_entries_marks_prefilter_protected_entries(tmp_path, monkeypatch):
    """预览要能提前告诉用户「哪几条会被前置过滤跳过」（§7）。"""
    client, api, _store = build_app(tmp_path=tmp_path)
    book = {
        'entries': [
            {'uid': '1', 'comment': '[MVU] InitVar', 'content': '变量: 0'},
            {'uid': '2', 'comment': '世界观', 'content': '北境常年积雪。'},
        ],
        'source_revision': 'rev-1',
    }
    monkeypatch.setattr(api.worldbook, 'read', lambda source: book)

    payload = client.post('/wi/entries', json={'card_id': 'Alice.png'}).get_json()

    assert payload['success'] is True
    items = {item['uid']: item for item in payload['items']}
    assert items['1']['skip_reason'] == 'protected:name'
    assert items['1']['skip_label'], '要给人话原因'
    assert items['2']['skip_reason'] == ''


def test_apply_removes_selected_tags_only(tmp_path):
    client, api, _store = build_app(
        cards={'Alice.png': dict(CARD, tags=['奇幻', '冒险'])},
        db_rows=[_removal_row()], tmp_path=tmp_path,
    )
    api.job_detail = {'run_id': 'run-1', 'job_type': 'ai.analyze', 'status': 'completed'}

    payload = client.post('/jobs/run-1/apply', json={'remove_tags': ['奇幻']}).get_json()

    assert payload['success'] is True
    removes = [entry['remove'] for entry in api.applied_tags if entry['remove']]
    assert removes == [['奇幻']], '勾选的标签要真的被移除'


def test_apply_does_not_remove_unselected_tags(tmp_path):
    client, api, _store = build_app(
        cards={'Alice.png': dict(CARD, tags=['奇幻', '冒险'])},
        db_rows=[_removal_row()], tmp_path=tmp_path,
    )
    api.job_detail = {'run_id': 'run-1', 'job_type': 'ai.analyze', 'status': 'completed'}

    client.post('/jobs/run-1/apply', json={}).get_json()

    assert all(not entry['remove'] for entry in api.applied_tags), '默认一条都不删'


def test_apply_ignores_removals_the_card_does_not_have(tmp_path):
    """只删「结果里建议过 + 卡片确实有」的标签，防止误删。"""
    client, api, _store = build_app(
        cards={'Alice.png': dict(CARD, tags=['冒险'])},
        db_rows=[_removal_row()], tmp_path=tmp_path,
    )
    api.job_detail = {'run_id': 'run-1', 'job_type': 'ai.analyze', 'status': 'completed'}

    client.post('/jobs/run-1/apply', json={'remove_tags': ['奇幻']}).get_json()

    assert all(not entry['remove'] for entry in api.applied_tags)


# ------------------------------------------------------------------ C4：改写格式已删除
#
# 「改写格式」这个用户可选项被删掉了（§2.2）。响应里不再回传它，作业 payload
# 里也不再有它——但**旧客户端传了也不能报错**（兼容），只是被忽略。


def test_submit_wi_rewrite_ignores_target_format(tmp_path, monkeypatch):
    client, api, _store = build_app(tmp_path=tmp_path)
    book = {
        'entries': [{'uid': '7', 'comment': '世界观', 'content': '北境常年积雪。' * 12}],
        'source_revision': 'rev-1',
    }
    monkeypatch.setattr(api.worldbook, 'read', lambda source: book)

    payload = client.post('/jobs/wi-rewrite', json={
        'card_id': 'Alice.png',
        'entry_uids': ['7'],
        'target_format': 'yaml',       # 旧客户端仍会传
    }).get_json()

    assert payload['success'] is True
    assert 'target_format' not in payload, '响应里不再有格式字段'
    submitted = api.submitted
    assert submitted['job_type'] == 'ai.wi_rewrite'
    assert 'target_format' not in submitted['payload'], '作业参数里不再带格式'


# ------------------------------------------------------------------ C9：分类建失败不能报成功
#
# 修之前：9/9 分类全部创建失败（嵌套分类被宿主拒），接口仍返回 `success: true`，
# 用户以为方案已经落地。


def _propose_row(categories, tags=(), merges=()):
    result = {
        'plan': {
            'create_categories': list(categories),
            'create_tags': list(tags),
            'tag_merges': list(merges),
        },
    }
    return {
        'run_id': 'run-1', 'key': 'proposal', 'status': 'done', 'seq': 0,
        'attempt': 1, 'result': result, 'payload': {},
    }


def test_propose_apply_reports_failure_when_category_cannot_be_created(tmp_path):
    client, api, _store = build_app(db_rows=[_propose_row(['同人/动漫', '游戏'])],
                                    tmp_path=tmp_path)
    api.job_detail = {'run_id': 'run-1', 'job_type': 'ai.propose', 'status': 'completed'}
    # 宿主拒绝第一个分类（模拟 P0-5 的 `/` 名字被当叶子名拒掉）
    api.cards.create_category = lambda name, **kwargs: name != '同人/动漫'

    response = client.post('/jobs/run-1/apply', json={})
    payload = response.get_json()

    assert payload['success'] is False, '有分类没建出来就不能报成功'
    assert '同人/动漫' in payload['msg']
    assert payload['detail']['failed_categories'] == ['同人/动漫']
    assert payload['detail']['created_categories'] == ['游戏']


def test_propose_apply_reports_success_when_all_categories_created(tmp_path):
    client, api, _store = build_app(db_rows=[_propose_row(['同人/动漫', '游戏'])],
                                    tmp_path=tmp_path)
    api.job_detail = {'run_id': 'run-1', 'job_type': 'ai.propose', 'status': 'completed'}

    payload = client.post('/jobs/run-1/apply', json={}).get_json()

    assert payload['success'] is True
    assert payload['created_categories'] == ['同人/动漫', '游戏']
    assert payload['failed_categories'] == []


# ------------------------------------------------------------------ C10：分类列表两种形状都认
#
# 宿主 H4 之前返回字符串数组，之后返回 `{path, name, count}` 对象数组。
# 插件必须两种都能读：只升级一边的用户不该看到「永远 0 个分类」。


@pytest.mark.parametrize('raw,expected', [
    # 旧宿主：字符串数组
    (['同人', '同人/动漫'], [
        {'path': '同人', 'name': '同人', 'count': 0},
        {'path': '同人/动漫', 'name': '动漫', 'count': 0},
    ]),
    # 新宿主：对象数组
    ([{'path': '同人/动漫', 'name': '动漫', 'count': 12}], [
        {'path': '同人/动漫', 'name': '动漫', 'count': 12},
    ]),
    # 混杂 + 脏值：能读的读，读不了的丢
    (['游戏', {'path': '同人'}, {'name': '科幻'}, '', None], [
        {'path': '游戏', 'name': '游戏', 'count': 0},
        {'path': '同人', 'name': '同人', 'count': 0},
        {'path': '科幻', 'name': '科幻', 'count': 0},
    ]),
])
def test_normalize_category_items_accepts_both_shapes(raw, expected):
    classify_core = load_backend_submodule('core.classify')
    assert classify_core.normalize_category_items(raw) == expected


def test_categories_endpoint_returns_objects_for_old_host(tmp_path):
    """旧宿主给字符串数组时，插件也要归一成对象再给前端。"""
    client, api, _store = build_app(tmp_path=tmp_path)
    api.cards.list_categories = lambda: ['同人', '同人/动漫']

    payload = client.get('/categories').get_json()

    assert payload['success'] is True
    assert payload['items'] == [
        {'path': '同人', 'name': '同人', 'count': 0},
        {'path': '同人/动漫', 'name': '动漫', 'count': 0},
    ]


def test_categories_endpoint_passes_through_objects(tmp_path):
    """新宿主给对象数组时原样透传（含 count）。"""
    client, api, _store = build_app(tmp_path=tmp_path)
    api.cards.list_categories = lambda: [
        {'path': '同人/动漫', 'name': '动漫', 'count': 12},
    ]

    payload = client.get('/categories').get_json()
    assert payload['items'] == [{'path': '同人/动漫', 'name': '动漫', 'count': 12}]


# ------------------------------------------------------------------ P1-5 标签合并执行
#
# 修之前 `tag_merges` 只展示、`requires_manual_review` 恒 true —— 「合并」根本没有
# 落地通道。宿主的 batch_tags 早有 remove，所以「重命名」= add(to) + remove(from)。


def _card_row(card_id, tags):
    return {'id': card_id, 'char_name': card_id, 'tags': list(tags)}


def test_propose_apply_executes_selected_tag_merges(tmp_path):
    merge = {'from': ['旧名', '别称'], 'to': '规范名'}
    client, api, _store = build_app(
        db_rows=[_card_row('a.png', ['旧名', '奇幻']), _card_row('b.png', ['别称'])],
        tmp_path=tmp_path,
    )
    api.job_detail = {'run_id': 'run-1', 'job_type': 'ai.propose', 'status': 'completed'}
    api.job_items = [_propose_row([], merges=[merge])]

    payload = client.post('/jobs/run-1/apply', json={'merges': [merge]}).get_json()

    assert payload['success'] is True
    executed = payload['merges_executed'][0]
    assert executed['to'] == '规范名'
    assert executed['moved'] == 2
    assert executed['failed'] == []
    assert payload['requires_manual_review'] is False

    moves = {(entry['card_ids'][0], tuple(entry['remove'])): entry['add'] for entry in api.applied_tags}
    assert moves[('a.png', ('旧名',))] == ['规范名']
    assert moves[('b.png', ('别称',))] == ['规范名']


def test_propose_apply_does_not_merge_without_selection(tmp_path):
    merge = {'from': ['旧名'], 'to': '规范名'}
    client, api, _store = build_app(
        db_rows=[_card_row('a.png', ['旧名'])], tmp_path=tmp_path,
    )
    api.job_detail = {'run_id': 'run-1', 'job_type': 'ai.propose', 'status': 'completed'}
    api.job_items = [_propose_row([], merges=[merge])]

    payload = client.post('/jobs/run-1/apply', json={}).get_json()

    assert payload['merges_executed'] == []
    assert payload['requires_manual_review'] is True, '没勾选就还是「需人工处理」'
    assert all(not entry['remove'] for entry in api.applied_tags)


def test_propose_apply_reports_invalid_merge(tmp_path):
    client, api, _store = build_app(db_rows=[], tmp_path=tmp_path)
    api.job_detail = {'run_id': 'run-1', 'job_type': 'ai.propose', 'status': 'completed'}
    api.job_items = [_propose_row([], merges=[])]

    payload = client.post(
        '/jobs/run-1/apply', json={'merges': [{'from': ['x'], 'to': ''}]},
    ).get_json()

    assert payload['merges_executed'][0]['error'] == 'invalid_merge'


# ------------------------------------------------------------------ 模型能力设置


def test_settings_returns_model_capability_defaults(env):
    client, _api, _store = env
    payload = client.get('/settings').get_json()

    assert payload['success'] is True
    caps = payload['model_capabilities']
    assert caps['context_length'] == 262144, '默认上下文 256K'
    assert caps['max_output_tokens'] == 32768, '默认输出 32K'
    assert caps['reasoning_enabled'] is True
    assert caps['reasoning_effort'] == 'auto'


def test_settings_saves_and_normalizes_model_capabilities(env):
    client, api, _store = env

    payload = client.post('/settings', json={
        'model_capabilities': {
            'context_length': 65536,
            'max_output_tokens': 999999,   # 超过上限 → 收敛到 262144 且不超过上下文
            'reasoning_enabled': False,
            'reasoning_effort': 'bogus',   # 非法值 → auto
        },
    }).get_json()

    assert payload['success'] is True
    caps = payload['model_capabilities']
    assert caps['context_length'] == 65536
    assert caps['max_output_tokens'] == 65536, '输出上限不得超过上下文'
    assert caps['reasoning_enabled'] is False
    assert caps['reasoning_effort'] == 'auto'
    assert api._config['context_length'] == 65536


# ------------------------------------------------------------------ 标签池（分类白/黑名单）

TAG_LIBRARY = {
    'tags': ['奇幻', '冒险', '校园', 'R18', '未分类'],
    'taxonomy': {
        'tag_to_category': {
            '奇幻': '题材', '冒险': '题材', '校园': '场景', 'R18': '成人',
        },
        'categories': {'题材': [], '场景': [], '成人': []},
    },
    'prefs': {'isolated_categories': ['成人']},
}


def test_tag_pool_lists_categories_and_current_pool(tmp_path):
    client, _api, _store = build_app(tag_library=TAG_LIBRARY, tmp_path=tmp_path)
    payload = client.get('/tag-pool').get_json()

    assert payload['success'] is True
    assert payload['tag_pool']['categories_include'] == []
    assert payload['tag_pool']['categories_exclude'] == []
    # 分类按名字排序（Unicode 码位：场景 < 成人 < 题材）；`isolated` 来自主程序的隔离分类
    assert [item['name'] for item in payload['categories']] == ['场景', '成人', '题材']
    assert payload['isolated'] == ['成人']
    assert payload['total_tags'] == 5
    assert payload['uncategorized'] == 1, '「未分类」没有 tag_to_category'
    # 隔离分类下的标签默认就不可用
    assert payload['effect']['allowed'] == 4


def test_tag_pool_accepts_chinese_separators(tmp_path):
    """非程序员不会只打英文逗号：中文逗号 / 顿号 / 换行都要能解析。"""
    client, api, _store = build_app(tag_library=TAG_LIBRARY, tmp_path=tmp_path)
    payload = client.post('/tag-pool', json={
        'categories_include': '题材，场景、\n题材',
        'categories_exclude': '成人',
    }).get_json()

    assert payload['success'] is True
    pool = payload['tag_pool']
    assert pool['categories_include'] == ['题材', '场景'], '去重 + 去空白 + 保序'
    assert pool['categories_exclude'] == ['成人']
    # 存的是**已声明**的字符串键：宿主的 config.set 只接受 config_schema 里的键，
    # 写内部结构 `tag_pool` 会被拒绝（实测返回 False，前端报「配置未写入」）。
    assert api._config['tag_pool_categories_include'] == '题材，场景'
    assert api._config['tag_pool_categories_exclude'] == '成人'
    assert 'tag_pool' not in api._config, '不要写未声明的键'

    # 生效效果：无分类的标签在「只取」下也被排除（否则设了像没设）
    assert payload['effect']['allowed'] == 3, '奇幻/冒险/校园'
    blocked = payload['effect']['blocked_sample']
    assert 'R18' in blocked, '隔离分类'
    assert '未分类' in blocked, '只取模式下未分类标签被排除'


def test_tag_pool_saves_per_category_rules_and_total(tmp_path):
    """每分类规则 + 最大总数量要落到**已声明**的键里，并能原样读回来。"""
    client, api, _store = build_app(tag_library=TAG_LIBRARY, tmp_path=tmp_path)

    payload = client.post('/tag-pool', json={
        'max_tags': 6,
        'categories_include': [],
        'categories_exclude': [],
        'rules': {
            '题材': {'confidence_min': 0.6, 'max_tags': 3},
            '场景': {'max_tags': 2},
        },
    }).get_json()

    assert payload['success'] is True
    assert payload['max_tags'] == 6
    assert payload['rules']['题材'] == {'confidence_min': 0.6, 'max_tags': 3}
    assert payload['allocation']['total'] == 5
    assert payload['allocation']['over'] == 0
    assert api._config['max_tags'] == 6
    assert '题材' in api._config['tag_category_rules'], '规则以 JSON 字符串存'

    again = client.get('/tag-pool').get_json()
    assert again['rules']['题材']['confidence_min'] == 0.6
    assert again['max_tags'] == 6


def test_tag_pool_warns_but_saves_when_quota_sum_over_total(tmp_path):
    """各分类数量之和 > 总上限：**只提醒，不拦保存**（用户选定的行为）。

    实际打标时按总上限截断，所以各分类配额此时只是参考值。
    """
    client, api, _store = build_app(tag_library=TAG_LIBRARY, tmp_path=tmp_path)

    payload = client.post('/tag-pool', json={
        'max_tags': 5,
        'rules': {'题材': {'max_tags': 3}, '场景': {'max_tags': 4}},
    }).get_json()

    assert payload['success'] is True, '超限也必须能保存'
    assert payload['allocation']['over'] == 2
    assert any(
        '超过最大总数量' in text and '按总上限' in text for text in payload['warnings']
    ), payload['warnings']
    # 规则照样落盘
    assert api._config['max_tags'] == 5
    assert '场景' in api._config['tag_category_rules']


def test_tag_pool_allows_total_greater_than_quota_sum(tmp_path):
    """总上限可以大于各分类之和（剩下的留给没单独设置的分类）。"""
    client, api, _store = build_app(tag_library=TAG_LIBRARY, tmp_path=tmp_path)
    payload = client.post('/tag-pool', json={
        'max_tags': 20, 'rules': {'题材': {'max_tags': 2}},
    }).get_json()

    assert payload['success'] is True
    assert payload['allocation'] == {
        'total': 2, 'max_tags': 20, 'over': 0, 'quotas': {'题材': 2},
    }


def test_tag_pool_rejects_invalid_total(tmp_path):
    client, _api, _store = build_app(tag_library=TAG_LIBRARY, tmp_path=tmp_path)
    for value in (0, 999, 'abc', -1):
        response = client.post('/tag-pool', json={'max_tags': value})
        assert response.status_code == 400, value


def test_tag_pool_saves_budget_tokens(tmp_path):
    """标签池 token 预算可调，并走已声明的 `tag_pool_budget_tokens` 键。"""
    client, api, _store = build_app(tag_library=TAG_LIBRARY, tmp_path=tmp_path)

    payload = client.post('/tag-pool', json={'max_tags': 8, 'budget_tokens': 3000}).get_json()

    assert payload['success'] is True
    assert payload['budget_tokens'] == 3000
    assert api._config['tag_pool_budget_tokens'] == 3000

    again = client.get('/tag-pool').get_json()
    assert again['budget_tokens'] == 3000
    assert again['default_budget_tokens'] == 1200


def test_tag_pool_rejects_bad_budget(tmp_path):
    client, _api, _store = build_app(tag_library=TAG_LIBRARY, tmp_path=tmp_path)
    for value in (0, 100, 99999, 'abc'):
        response = client.post('/tag-pool', json={'budget_tokens': value})
        assert response.status_code == 400, value


def test_tag_pool_budget_is_clamped_by_context(tmp_path):
    """预算不能超过模型上下文可用额度（8K 上下文时 20000 会被夹到 ~3K）。"""
    client, api, _store = build_app(tag_library=TAG_LIBRARY, tmp_path=tmp_path)
    api._config.update({'context_length': 8192, 'max_output_tokens': 1024})

    payload = client.post('/tag-pool', json={'budget_tokens': 20000}).get_json()

    assert payload['success'] is True
    assert payload['budget_tokens'] == 3072, '8192 - 1024 - 4096 = 3072'
    assert payload['budget_tokens'] < 20000


def test_tag_pool_reads_declared_string_keys(tmp_path):
    """发一次保存后，再读回来必须还是那份值（配置页表单直接改这两个键也一样）。"""
    client, api, _store = build_app(tag_library=TAG_LIBRARY, tmp_path=tmp_path)
    api._config.update({
        'tag_pool_categories_include': '题材，场景',
        'tag_pool_categories_exclude': '成人',
    })

    payload = client.get('/tag-pool').get_json()

    assert payload['tag_pool']['categories_include'] == ['题材', '场景']
    assert payload['tag_pool']['categories_exclude'] == ['成人']


def test_tag_pool_counts_unmapped_tags_as_default_category(tmp_path):
    """没有分类映射的标签属于宿主的 `default_category`（通常叫「未整理」）。

    不计入的话「未整理」的数量会比宿主界面少一大截（实测 12 vs 287）。
    """
    library = {
        'tags': ['奇幻', '散装标签'],
        'taxonomy': {
            'default_category': '未整理',
            'tag_to_category': {'奇幻': '题材'},
            'categories': {'题材': []},
        },
        'prefs': {},
    }
    client, _api, _store = build_app(tag_library=library, tmp_path=tmp_path)

    payload = client.get('/tag-pool').get_json()

    names = {item['name']: item['tags'] for item in payload['categories']}
    assert names['未整理'] == 1, '未映射的标签计入默认分类'
    assert names['题材'] == 1
    assert payload['uncategorized'] == 0, '有了默认分类就不该再报「无分类」'


def test_tag_pool_accepts_arrays_and_reports_unknown_names(tmp_path):
    client, _api, _store = build_app(tag_library=TAG_LIBRARY, tmp_path=tmp_path)
    payload = client.post('/tag-pool', json={
        'categories_include': ['题材', '题材库'],  # 后者拼错了
        'categories_exclude': [],
    }).get_json()

    assert payload['success'] is True
    assert payload['tag_pool']['categories_include'] == ['题材', '题材库']
    assert any('题材库' in text for text in payload['warnings']), payload['warnings']


def test_tag_pool_rejects_category_on_both_sides(tmp_path):
    client, api, _store = build_app(tag_library=TAG_LIBRARY, tmp_path=tmp_path)
    before = dict(api._config)

    response = client.post('/tag-pool', json={
        'categories_include': '题材',
        'categories_exclude': '题材,成人',
    })
    payload = response.get_json()

    assert response.status_code == 400
    assert payload['success'] is False
    assert '不能同时' in payload['msg']
    assert api._config == before, '冲突时一个字节都不该写'


def test_suggestions_hide_tags_already_in_library(tmp_path):
    """建议队列里「已经在标签库里」的条目不再显示（历史作业留下的脏数据）。"""
    client, api, _store = build_app(tag_library=TAG_LIBRARY, tmp_path=tmp_path)
    api._ui['tag_suggestions'] = {
        '真的新标签': {'count': 2, 'cards': ['a.png'], 'reason': 'r',
                   'first_seen': 0, 'last_seen': 0},
        '奇幻': {'count': 1, 'cards': [], 'reason': 'r',
               'first_seen': 0, 'last_seen': 0},
    }

    payload = client.get('/suggestions').get_json()

    assert [item['name'] for item in payload['items']] == ['真的新标签']
    assert payload['hidden_existing'] == 1, '已有标签应按「已存在」隐藏'
    assert payload['total'] == 1


def test_suggestions_clear_still_works(tmp_path):
    client, api, store = build_app(tag_library=TAG_LIBRARY, tmp_path=tmp_path)
    api._ui['tag_suggestions'] = {
        '真的新标签': {'count': 1, 'cards': [], 'reason': '', 'first_seen': 0, 'last_seen': 0},
    }
    payload = client.post('/suggestions/clear', json={'names': ['真的新标签']}).get_json()

    assert payload['success'] is True
    assert payload['removed'] == ['真的新标签']
    assert store.list_tag_suggestions() == {}


# ------------------------------------------------------------------ 归类落地：弃权不得移动


def test_apply_classify_never_moves_undecided_to_root(tmp_path):
    """弃权（category 留空）的条目即使被「接受」，也不能被当成根目录移动。"""
    client, api, store = build_app(tmp_path=tmp_path)
    moves = []
    api.cards.move = lambda ids, target: (
        moves.append((list(ids), target)) or {'count': len(ids), 'failed': []}
    )
    api.cards.create_category = lambda name, parent='': True
    api.job_detail = {'run_id': 'run-1', 'job_type': 'ai.classify', 'status': 'completed'}
    api.job_items = [
        {'key': 'A.png', 'status': 'done',
         'result': {'card_id': 'A.png', 'category': '', 'skipped': 'undecided'}},
        {'key': 'B.png', 'status': 'done',
         'result': {'card_id': 'B.png', 'category': '奇幻'}},
    ]

    payload = client.post('/jobs/run-1/apply', json={}).get_json()

    assert payload['applied'] == 1
    assert payload['skipped'] == 1
    assert moves == [(['B.png'], '奇幻')], '弃权条目绝不能移动（空分类=根目录）'
    assert payload['details'][0]['error'] == 'undecided'


# ------------------------------------------------------------------ Agent 预算透传


def test_submit_agent_keeps_raw_budget_for_adaptive_iterations(tmp_path):
    """提交时不能把 budget 归一化掉：作业层要靠「有没有 max_iterations」决定是否自适应。"""
    client, api, _store = build_app(tmp_path=tmp_path)

    payload = client.post('/jobs/agent', json={'budget': {'max_reads': 12}}).get_json()

    assert payload['success'] is True
    assert payload['budget_adaptive'] is True
    assert payload['budget']['max_iterations'] is None, '自适应时回执不能谎报成默认 6'
    submitted = api.submitted
    assert submitted['payload']['budget'] == {'max_reads': 12}, (
        '作业 payload 里必须是用户原始 budget，不能预先填 max_iterations=6'
    )
