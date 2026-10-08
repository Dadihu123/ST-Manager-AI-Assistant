"""持久化层：档案文件、provider 与密钥隔离、元数据裁剪、建议队列、用量台账。"""

import json
import os

import pytest

from conftest import load_backend_submodule
from fake_api import FakeApi

pytest.importorskip('flask', reason='装配层依赖 flask（主程序运行环境必备）')
store_module = load_backend_submodule('store')
profiles_core = load_backend_submodule('core.profiles')
errors = load_backend_submodule('core.errors')

PluginStore = store_module.PluginStore
AiAssistantError = errors.AiAssistantError


@pytest.fixture
def api(tmp_path):
    return FakeApi(
        data_dir=str(tmp_path / 'data'),
        secrets={'api_key': 'sk-default', 'base_url': 'https://api.openai.com/v1'},
    )


@pytest.fixture
def store(api):
    return PluginStore(api)


def make_profile(profile_id: str, **overrides) -> dict:
    """以内置档案为骨架构造合法档案（system / user_template 非空是硬校验）。"""
    data = dict(profiles_core.builtin_profile('builtin-default'))
    data.update({'id': profile_id, 'name': profile_id, 'builtin': False, 'version': 1})
    data.update(overrides)
    return data


# ------------------------------------------------------------------ 档案


def test_builtin_profiles_are_listed_without_files(store):
    ids = {item['id'] for item in store.list_profiles()}
    assert 'builtin-default' in ids
    assert 'builtin-gpt' in ids


def test_save_profile_persists_to_prompts_dir(store):
    saved = store.save_profile(make_profile('my-gpt', name='我的', model_match='gpt-*'))
    assert saved['id'] == 'my-gpt'
    assert saved['version'] == 1

    path = os.path.join(store.prompts_dir(), 'my-gpt.json')
    assert os.path.isfile(path)

    with open(path, 'r', encoding='utf-8') as handle:
        payload = json.load(handle)
    assert payload['name'] == '我的'


def test_save_profile_bumps_version(store):
    store.save_profile(make_profile('my-gpt', name='v1'))
    again = store.save_profile(make_profile('my-gpt', name='v2'))
    assert again['version'] == 2
    assert store.get_profile('my-gpt')['name'] == 'v2'


def test_save_profile_rejects_unknown_variable(store):
    with pytest.raises(AiAssistantError) as excinfo:
        store.save_profile(make_profile(
            'bad-profile', name='bad', user_template='{{ card.nickname }}'))
    assert excinfo.value.code == errors.CODE_PROFILE_INVALID
    assert any('未知变量' in item for item in excinfo.value.detail['errors'])


def test_save_profile_rejects_empty_templates(store):
    with pytest.raises(AiAssistantError) as excinfo:
        store.save_profile({'id': 'empty-profile', 'name': 'x', 'system': '', 'user_template': ''})
    assert excinfo.value.code == errors.CODE_PROFILE_INVALID


def test_user_profile_overrides_builtin_of_same_id(store):
    store.save_profile(dict(profiles_core.builtin_profile('builtin-gpt'), name='改过的名字'))
    merged = {item['id']: item for item in store.list_profiles()}
    assert merged['builtin-gpt']['name'] == '改过的名字'


def test_delete_user_profile_reverts_to_builtin(store):
    store.save_profile(dict(profiles_core.builtin_profile('builtin-claude'), name='临时'))
    result = store.delete_profile('builtin-claude')

    assert result['restored_builtin'] is True
    assert store.get_profile('builtin-claude')['name'] != '临时'


def test_delete_unknown_profile_raises(store):
    with pytest.raises(AiAssistantError) as excinfo:
        store.delete_profile('nope')
    assert excinfo.value.code == errors.CODE_PROFILE_NOT_FOUND


def test_duplicate_profile_generates_unique_id(store):
    first = store.duplicate_profile('builtin-gpt')
    second = store.duplicate_profile('builtin-gpt')

    assert first['id'] == 'builtin-gpt-copy'
    assert second['id'] == 'builtin-gpt-copy-2'
    assert first['builtin'] is False
    assert first['version'] == 1


def test_reset_profiles_removes_user_files_and_backs_up(store):
    store.save_profile(make_profile('custom-a', name='A'))
    result = store.reset_profiles()

    assert 'custom-a' in result['removed']
    assert os.path.isdir(result['backup_dir'])
    assert os.listdir(result['backup_dir'])


def test_export_and_import_profiles(store):
    store.save_profile(make_profile('custom-a', name='A'))
    payload = store.export_profiles()
    assert payload['schema_version'] == profiles_core.SCHEMA_VERSION
    assert any(item['id'] == 'custom-a' for item in payload['profiles'])

    result = store.import_profiles(payload)
    imported_ids = {item['id'] for item in result['imported']}
    assert 'custom-a-imported' in imported_ids


def test_import_rejects_invalid_payload(store):
    with pytest.raises(AiAssistantError) as excinfo:
        store.import_profiles('not-a-profile')
    assert excinfo.value.code == errors.CODE_INVALID_PAYLOAD


def test_resolve_profile_by_id_and_by_model(store):
    store.save_profile(make_profile('my-gpt', model_match='gpt-*', priority=999))
    assert store.resolve_profile('my-gpt', '')['id'] == 'my-gpt'
    assert store.resolve_profile('', 'gpt-4o')['id'] == 'my-gpt'
    assert store.resolve_profile('', 'unknown-model')['id'] == 'builtin-default'


def test_resolve_unknown_profile_raises(store):
    with pytest.raises(AiAssistantError) as excinfo:
        store.resolve_profile('missing-profile', '')
    assert excinfo.value.code == errors.CODE_PROFILE_NOT_FOUND


def test_profile_index_is_written_into_ui_namespace(store, api):
    store.sync_profile_index()
    index = api.ui_data_read().get('prompt_index')
    assert isinstance(index, list)
    assert all('system' not in item for item in index), '索引不应包含模板正文'


def test_ui_mutation_preserves_foreign_keys(store, api):
    """C3 回归：store 的任何写入都不得清掉主程序管理的键。"""
    api.ui_data_update(lambda entry: entry.update({'config': {'x': 1}, 'enabled': True}))
    store.sync_profile_index()
    store.record_usage({'prompt_tokens': 1, 'completion_tokens': 2})
    store.record_tag_suggestions([{'name': 'T'}], card_key='c1')

    namespace = api.ui_data_read()
    assert namespace['config'] == {'x': 1}
    assert namespace['enabled'] is True


# ------------------------------------------------------------------ provider


def test_first_run_creates_default_provider_from_secrets(store):
    config = store.providers_config()
    assert config['items'], '首次运行应自动生成一条 provider'
    assert config['items'][0]['base_url'] == 'https://api.openai.com/v1'
    assert config['default_id'] == config['items'][0]['id']


def test_provider_id_from_base_url():
    assert store_module.provider_id_from_base_url('https://api.openai.com/v1') == 'openai'
    assert store_module.provider_id_from_base_url('https://api.deepseek.com/v1') == 'deepseek'
    assert store_module.provider_id_from_base_url('http://localhost:11434/v1') == 'localhost'
    assert store_module.provider_id_from_base_url('') == 'default'


def test_save_provider_persists_and_keeps_default(store):
    store.save_provider({'id': 'deepseek', 'name': 'DS', 'base_url': 'https://api.deepseek.com/v1'})
    ids = {item['id'] for item in store.list_providers()}
    assert {'openai', 'deepseek'} <= ids


def test_save_provider_rejects_bad_url(store):
    with pytest.raises(AiAssistantError) as excinfo:
        store.save_provider({'id': 'bad', 'name': 'bad', 'base_url': 'ftp://x/y'})
    assert excinfo.value.code == errors.CODE_INVALID_PAYLOAD


def test_save_provider_rejects_bad_id(store):
    with pytest.raises(AiAssistantError):
        store.save_provider({'id': 'Bad Id', 'name': 'x', 'base_url': 'https://a.com/v1'})


def test_provider_secrets_are_isolated_per_provider(store, api):
    store.save_provider({'id': 'deepseek', 'name': 'DS', 'base_url': 'https://api.deepseek.com/v1'})
    store.set_provider_secrets('deepseek', api_key='sk-deepseek')

    assert store.resolve_provider_key('deepseek') == 'sk-deepseek'
    assert api._secrets['provider__deepseek__api_key'] == 'sk-deepseek'


def test_default_provider_falls_back_to_plain_api_key(store):
    config = store.providers_config()
    default_id = config['default_id']
    assert store.resolve_provider_key(default_id) == 'sk-default'


def test_non_default_provider_does_not_fall_back(store):
    store.save_provider({'id': 'deepseek', 'name': 'DS', 'base_url': 'https://api.deepseek.com/v1'})
    assert store.resolve_provider_key('deepseek') == ''


def test_clearing_provider_secret(store):
    store.set_provider_secrets('openai', api_key='sk-new')
    assert store.resolve_provider_key('openai') == 'sk-new'

    store.set_provider_secrets('openai', api_key='')
    assert store.resolve_provider_key('openai') == 'sk-default', '清除专属密钥后应回退到默认密钥'


def test_provider_secrets_reject_non_object_headers(store):
    with pytest.raises(AiAssistantError) as excinfo:
        store.set_provider_secrets('openai', extra_headers='[1, 2]')
    assert excinfo.value.code == errors.CODE_INVALID_PAYLOAD


def test_provider_headers_round_trip(store):
    store.set_provider_secrets('openai', extra_headers={'X-Title': 'ST'})
    assert store.resolve_provider_headers('openai') == {'X-Title': 'ST'}


def test_provider_headers_invalid_json_is_ignored(store, api):
    api._secrets[store_module.extra_headers_secret_name('openai')] = 'not json'
    assert store.resolve_provider_headers('openai') == {}


def test_delete_default_provider_is_refused(store):
    default_id = store.providers_config()['default_id']
    with pytest.raises(AiAssistantError):
        store.delete_provider(default_id)


def test_delete_provider_clears_its_secrets(store, api):
    store.save_provider({'id': 'deepseek', 'name': 'DS', 'base_url': 'https://api.deepseek.com/v1'})
    store.set_provider_secrets('deepseek', api_key='sk-deepseek')
    store.delete_provider('deepseek')

    assert 'provider__deepseek__api_key' not in api._secrets


def test_set_default_provider(store):
    store.save_provider({'id': 'deepseek', 'name': 'DS', 'base_url': 'https://api.deepseek.com/v1'})
    store.set_default_provider('deepseek')
    assert store.providers_config()['default_id'] == 'deepseek'


def test_describe_providers_never_leaks_secret_value(store):
    store.set_provider_secrets('openai', api_key='sk-super-secret-value')
    described = store.describe_providers()
    dumped = json.dumps(described, ensure_ascii=False)
    assert 'sk-super-secret-value' not in dumped
    assert any(item.get('has_api_key') for item in described)


def test_resolve_unknown_provider_raises(store):
    with pytest.raises(AiAssistantError) as excinfo:
        store.resolve_provider('nope')
    assert excinfo.value.code == errors.CODE_PROVIDER_NOT_FOUND


# ------------------------------------------------------------------ 卡片元数据


def test_card_meta_round_trip(store):
    store.update_card_meta('card-a', {'content_hash': 'sha256:abc', 'model': 'm'})
    meta = store.get_card_meta('card-a')
    assert meta['content_hash'] == 'sha256:abc'
    assert meta['model'] == 'm'
    assert meta['updated_at'] > 0


def test_card_meta_is_persisted_to_jsonl(store):
    store.update_card_meta('card-a', {'content_hash': 'h1'})
    records = store._read_jsonl(store.card_meta_path())
    assert records and records[-1]['ui_key'] == 'card-a'


def test_card_meta_ui_snapshot_is_size_limited(store, api):
    for index in range(20):
        store.update_card_meta(f'card-{index}', {'blob': 'x' * 20000})

    snapshot = api.ui_data_read().get('card_meta') or {}
    size = len(json.dumps(snapshot, ensure_ascii=False).encode('utf-8'))
    assert size <= store_module.CARD_META_UI_BYTES_LIMIT


def test_card_meta_ui_snapshot_is_count_limited(store, api):
    for index in range(store_module.CARD_META_UI_LIMIT + 50):
        store.update_card_meta(f'card-{index}', {'n': index})

    snapshot = api.ui_data_read().get('card_meta') or {}
    assert len(snapshot) <= store_module.CARD_META_UI_LIMIT


def test_full_card_meta_survives_ui_trimming(store):
    for index in range(store_module.CARD_META_UI_LIMIT + 20):
        store.update_card_meta(f'card-{index}', {'n': index})

    # 热缓存被裁剪，但 jsonl 里仍能查到最早那张卡
    assert store.get_card_meta('card-0')['n'] == 0


# ------------------------------------------------------------------ 建议队列与用量


def test_tag_suggestions_accumulate_counts(store):
    store.record_tag_suggestions([{'name': '赛博朋克', 'reason': 'r1'}], card_key='c1')
    store.record_tag_suggestions([{'name': '赛博朋克'}], card_key='c2')

    queue = store.list_tag_suggestions()
    assert queue['赛博朋克']['count'] == 2
    assert queue['赛博朋克']['cards'] == ['c1', 'c2']
    assert queue['赛博朋克']['reason'] == 'r1'


def test_clear_tag_suggestions(store):
    store.record_tag_suggestions([{'name': 'A'}, {'name': 'B'}])
    result = store.clear_tag_suggestions(['A'])
    assert result['removed'] == ['A']
    assert set(store.list_tag_suggestions()) == {'B'}


def test_usage_ledger_accumulates(store):
    store.record_usage({'prompt_tokens': 100, 'completion_tokens': 20})
    store.record_usage({'prompt_tokens': 50, 'completion_tokens': 10})

    ledger = store.usage_ledger()
    assert ledger['total_prompt_tokens'] == 150
    assert ledger['total_completion_tokens'] == 30
    assert ledger['jobs'] == 2


def test_recent_usage_reads_jsonl_tail(store):
    for index in range(5):
        store.record_usage({'prompt_tokens': index, 'completion_tokens': 0})
    recent = store.recent_usage(3)
    assert [item['prompt_tokens'] for item in recent] == [2, 3, 4]


def test_pending_reviews_round_trip(store):
    store.mark_pending_review('run-1', 'ai.analyze', 12)
    assert store.list_pending_reviews()['run-1']['items'] == 12

    store.clear_pending_review('run-1')
    assert store.list_pending_reviews() == {}


def test_outbound_acknowledgement(store):
    assert store.outbound_acknowledged() is False
    store.acknowledge_outbound()
    assert store.outbound_acknowledged() is True


def test_atomic_write_leaves_no_temp_file(store):
    store.save_profile(make_profile('atomic', name='A'))
    directory = store.prompts_dir()
    assert not [name for name in os.listdir(directory) if name.endswith('.tmp')]
