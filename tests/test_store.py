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


def test_duplicated_functional_profile_is_actually_selected(store):
    """「复制内置归类档案 → 改提示词」必须真的生效。

    这条路是「分类描述拿不到 → 让用户自己改提示词」的兜底。以前有两处会把它
    吃掉：①`normalize_profile` 把空 `model_match` 补成 `*`（副本就再也当不了
    任务专用档案）；②同优先级时按 id 排序，`builtin-classify` 恒定排在前面。
    """
    copy = store.duplicate_profile('builtin-classify', new_id='my-classify')

    assert copy['task'] == 'classify', '副本要继承任务归属'
    assert copy['builtin'] is False
    assert copy['model_match'] == '', '空串才能让它走「按任务选中」那条路'
    assert copy['system'], '提示词要带过来给用户改'

    selected = store.resolve_profile('', 'deepseek-chat', task='classify')
    assert selected['id'] == 'my-classify', '用户副本要盖过内置档案'

    # 删掉副本后回到内置档案
    store.delete_profile('my-classify')
    assert store.resolve_profile('', 'deepseek-chat', task='classify')['id'] == 'builtin-classify'


def test_profile_index_is_written_into_ui_namespace(store, api):
    store.sync_profile_index()
    index = api.private.read().get('prompt_index')
    assert isinstance(index, list)
    assert all('system' not in item for item in index), '索引不应包含模板正文'


def test_ui_mutation_preserves_foreign_keys(store, api):
    """C3 回归：store 的任何写入都不得清掉主程序管理的键。"""
    api.private.update(lambda entry: entry.update({'config': {'x': 1}, 'enabled': True}))
    store.sync_profile_index()
    store.record_usage({'prompt_tokens': 1, 'completion_tokens': 2})
    store.record_tag_suggestions([{'name': 'T'}], card_key='c1')

    namespace = api.private.read()
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


def test_card_meta_is_not_mirrored_into_ui_data(store, api):
    """card_meta 不再镜像进 ui_data（镜像曾占 ~180 KiB，顶爆宿主配额）。"""
    store.update_card_meta('card-a', {'summary': '概览'})

    assert 'card_meta' not in (api.private.read() or {}), 'ui_data 里不该再有镜像'
    assert store.get_card_meta('card-a')['summary'] == '概览', '数据仍从文件缓存读取'


def test_card_meta_ui_mirror_is_migrated_before_removal(store, api):
    """迁移：只在 ui_data 镜像里的记录要先落文件，再删镜像（不能丢 applied_runs）。"""
    ui = api.private.read()
    ui['card_meta'] = {'legacy-card': {
        'ui_key': 'legacy-card', 'n': 1, 'applied_runs': ['run-old'],
    }}
    api.private.update(lambda entry: entry.update({'card_meta': ui['card_meta']}))

    store.sync_card_meta_ui()

    assert 'card_meta' not in (api.private.read() or {})
    assert store.get_card_meta('legacy-card')['applied_runs'] == ['run-old']


def test_full_card_meta_survives_ui_removal(store):
    for index in range(120):
        store.update_card_meta(f'card-{index}', {'n': index})

    assert store.get_card_meta('card-0')['n'] == 0
    assert store.get_card_meta('card-119')['n'] == 119


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

# ------------------------------------------------------------------ 手动标签（审批页）


def test_manual_tags_normalize_input():
    """分隔符全认；去空白/去重/限长/限个数。"""
    assert store_module.normalize_manual_tags('塔罗、占卜师/塔罗，都市异能;预知')
    assert store_module.normalize_manual_tags('塔罗、占卜师/塔罗，都市异能;预知') == [
        '塔罗', '占卜师', '都市异能', '预知',
    ]
    assert store_module.normalize_manual_tags('  ') == []
    assert store_module.normalize_manual_tags(None) == []
    assert store_module.normalize_manual_tags('x' * 40) == [], '超长标签直接丢'
    many = ','.join(f't{i}' for i in range(50))
    assert len(store_module.normalize_manual_tags(many)) == store_module.MANUAL_TAGS_PER_ITEM


def test_manual_tags_set_and_mark_applied(store):
    store.record_run({'run_id': 'run-1', 'task': 'analyze'})

    saved = store.set_manual_tags('run-1', 'Alice.png', ['塔罗', '占卜师'])
    assert saved['tags'] == ['塔罗', '占卜师']
    assert store.manual_tags('run-1')['Alice.png']['tags'] == ['塔罗', '占卜师']

    assert store.mark_manual_applied('run-1', 'Alice.png', ['塔罗']) is True
    entry = store.manual_tags('run-1')['Alice.png']
    assert entry['applied'] == ['塔罗']

    # 整组替换时：移除的标签不再保留「已写」状态
    store.set_manual_tags('run-1', 'Alice.png', ['占卜师'])
    entry = store.manual_tags('run-1')['Alice.png']
    assert entry['tags'] == ['占卜师']
    assert entry['applied'] == [], '被移除的标签不该还记着已写入'


def test_manual_tags_follow_run_lifecycle(store):
    for index in range(3):
        store.record_run({'run_id': f'run-{index}', 'task': 'analyze', 'created_at': index})
        store.set_manual_tags(f'run-{index}', 'Alice.png', [f'tag{index}'])

    assert set(store.manual_tags('run-1')) == {'Alice.png'}
    store.forget_run('run-1')
    assert store.manual_tags('run-1') == {}, '作业被遗忘时，手动标签一起清掉'
    assert store.manual_tags('run-2'), '别的作业不受影响'


def test_manual_tags_are_pruned_with_ledger(store):
    # 先给「老作业」写手动标签，再灌满台账把它挤掉
    store.record_run({'run_id': 'run-0', 'task': 'analyze', 'created_at': 0})
    store.set_manual_tags('run-0', 'Alice.png', ['旧标签'])
    for index in range(1, 55):
        store.record_run({'run_id': f'run-{index}', 'task': 'analyze', 'created_at': index})

    ui_runs = store.read_ui().get('runs') or {}
    assert 'run-0' not in ui_runs, '台账只保留最近 50 条'
    assert store.manual_tags('run-0') == {}, '被台账裁掉的作业，手动标签一起清掉'

    store.set_manual_tags('run-54', 'Alice.png', ['塔罗'])
    assert store.manual_tags('run-54')['Alice.png']['tags'] == ['塔罗']

# ------------------------------------------------------------------ AI 打标历史记录


def test_history_record_and_group_by_local_date(store):
    now = int(store_module.time.time())
    store.record_history({
        'entry_id': 'e1', 'run_id': 'r1', 'item_key': 'a', 'card_id': 'a',
        'applied_at': now, 'tags': [{'name': '奇幻', 'confidence': 0.9}],
    })
    store.record_history({
        'entry_id': 'e2', 'run_id': 'r1', 'item_key': 'b', 'card_id': 'b',
        'applied_at': now - 86400,
    })

    dates = store.history_dates()
    assert len(dates) == 2
    assert dates[0]['count'] == 1
    today = store.history_entries(dates[0]['date'])
    assert [entry['entry_id'] for entry in today] == ['e1']
    assert today[0]['tags'][0]['name'] == '奇幻'


def test_history_upsert_delete_and_clear(store):
    now = int(store_module.time.time())
    day = store_module.history_day(now)
    store.record_history({'entry_id': 'e1', 'run_id': 'r1', 'item_key': 'a', 'applied_at': now})
    store.record_history({'entry_id': 'e1', 'run_id': 'r1', 'item_key': 'a', 'applied_at': now,
                          'summary': '更新过的概览'})

    entry = store.get_history_entry('e1')
    assert entry['summary'] == '更新过的概览', '同一条目重复落地应更新而不是重复'
    assert len(store.history_entries(day)) == 1

    store.record_history({'entry_id': 'e2', 'run_id': 'r1', 'item_key': 'b', 'applied_at': now})
    assert store.clear_history_date(day) == 2
    assert store.history_dates() == []


def test_history_update_entry(store):
    now = int(store_module.time.time())
    store.record_history({'entry_id': 'e1', 'run_id': 'r1', 'item_key': 'a',
                          'applied_at': now, 'tags': []})

    updated = store.update_history_entry('e1', {'tags': [{'name': '塔罗'}]})

    assert updated['tags'] == [{'name': '塔罗'}]
    assert store.get_history_entry('e1')['tags'] == [{'name': '塔罗'}]
    assert store.update_history_entry('missing', {'tags': []}) == {}
