"""Agent 工具循环（阶段二）：动作协议、读取门禁、上下文压缩、循环本身。

**为什么需要它**：卡片太多，一次读不完。阶段一只采样 30 张就下结论，
样本之外完全没看过；这里让模型**自己决定看哪些**——先看全库目录（便宜），
再按需分批读细节（贵），最后提议。

设计对照 shujuku 的 Agent 实现（33 文件 / 73 万字符），只保留它真正的四条机制：
轮次上限、读取配额、读取门禁、上下文压缩。其余（子代理/派工/账本/事务）
是世界推演业务特有的，不复用。
"""

import json

import pytest

from conftest import load_backend_submodule

pytest.importorskip('flask', reason='装配层依赖 flask')

agent = load_backend_submodule('core.agent')
profiles = load_backend_submodule('core.profiles')
jobs = load_backend_submodule('jobs')

from fake_api import FakeApi, FakeCtx  # noqa: E402


# ------------------------------------------------------------------ 动作协议


def test_read_action_parses():
    action = agent.parse_action({'action': 'read', 'ids': ['a.png', 'b.png']})
    assert action['kind'] == agent.ACTION_READ
    assert action['ids'] == ['a.png', 'b.png']
    assert action['detail'] == agent.DETAIL_SUMMARY


def test_read_action_accepts_detail():
    action = agent.parse_action(
        {'action': 'read', 'ids': ['a.png'], 'detail': 'full'},
    )
    assert action['detail'] == agent.DETAIL_FULL


def test_read_action_rejects_empty_ids():
    with pytest.raises(agent.ProtocolError) as excinfo:
        agent.parse_action({'action': 'read', 'ids': []})
    assert excinfo.value.reason_code == 'IDS_REQUIRED'


def test_read_action_rejects_too_many_ids():
    """一次要 200 张会让请求直接超长。"""
    with pytest.raises(agent.ProtocolError) as excinfo:
        agent.parse_action({
            'action': 'read',
            'ids': [f'c{i}.png' for i in range(agent.MAX_IDS_PER_READ + 1)],
        })
    assert excinfo.value.reason_code == 'TOO_MANY_IDS'


def test_read_action_rejects_bad_detail():
    with pytest.raises(agent.ProtocolError) as excinfo:
        agent.parse_action({'action': 'read', 'ids': ['a'], 'detail': 'huge'})
    assert excinfo.value.reason_code == 'INVALID_DETAIL'


def test_search_action_parses():
    action = agent.parse_action({'action': 'search', 'query': '奇幻'})
    assert action['kind'] == agent.ACTION_SEARCH
    assert action['limit'] == 10


def test_search_action_caps_limit():
    action = agent.parse_action({'action': 'search', 'query': 'x', 'limit': 999})
    assert action['limit'] == agent.MAX_SEARCH_RESULTS


def test_finalize_action_parses():
    action = agent.parse_action({
        'action': 'finalize',
        'categories': [{'name': '奇幻'}],
        'rationale': '按题材',
    })
    assert action['kind'] == agent.ACTION_FINALIZE
    assert action['proposal']['categories'] == [{'name': '奇幻'}]


def test_finalize_requires_categories():
    with pytest.raises(agent.ProtocolError) as excinfo:
        agent.parse_action({'action': 'finalize'})
    assert excinfo.value.reason_code == 'CATEGORIES_REQUIRED'


def test_unknown_action_is_rejected():
    with pytest.raises(agent.ProtocolError) as excinfo:
        agent.parse_action({'action': 'delete_everything'})
    assert excinfo.value.reason_code == 'INVALID_ACTION'


def test_extra_fields_are_rejected():
    """静默忽略多余字段会让模型以为它有效，下一轮继续写。"""
    with pytest.raises(agent.ProtocolError) as excinfo:
        agent.parse_action({'action': 'read', 'ids': ['a'], 'force': True})
    assert excinfo.value.reason_code == 'UNKNOWN_FIELD'


def test_non_object_is_rejected():
    with pytest.raises(agent.ProtocolError) as excinfo:
        agent.parse_action(['read'])
    assert excinfo.value.reason_code == 'OBJECT_REQUIRED'


# ------------------------------------------------------------------ JSON 提取


def test_extract_json_plain():
    assert agent.extract_json('{"action": "read"}') == {'action': 'read'}


def test_extract_json_from_fence():
    assert agent.extract_json('```json\n{"a": 1}\n```') == {'a': 1}


def test_extract_json_with_surrounding_prose():
    assert agent.extract_json('好的，我来读一下：\n{"a": 1}\n以上。') == {'a': 1}


def test_extract_json_empty_raises():
    with pytest.raises(agent.ProtocolError) as excinfo:
        agent.extract_json('')
    assert excinfo.value.reason_code == 'EMPTY_OUTPUT'


def test_extract_json_garbage_raises():
    with pytest.raises(agent.ProtocolError) as excinfo:
        agent.extract_json('完全不是 JSON')
    assert excinfo.value.reason_code == 'JSON_PARSE_FAILED'


# ------------------------------------------------------------------ 协议修正预算


def test_repair_state_allows_first_retry():
    state = agent.RepairState(2)
    outcome = state.record(agent.ProtocolError('INVALID_ACTION', '$.action', 'read'))
    assert outcome['retry'] is True


def test_repair_state_blocks_same_error_twice():
    """同一个错误连续犯两次，多半是提示词或通道的问题，再重试只是白烧 token。"""
    state = agent.RepairState(5)
    error = agent.ProtocolError('INVALID_ACTION', '$.action', 'read')
    state.record(error)
    second = state.record(error)
    assert second['retry'] is False


def test_repair_state_exhausts_after_max_attempts():
    state = agent.RepairState(1)
    state.record(agent.ProtocolError('A', '$', 'x'))
    outcome = state.record(agent.ProtocolError('B', '$', 'y'))
    assert outcome['retry'] is False


def test_render_rejection_tells_the_model_what_to_do():
    text = agent.render_rejection(agent.ProtocolError('INVALID_ACTION', '$.action', 'a | b'))
    assert 'INVALID_ACTION' in text
    assert 'read' in text and 'search' in text and 'finalize' in text


# ------------------------------------------------------------------ 读取门禁


def test_gate_allows_small_batch():
    state = {'granted_tokens': 0}
    items = [{'card_id': 'a', 'label': 'A', 'tokens': 100}]
    gate = agent.gate_read_batch(items, state, budget={'context_budget': 40000})
    assert gate['allowed'] is True
    assert gate['batch_tokens'] == 100


def test_gate_rejects_when_batch_exceeds_total_budget():
    """累计上限防止模型化整为零、一小批一小批地读光整个卡片库。"""
    budget = agent.normalize_budget({'read_batch_tokens': 1000, 'max_iterations': 2})
    state = {'granted_tokens': budget['read_batch_tokens'] * 2 - 10}
    gate = agent.gate_read_batch(
        [{'card_id': 'a', 'label': 'A', 'tokens': 500}], state, budget=budget,
    )
    assert gate['allowed'] is False
    assert gate['reason'] == 'read-batch-too-large'


def test_gate_allows_small_batch_near_context_limit():
    """上下文快满时，小批量仍放行（不能因为快满就完全不让读）。"""
    budget = agent.normalize_budget({'context_budget': 10000})
    state = {'granted_tokens': 0}
    gate = agent.gate_read_batch(
        [{'card_id': 'a', 'label': 'A', 'tokens': 200}], state, budget=budget,
        context_tokens=9900,
    )
    assert gate['allowed'] is True


def test_gate_rejects_large_batch_near_context_limit():
    budget = agent.normalize_budget({'context_budget': 10000})
    state = {'granted_tokens': 0}
    gate = agent.gate_read_batch(
        [{'card_id': 'a', 'label': 'A', 'tokens': 5000}], state, budget=budget,
        context_tokens=9900,
    )
    assert gate['allowed'] is False
    assert gate['reason'] == 'near-context-overflow'


def test_gate_report_names_the_offending_items():
    """静默截断会让模型以为自己读到了，必须回灌具体原因。"""
    budget = agent.normalize_budget({'read_batch_tokens': 500, 'max_iterations': 1})
    gate = agent.gate_read_batch(
        [{'card_id': 'a.png', 'label': '爱丽丝', 'tokens': 5000}],
        {'granted_tokens': 0}, budget=budget,
    )
    assert gate['allowed'] is False
    assert '爱丽丝' in gate['report']
    assert '本批正文未注入' in gate['report']


def test_gate_allows_empty_batch():
    gate = agent.gate_read_batch([], {'granted_tokens': 0})
    assert gate['allowed'] is True


# ------------------------------------------------------------------ 上下文压缩


def test_compact_keeps_recent_when_under_budget():
    transcript = [{'role': 'user', 'content': 'x' * 10}]
    result = agent.compact_transcript(transcript, {'context_budget': 40000})
    assert result['changed'] is False
    assert len(result['messages']) == 1


def test_compact_drops_oldest_pairs():
    transcript = []
    for index in range(10):
        transcript.append({'role': 'assistant', 'content': f'action {index}'})
        transcript.append({'role': 'user', 'content': 'x' * 2000})

    result = agent.compact_transcript(transcript, {'context_budget': 5000})
    assert result['changed'] is True
    assert result['dropped'] > 0
    # 最近一组必须保留
    assert 'action 9' in result['messages'][-2]['content']


def test_compact_never_drops_the_last_group():
    transcript = [
        {'role': 'assistant', 'content': 'a'},
        {'role': 'user', 'content': 'x' * 100000},
    ]
    result = agent.compact_transcript(transcript, {'context_budget': 1000})
    assert len(result['messages']) >= 2
    assert result['messages'][-1]['content']


def test_compact_inserts_replay_hint():
    """丢弃的是可重放的读取结果，所以只需要告诉模型「可以重新 read」。"""
    transcript = []
    for index in range(10):
        transcript.append({'role': 'assistant', 'content': f'action {index}'})
        transcript.append({'role': 'user', 'content': 'x' * 2000})

    result = agent.compact_transcript(transcript, {'context_budget': 5000})
    assert '重新 read' in result['messages'][0]['content']


def test_compact_handles_empty_transcript():
    result = agent.compact_transcript([], {'context_budget': 1000})
    assert result['changed'] is False


def test_compact_handles_garbage():
    result = agent.compact_transcript('not a list', {'context_budget': 1000})
    assert result['changed'] is False


# ------------------------------------------------------------------ 目录


def _row(card_id, category='', tags=None, name=None):
    return {
        'card_id': card_id,
        'category': category,
        'tags': list(tags or []),
        'brief': {'name': name or card_id, 'tags': list(tags or []), 'excerpt': ''},
    }


def test_catalog_covers_all_cards_when_cheap():
    rows = [_row(f'c{i}.png', category='奇幻', tags=['魔法']) for i in range(50)]
    catalog = agent.build_catalog(rows, budget={'catalog_tokens': 40000})
    assert catalog['stats']['total'] == 50
    assert catalog['stats']['listed'] == 50
    assert catalog['stats']['truncated'] is False


def test_catalog_truncates_and_reports():
    rows = [_row(f'很长很长的卡片名字{c}'.join(['x'] * 8), category='奇幻')
            for c in range(200)]
    catalog = agent.build_catalog(rows, budget={'catalog_tokens': 500})
    assert catalog['stats']['truncated'] is True
    assert catalog['stats']['listed'] < 200
    assert catalog['stats']['total'] == 200


def test_catalog_keeps_statistics_even_when_truncated():
    """被截断时必须让模型知道整体形状（分类计数、标签频次）。"""
    rows = [_row(f'c{i}.png', category='奇幻' if i % 2 else '科幻', tags=['魔法'])
            for i in range(100)]
    catalog = agent.build_catalog(rows, budget={'catalog_tokens': 200})
    assert catalog['stats']['category_counts']['奇幻'] == 50
    assert catalog['stats']['category_counts']['科幻'] == 50


def test_format_catalog_marks_truncation():
    rows = [_row('x' * 500 + str(i), category='奇幻') for i in range(100)]
    catalog = agent.build_catalog(rows, budget={'catalog_tokens': 200})
    text = agent.format_catalog(catalog)
    assert '截断' in text
    assert 'search' in text


def test_format_catalog_empty_returns_empty_string():
    assert agent.format_catalog(agent.build_catalog([])) == ''


def test_search_matches_name_tag_and_category():
    rows = [
        _row('a.png', name='爱丽丝', tags=['魔法']),
        _row('b.png', category='科幻'),
    ]
    assert [r['card_id'] for r in agent.search_catalog(rows, '爱丽丝')] == ['a.png']
    assert [r['card_id'] for r in agent.search_catalog(rows, '魔法')] == ['a.png']
    assert [r['card_id'] for r in agent.search_catalog(rows, '科幻')] == ['b.png']


def test_search_respects_limit():
    rows = [_row(f'c{i}.png', name='魔法师') for i in range(50)]
    assert len(agent.search_catalog(rows, '魔法', limit=5)) == 5


def test_search_empty_query_returns_nothing():
    assert agent.search_catalog([_row('a.png')], '') == []


# ------------------------------------------------------------------ 任务规格


def test_builtin_agent_profile_is_valid():
    profile = profiles.builtin_profile('builtin-agent')
    assert profile is not None
    assert profiles.validate_profile(profile) == []


def test_agent_profile_does_not_hijack_model_matching():
    assert profiles.builtin_profile('builtin-agent')['model_match'] == ''
    assert profiles.select_profile(
        profiles.builtin_profiles(), 'some-model')['id'] == 'builtin-default'


# ------------------------------------------------------------------ 循环


@pytest.fixture
def env(tmp_path):
    store_module = load_backend_submodule('store')
    cards = {
        f'c{i}.png': {'data': {
            'name': f'卡{i}',
            'tags': ['奇幻'] if i % 2 else ['科幻'],
            'description': f'第 {i} 张卡的简介',
        }}
        for i in range(8)
    }
    api = FakeApi(
        data_dir=str(tmp_path / 'data'), secrets={'api_key': 'sk-test'},
        cards=cards,
        tag_library={'tags': ['奇幻'], 'taxonomy': {}, 'prefs': {}},
    )
    api.cards.list = lambda **kwargs: {
        'items': [
            {'id': cid, 'char_name': data['data']['name'],
             'tags': data['data']['tags'], 'category': ''}
            for cid, data in cards.items()
        ],
        'total': len(cards), 'index_ready': True,
    }
    api.cards.list_categories = lambda: []
    return api, store_module.PluginStore(api)


def _reply(payload):
    return {'content': json.dumps(payload, ensure_ascii=False), 'usage': {}}


FINAL = {
    'action': 'finalize',
    'categories': [
        {'name': '题材/奇幻', 'description': '剑与魔法', 'suggested_tags': ['魔法'],
         'card_count_estimate': 4},
        {'name': '题材/科幻', 'description': '未来科技', 'suggested_tags': ['太空'],
         'card_count_estimate': 4},
    ],
    'tag_merges': [],
    'rationale': '按题材',
}


def test_agent_loop_reads_then_finalizes(env, monkeypatch):
    api, store = env
    replies = [
        _reply({'action': 'read', 'ids': ['c0.png', 'c1.png']}),
        _reply(FINAL),
    ]
    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: replies.pop(0))

    ctx = FakeCtx([{'key': 'agent', 'payload': {}}], payload={
        'provider_id': 'openai', 'budget': {'max_iterations': 5},
    })
    jobs.run_agent(ctx, api=api, store=store)

    assert ctx.summary['awaiting_review'] == 1
    assert ctx.summary['iterations'] == 2
    assert ctx.summary['cards_read'] == 2
    assert ctx.summary['categories'] == 2
    assert ctx.reports[0]['result']['agent_meta']['cards_read'] == 2


def test_agent_loop_can_finalize_without_reading(env, monkeypatch):
    api, store = env
    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: _reply(FINAL))

    ctx = FakeCtx([{'key': 'agent', 'payload': {}}], payload={'provider_id': 'openai'})
    jobs.run_agent(ctx, api=api, store=store)

    assert ctx.summary['iterations'] == 1
    assert ctx.summary['cards_read'] == 0


def test_agent_loop_rejects_unsafe_categories(env, monkeypatch):
    api, store = env
    reply = {
        'action': 'finalize',
        'categories': [{'name': '../etc'}, {'name': '合法'}],
        'tag_merges': [],
    }
    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: _reply(reply))

    ctx = FakeCtx([{'key': 'agent', 'payload': {}}], payload={'provider_id': 'openai'})
    jobs.run_agent(ctx, api=api, store=store)

    result = ctx.reports[0]['result']
    assert [c['path'] for c in result['categories']] == ['合法']
    assert any('丢弃' in w for w in result['warnings'])


def test_agent_loop_retries_protocol_error(env, monkeypatch):
    """协议错误要回灌具体原因让模型自己纠正，而不是直接失败。"""
    api, store = env
    replies = [
        _reply({'action': 'explode'}),
        _reply(FINAL),
    ]
    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: replies.pop(0))

    ctx = FakeCtx([{'key': 'agent', 'payload': {}}], payload={'provider_id': 'openai'})
    jobs.run_agent(ctx, api=api, store=store)

    assert ctx.summary['awaiting_review'] == 1
    assert any('协议未通过' in str(line) for line in (ctx.logs or []))


def test_agent_loop_gives_up_after_persistent_protocol_error(env, monkeypatch):
    api, store = env
    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: _reply({'action': 'nope'}))

    ctx = FakeCtx([{'key': 'agent', 'payload': {}}], payload={'provider_id': 'openai'})
    with pytest.raises(Exception):
        jobs.run_agent(ctx, api=api, store=store)

    assert ctx.summary['failed'] == 1


def test_agent_loop_respects_max_iterations(env, monkeypatch):
    """轮次上限必须有 —— 否则模型可以无限读下去。"""
    api, store = env
    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: _reply(
        {'action': 'search', 'query': '卡'},
    ))

    ctx = FakeCtx([{'key': 'agent', 'payload': {}}], payload={
        'provider_id': 'openai', 'budget': {'max_iterations': 3},
    })
    jobs.run_agent(ctx, api=api, store=store)

    assert ctx.summary['failed'] == 1
    assert ctx.reports[0]['error'] == 'no_finalize'
    assert ctx.reports[0]['result']['iterations'] == 3


def test_agent_loop_enforces_read_quota(env, monkeypatch):
    """读取配额用尽后要拒绝并提示 finalize，而不是继续读。"""
    api, store = env
    replies = [
        _reply({'action': 'read', 'ids': ['c0.png', 'c1.png']}),
        _reply({'action': 'read', 'ids': ['c2.png', 'c3.png']}),
        _reply(FINAL),
    ]
    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: replies.pop(0))

    ctx = FakeCtx([{'key': 'agent', 'payload': {}}], payload={
        'provider_id': 'openai',
        'budget': {'max_iterations': 5, 'max_reads': 2},
    })
    jobs.run_agent(ctx, api=api, store=store)

    assert ctx.summary['cards_read'] == 2, '超出配额的部分不能读'
    assert ctx.summary['awaiting_review'] == 1


def test_agent_loop_reports_empty_library(env, monkeypatch):
    api, store = env
    api.cards.list = lambda **kwargs: {'items': [], 'total': 0, 'index_ready': True}
    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: _reply(FINAL))

    ctx = FakeCtx([{'key': 'agent', 'payload': {}}], payload={'provider_id': 'openai'})
    jobs.run_agent(ctx, api=api, store=store)

    assert ctx.summary['failed'] == 1
    assert ctx.reports[0]['error'] == 'empty_library'


def test_agent_loop_does_not_touch_cards(env, monkeypatch):
    """Agent 是只读的：读卡、提议，不写卡片。"""
    api, store = env
    writes = []
    api.cards.apply_tags = lambda *a, **k: writes.append('tags') or {}
    api.cards.write_fields = lambda *a, **k: writes.append('fields') or True
    api.cards.move = lambda *a, **k: writes.append('move') or {'count': 0}

    replies = [_reply({'action': 'read', 'ids': ['c0.png']}), _reply(FINAL)]
    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: replies.pop(0))

    ctx = FakeCtx([{'key': 'agent', 'payload': {}}], payload={'provider_id': 'openai'})
    jobs.run_agent(ctx, api=api, store=store)

    assert writes == [], 'Agent 运行期间不得写卡片'


def test_agent_meta_is_reported(env, monkeypatch):
    """用户需要知道方案基于多少样本、跑了几轮。"""
    api, store = env
    replies = [_reply({'action': 'read', 'ids': ['c0.png']}), _reply(FINAL)]
    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: replies.pop(0))

    ctx = FakeCtx([{'key': 'agent', 'payload': {}}], payload={'provider_id': 'openai'})
    jobs.run_agent(ctx, api=api, store=store)

    meta = ctx.reports[0]['result']['agent_meta']
    assert meta['iterations'] == 2
    assert meta['cards_read'] == 1
    assert meta['catalog_total'] == 8
    assert meta['catalog_listed'] == 8


def test_manifest_declares_agent_job():
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'manifest.json'), encoding='utf-8') as handle:
        manifest = json.load(handle)
    assert 'ai.agent' in manifest['backend']['jobs']
