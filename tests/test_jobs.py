"""作业处理器 `ai.analyze`：写回语义、幂等、失败隔离、取消与用量。

LLM 调用与概览 bridge 全部打桩，测试不产生真实网络请求、不依赖主程序。
"""

import json
import os

import pytest

from conftest import load_backend_submodule
from fake_api import FakeApi, FakeCtx

pytest.importorskip('flask', reason='装配层依赖 flask（主程序运行环境必备）')

jobs_module = load_backend_submodule('jobs')
store_module = load_backend_submodule('store')
bridge = load_backend_submodule('summary_bridge')
errors = load_backend_submodule('core.errors')
profiles_core = load_backend_submodule('core.profiles')

PluginStore = store_module.PluginStore
AiAssistantError = errors.AiAssistantError

CARD = {
    'name': 'Alice',
    'description': '一名北境骑士，性格沉稳。',
    'first_mes': '你好，旅行者。',
    'personality': '沉稳、寡言',
    'scenario': '北境城堡',
    'mes_example': '<START>\n{{user}}: 你好',
    'tags': ['奇幻'],
    'character_book': {'entries': [{'comment': '世界观', 'content': '北境常年积雪。'}]},
}

LLM_REPLY = json.dumps({
    'summary': '北境骑士，性格沉稳，适合冒险向剧情。',
    'tags': [{'name': '奇幻', 'confidence': 0.9}, {'name': '骑士', 'confidence': 0.8}],
    'suggested_new_tags': [{'name': '北境', 'reason': '标签库中不存在'}],
    'card_type': 'OC',
    'language': 'zh',
}, ensure_ascii=False)


@pytest.fixture
def api(tmp_path):
    return FakeApi(
        data_dir=str(tmp_path / 'data'),
        secrets={'api_key': 'sk-test', 'base_url': 'https://api.openai.com/v1'},
        cards={'Alice.png': dict(CARD), 'Bob.png': dict(CARD, name='Bob')},
        tag_library={
            'tags': ['奇幻', '冒险'],
            'taxonomy': {'tag_to_category': {'奇幻': '题材'}},
            'prefs': {},
        },
    )


@pytest.fixture
def store(api):
    return PluginStore(api)


@pytest.fixture
def stub_llm(monkeypatch):
    """打桩 LLM 调用，记录每次请求。"""
    calls = []

    def fake_chat(messages, **kwargs):
        calls.append({'messages': messages, **kwargs})
        return {
            'content': LLM_REPLY,
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20},
            'usage_estimated': False,
            'model': kwargs.get('model') or 'test-model',
            'latency_ms': 5,
            'attempts': 1,
        }

    monkeypatch.setattr(jobs_module.llm, 'chat', fake_chat)
    return calls


@pytest.fixture
def stub_bridge(monkeypatch):
    """打桩概览 bridge：默认成功写入主程序侧。"""
    written = {}

    def fake_write_summary(api_obj, store_obj, card_id, summary, *, ui_key=''):
        written[str(card_id)] = str(summary)
        return {'ok': True, 'origin': 'ui_data', 'ui_key': ui_key or card_id, 'message': ''}

    monkeypatch.setattr(jobs_module.summary_bridge, 'write_summary', fake_write_summary)
    monkeypatch.setattr(jobs_module.summary_bridge, 'resolve_key', lambda api_obj, card_id: str(card_id))
    return written


def make_items(card_ids=('Alice.png',), *, mode='both', content_hash='sha256:stub'):
    """构造与主程序一致的条目结构：`{key, payload, seq}`。

    `core/plugins/jobs.py:739` 的 `JobItem` 是 `{key, payload, seq}`——单项参数
    放在 `payload` 里，因此插件从 `item['payload']` 读取 `card_id` / `mode` 等。
    """
    return [
        {
            'key': card_id,
            'seq': index,
            'payload': {
                'card_id': card_id,
                'ui_key': card_id,
                'mode': mode,
                'content_hash': content_hash,
            },
        }
        for index, card_id in enumerate(card_ids)
    ]


def run_job(api, store, *, items=None, payload=None, ctx=None):
    items = items if items is not None else make_items()
    payload = payload or {}
    ctx = ctx or FakeCtx(items, payload)
    handler = api.jobs.get('ai.analyze') or jobs_module.run_analyze
    if handler is jobs_module.run_analyze:
        handler(ctx, api=api, store=store)
    else:
        handler(ctx)
    return ctx


def register(api, store):
    jobs_module.register_jobs(api, store)


# ------------------------------------------------------------------ 前置检查


def test_missing_api_key_aborts_job(api, store, stub_llm):
    api.secrets.set('api_key', None)
    ctx = FakeCtx(make_items(), {})
    with pytest.raises(AiAssistantError) as excinfo:
        jobs_module.run_analyze(ctx, api=api, store=store)

    assert excinfo.value.code == errors.CODE_SECRET_MISSING
    assert ctx.summary['aborted'] is True
    assert stub_llm == []


def test_unknown_provider_aborts_job(api, store, stub_llm):
    ctx = FakeCtx(make_items(), {'provider_id': 'nope'})
    with pytest.raises(AiAssistantError) as excinfo:
        jobs_module.run_analyze(ctx, api=api, store=store)
    assert excinfo.value.code == errors.CODE_PROVIDER_NOT_FOUND


def test_unknown_profile_aborts_job(api, store, stub_llm):
    ctx = FakeCtx(make_items(), {'profile_id': 'nope'})
    with pytest.raises(AiAssistantError) as excinfo:
        jobs_module.run_analyze(ctx, api=api, store=store)
    assert excinfo.value.code == errors.CODE_PROFILE_NOT_FOUND


# ------------------------------------------------------------------ auto 写回


def test_auto_mode_writes_summary_and_merges_tags(api, store, stub_llm, stub_bridge):
    ctx = run_job(api, store, payload={'apply_mode': 'auto'})

    assert stub_bridge['Alice.png'].startswith('北境骑士')
    assert api.applied_tags == [{'card_ids': ['Alice.png'], 'add': ['奇幻'], 'replace': False}]
    assert [report['status'] for report in ctx.reports] == ['done']

    result = ctx.reports[0]['result']
    assert result['applied'] is True
    assert result['summary_origin'] == 'ui_data'
    assert result['tags_applied'] == ['奇幻']
    # 「骑士」不在标签库中 → 只进建议队列，不写入卡片
    assert result['tags_suggested'] == ['北境', '骑士']
    assert result['new_tags'] == [
        {'name': '北境', 'reason': '标签库中不存在'},
        {'name': '骑士', 'reason': '标签库中不存在'},
    ]


def test_auto_mode_records_card_meta(api, store, stub_llm, stub_bridge):
    run_job(api, store, payload={'apply_mode': 'auto'})
    meta = store.get_card_meta('Alice.png')

    assert meta['content_hash'] == 'sha256:stub'
    assert meta['model']
    assert meta['usage'] == {'prompt_tokens': 100, 'completion_tokens': 20}
    assert meta['last_run_id']


def test_auto_mode_queues_new_tag_suggestions(api, store, stub_llm, stub_bridge):
    run_job(api, store, payload={'apply_mode': 'auto'})
    queue = store.list_tag_suggestions()
    assert '北境' in queue
    assert queue['北境']['cards'] == ['Alice.png']


def test_tag_library_governance_is_respected(api, store, stub_llm, stub_bridge):
    """AI 只能写入标签库里已有的标签；未知标签一律转为建议。"""
    api._tag_library = {'tags': [], 'taxonomy': {}, 'prefs': {}}
    ctx = run_job(api, store, payload={'apply_mode': 'auto'})

    assert api.applied_tags == []
    assert set(ctx.reports[0]['result']['tags_suggested']) == {'奇幻', '骑士', '北境'}


def test_summary_only_mode_skips_tags(api, store, stub_llm, stub_bridge):
    ctx = run_job(api, store, items=make_items_with_mode('summary'),
                  payload={'apply_mode': 'auto'})
    assert api.applied_tags == []
    assert stub_bridge['Alice.png']
    assert ctx.reports[0]['result']['applied'] is True


def test_tags_only_mode_skips_summary(api, store, stub_llm, stub_bridge):
    run_job(api, store, items=make_items_with_mode('tags'), payload={'apply_mode': 'auto'})
    assert 'Alice.png' not in stub_bridge
    assert api.applied_tags[0]['add'] == ['奇幻']


def make_items_with_mode(mode, card_ids=('Alice.png',)):
    return make_items(card_ids, mode=mode)


def test_apply_tags_disabled_by_payload(api, store, stub_llm, stub_bridge):
    ctx = run_job(api, store, payload={'apply_mode': 'auto', 'apply_tags': False})
    assert api.applied_tags == []
    assert any('跳过标签写入' in item for item in ctx.reports[0]['result']['warnings'])


def test_bridge_failure_degrades_to_namespace(api, store, stub_llm, monkeypatch):
    monkeypatch.setattr(jobs_module.summary_bridge, 'write_summary', lambda *a, **k: {
        'ok': False, 'origin': 'namespace', 'ui_key': 'Alice.png', 'message': '主程序拒绝写入',
    })
    monkeypatch.setattr(jobs_module.summary_bridge, 'resolve_key', lambda api_obj, card_id: str(card_id))

    ctx = run_job(api, store, payload={'apply_mode': 'auto'})
    result = ctx.reports[0]['result']

    assert result['summary_origin'] == 'namespace'
    # 降级原因现在由 apply_actions 的执行记录给出（不再由 jobs 手写文案）
    assert any('主程序拒绝写入' in item for item in result['warnings'])
    note = [a for a in result['actions'] if a['action'] == 'write_card_note'][0]
    assert note['ok'] is False
    assert note['origin'] == 'namespace'


# ------------------------------------------------------------------ review / dry-run


def test_review_mode_defers_writeback(api, store, stub_llm, stub_bridge):
    ctx = run_job(api, store, payload={'apply_mode': 'review'})

    assert [report['status'] for report in ctx.reports] == ['awaiting_review']
    assert stub_bridge == {}, 'review 模式不得写盘'
    assert api.applied_tags == []
    assert ctx.summary['awaiting_review'] == 1


def test_review_mode_does_not_record_card_meta(api, store, stub_llm, stub_bridge):
    """待审批的卡片不能被记为「已分析」，否则会被幂等判定误跳过。"""
    run_job(api, store, payload={'apply_mode': 'review'})
    assert store.get_card_meta('Alice.png') == {}


def test_review_result_carries_proposals(api, store, stub_llm, stub_bridge):
    ctx = run_job(api, store, payload={'apply_mode': 'review'})
    result = ctx.reports[0]['result']

    assert result['summary'].startswith('北境骑士')
    assert result['tags'][0]['name'] == '奇幻'
    assert result['new_tags'][0]['name'] == '北境'
    assert result['applied'] is False


def test_dry_run_finishes_without_writing(api, store, stub_llm, stub_bridge):
    ctx = run_job(api, store, payload={'apply_mode': 'auto', 'dry_run': True})

    assert [report['status'] for report in ctx.reports] == ['done']
    assert ctx.reports[0]['result']['dry_run'] is True
    assert stub_bridge == {}
    assert api.applied_tags == []


# ------------------------------------------------------------------ 失败隔离


def test_unreadable_card_is_reported_as_failure(api, store, stub_llm, stub_bridge):
    ctx = run_job(api, store, items=make_items(('Missing.png',)))

    assert ctx.reports[0]['status'] == 'failed'
    assert errors.CODE_CARD_UNREADABLE in ctx.reports[0]['error']


def test_llm_failure_marks_only_that_item(api, store, monkeypatch, stub_bridge):
    def failing_chat(messages, **kwargs):
        raise AiAssistantError(errors.CODE_LLM_TIMEOUT, '超时', http_status=504)

    monkeypatch.setattr(jobs_module.llm, 'chat', failing_chat)
    ctx = run_job(api, store, items=make_items(('Alice.png', 'Bob.png')))

    assert [report['status'] for report in ctx.reports] == ['failed', 'failed']
    assert ctx.summary['failed'] == 2


def test_unparseable_output_is_reported(api, store, monkeypatch, stub_bridge):
    monkeypatch.setattr(jobs_module.llm, 'chat', lambda messages, **kwargs: {
        'content': '抱歉，我无法完成这个任务。',
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5},
        'usage_estimated': False, 'model': 'm', 'latency_ms': 1, 'attempts': 1,
    })
    ctx = run_job(api, store)

    assert ctx.reports[0]['status'] == 'failed'
    assert errors.CODE_PARSE_FAILED in ctx.reports[0]['error']
    assert ctx.reports[0]['result']['detail']['excerpt']


def _schema_with_min_summary(min_chars: int) -> dict:
    """构造一份「概览至少 N 字」的 output_schema（v2）。"""
    base = profiles_core.DEFAULT_OUTPUT_SCHEMA
    schema = json.loads(json.dumps(base))
    schema['properties']['summary']['min_chars'] = min_chars
    return schema


def test_strict_validation_failure_is_reported(api, store, monkeypatch, stub_bridge):
    """strict 模式下，概览短于 schema 下限 → 判失败（不挽救）。"""
    store.save_profile(dict(
        profiles_core.builtin_profile('builtin-default'),
        id='strict-profile', name='strict',
        output_schema=_schema_with_min_summary(100),
    ))
    monkeypatch.setattr(jobs_module.llm, 'chat', lambda messages, **kwargs: {
        'content': '{"summary": "太短", "tags": []}',
        'usage': {}, 'usage_estimated': True, 'model': 'm', 'latency_ms': 1, 'attempts': 1,
    })
    ctx = run_job(api, store, payload={'strict': True, 'apply_mode': 'auto',
                                       'profile_id': 'strict-profile'})

    assert ctx.reports[0]['status'] == 'failed'
    assert errors.CODE_VALIDATION_FAILED in ctx.reports[0]['error']


def test_non_strict_mode_salvages_short_summary(api, store, monkeypatch, stub_bridge):
    store.save_profile(dict(
        profiles_core.builtin_profile('builtin-default'),
        id='lenient-profile', name='lenient',
        output_schema=_schema_with_min_summary(100),
    ))
    monkeypatch.setattr(jobs_module.llm, 'chat', lambda messages, **kwargs: {
        'content': '{"summary": "太短", "tags": []}',
        'usage': {}, 'usage_estimated': True, 'model': 'm', 'latency_ms': 1, 'attempts': 1,
    })
    ctx = run_job(api, store, payload={'strict': False, 'apply_mode': 'auto',
                                       'profile_id': 'lenient-profile'})

    assert ctx.reports[0]['status'] == 'done'
    assert any('短于下限' in item for item in ctx.reports[0]['result']['warnings'])


def test_consecutive_fatal_failures_abort_job(api, store, monkeypatch, stub_bridge):
    def failing_chat(messages, **kwargs):
        raise AiAssistantError(errors.CODE_LLM_AUTH_FAILED, '认证失败', http_status=401)

    monkeypatch.setattr(jobs_module.llm, 'chat', failing_chat)
    card_ids = tuple(f'card-{index}.png' for index in range(10))
    items = make_items(card_ids)
    api._cards.update({card_id: dict(CARD) for card_id in card_ids})

    ctx = run_job(api, store, items=items)

    assert len(ctx.reports) == jobs_module.ABORT_AFTER_CONSECUTIVE_FAILURES
    assert any('已中止作业' in message for _level, message in ctx.logs)


def test_internal_exception_does_not_stop_other_items(api, store, monkeypatch, stub_bridge):
    calls = {'count': 0}

    def flaky_chat(messages, **kwargs):
        calls['count'] += 1
        if calls['count'] == 1:
            raise RuntimeError('boom')
        return {
            'content': LLM_REPLY, 'usage': {'prompt_tokens': 1, 'completion_tokens': 1},
            'usage_estimated': False, 'model': 'm', 'latency_ms': 1, 'attempts': 1,
        }

    monkeypatch.setattr(jobs_module.llm, 'chat', flaky_chat)
    ctx = run_job(api, store, items=make_items(('Alice.png', 'Bob.png')),
                  payload={'apply_mode': 'auto'})

    assert [report['status'] for report in ctx.reports] == ['failed', 'done']


# ------------------------------------------------------------------ 取消


def test_cancel_stops_before_next_item(api, store, stub_llm, stub_bridge):
    items = make_items(('Alice.png', 'Bob.png'))
    ctx = FakeCtx(items, {'apply_mode': 'auto'})

    original_report = ctx.report

    def report_and_cancel(item_key, **kwargs):
        original_report(item_key, **kwargs)
        ctx.cancel()

    ctx.report = report_and_cancel
    jobs_module.run_analyze(ctx, api=api, store=store)

    assert len(ctx.reports) == 1
    assert any('取消' in message or '暂停' in message for _level, message in ctx.logs)


# ------------------------------------------------------------------ 摘要与用量


def test_summary_and_usage_are_recorded(api, store, stub_llm, stub_bridge):
    ctx = run_job(api, store, items=make_items(('Alice.png', 'Bob.png')),
                  payload={'apply_mode': 'auto'})

    assert ctx.summary['items'] == 2
    assert ctx.summary['applied'] == 2
    assert ctx.summary['prompt_tokens'] == 200
    assert ctx.summary['completion_tokens'] == 40

    ledger = store.usage_ledger()
    assert ledger['total_prompt_tokens'] == 200
    assert ledger['jobs'] == 1


def test_usage_estimated_is_surfaced_as_warning(api, store, monkeypatch, stub_bridge):
    monkeypatch.setattr(jobs_module.llm, 'chat', lambda messages, **kwargs: {
        'content': LLM_REPLY, 'usage': {'prompt_tokens': 0, 'completion_tokens': 0},
        'usage_estimated': True, 'model': 'm', 'latency_ms': 1, 'attempts': 1,
    })
    ctx = run_job(api, store, payload={'apply_mode': 'auto'})
    assert any('估算' in item for item in ctx.reports[0]['result']['warnings'])


def test_missing_required_variable_fails_item(api, store, stub_llm, stub_bridge):
    """卡片缺少 name 时，必填变量 `card.name` 缺席 → 该条目失败（不静默产出空分析）。"""
    store.save_profile(dict(profiles_core.builtin_profile('builtin-default'),
                            id='needs-name', name='n',
                            user_template='{{ card.name }} {{ model.profile_id }}'))
    api._cards['Nameless.png'] = {'description': '没有名字'}
    ctx = run_job(api, store, items=make_items(('Nameless.png',)),
                  payload={'profile_id': 'needs-name', 'apply_mode': 'auto'})

    assert ctx.reports[0]['status'] == 'failed'
    assert errors.CODE_MISSING_VARIABLE in ctx.reports[0]['error']
    assert stub_llm == [], '缺变量时不应发起 LLM 调用（避免浪费配额）'


# ------------------------------------------------------------------ 注册


def test_register_jobs_registers_declared_type(api, store):
    register(api, store)
    assert 'ai.analyze' in api.registered_jobs


# ------------------------------------------------------------------ 辅助函数


def test_split_tags_separates_known_and_new():
    accepted, suggested, blocked = jobs_module.split_tags(
        [{'name': '奇幻'}, {'name': '北境'}], {'奇幻'})
    assert accepted == ['奇幻']
    assert suggested == [{'name': '北境', 'reason': '标签库中不存在'}]
    assert blocked == []


def test_split_tags_hard_filters_by_blocked_set():
    """`blocked` 给了就是硬过滤：隔离分类的标签即使 AI 输出也进不了 accepted。

    注意：只有**明确被禁**的标签才挡；陌生标签仍走「新标签建议」。
    """
    accepted, suggested, blocked = jobs_module.split_tags(
        [{'name': '奇幻'}, {'name': 'R18'}, {'name': '北境'}],
        {'奇幻', 'R18'}, blocked={'R18'},
    )
    assert accepted == ['奇幻']
    assert blocked == ['R18'], '被挡下的要如实报出来'
    assert suggested == [{'name': '北境', 'reason': '标签库中不存在'}], (
        '陌生标签不能被硬过滤吃掉，它该进建议队列'
    )


def test_merge_suggestions_dedupes():
    merged = jobs_module.merge_suggestions(
        [{'name': 'A', 'reason': 'r1'}], [{'name': 'A', 'reason': 'r2'}, {'name': 'B'}])
    assert [item['name'] for item in merged] == ['A', 'B']


def test_known_tag_set_includes_taxonomy_tags():
    library = {'tags': ['A'], 'taxonomy': {'tag_to_category': {'B': 'x'},
                                           'category_tag_order': {'x': ['C']}}}
    assert jobs_module.known_tag_set(library) == {'A', 'B', 'C'}


def test_embedded_entries_supports_both_structures():
    assert jobs_module._embedded_entries(CARD) == CARD['character_book']['entries']
    assert jobs_module._embedded_entries({'character_book': CARD['character_book']['entries']}) \
        == CARD['character_book']['entries']
    assert jobs_module._embedded_entries({}) == []
