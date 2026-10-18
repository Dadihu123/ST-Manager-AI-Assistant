"""应用动作执行器（`backend/actions.py`）。

这是 task spec 的「怎么落地」那一半。测试重点是：
**动作是可声明的、单动作失败不拖垮其余、未知动作被如实报告而不是静默丢弃。**
"""

import pytest

from conftest import load_backend_submodule

pytest.importorskip('flask', reason='装配层依赖 flask')
actions = load_backend_submodule('actions')
errors = load_backend_submodule('core.errors')

from fake_api import FakeApi  # noqa: E402


@pytest.fixture
def api(tmp_path):
    return FakeApi(data_dir=str(tmp_path))


@pytest.fixture
def store(api):
    store_module = load_backend_submodule('store')
    return store_module.PluginStore(api)


def run(action_list, result, api, store, card_id='Alice.png', context=None):
    return actions.run_actions(
        action_list, result, api=api, store=store, card_id=card_id, context=context,
    )


# ------------------------------------------------------------------ 单独动作


def test_write_card_note_writes_note(api, store):
    outcome = run([{'from': 'summary', 'action': 'write_card_note'}],
                  {'summary': '一段概览'}, api, store)

    assert outcome['applied'] is True
    assert api._notes['Alice.png'] == '一段概览'


def test_write_card_note_skips_empty_value(api, store):
    outcome = run([{'from': 'summary', 'action': 'write_card_note'}],
                  {'summary': '   '}, api, store)

    assert outcome['applied'] is False
    assert outcome['actions'][0]['skipped'] == 'empty'
    assert 'Alice.png' not in api._notes


def test_apply_tags_handles_string_list(api, store):
    """`accepted_tags` 可能是字符串列表（已筛过已知标签），不能假定是 dict。"""
    outcome = run(
        [{'from': 'tags', 'action': 'apply_tags'}],
        {'tags': [{'name': '奇幻'}, {'name': '法师'}]},
        api, store,
        context={'accepted_tags': ['奇幻', '法师']},
    )

    assert outcome['applied'] is True
    assert api.applied_tags[0]['add'] == ['奇幻', '法师']


def test_apply_tags_surfaces_blacklist_skip(api, store):
    original = api.cards.apply_tags

    def fake_apply(card_ids, *, add=(), remove=(), replace=False):
        return {'updated': 1, 'added': list(add), 'removed': [], 'failed': [],
                'skipped_blacklist': ['NSFW'], 'skipped_unknown': []}

    api.cards.apply_tags = fake_apply
    try:
        outcome = run([{'from': 'tags', 'action': 'apply_tags'}],
                      {'tags': []}, api, store, context={'accepted_tags': ['NSFW']})
    finally:
        api.cards.apply_tags = original

    assert any('黑名单' in item for item in outcome['warnings'])


def test_apply_tags_skips_empty(api, store):
    outcome = run([{'from': 'tags', 'action': 'apply_tags'}],
                  {'tags': []}, api, store, context={'accepted_tags': []})
    assert outcome['applied'] is False
    assert outcome['actions'][0]['skipped'] == 'empty'


def test_queue_suggestions_records_into_store(api, store):
    outcome = run(
        [{'from': 'suggested_new_tags', 'action': 'queue_suggestions'}],
        {'suggested_new_tags': [{'name': '赛博朋克', 'reason': '有机械元素'}]},
        api, store,
    )

    assert outcome['applied'] is True
    assert '赛博朋克' in store.list_tag_suggestions()


def test_write_card_fields_only_allows_declared_fields(api, store):
    """模型可能凭空写出奇怪键名，必须按 `params.fields` 白名单过滤。"""
    written = {}

    def fake_write(card_id, patch):
        written.update(patch)
        return True

    api.cards.write_fields = fake_write
    outcome = run(
        [{'from': 'rewrite', 'action': 'write_card_fields',
          'params': {'fields': ['description'], 'snapshot': False}}],
        {'rewrite': {'description': '新简介', 'name': '恶意改名'}},
        api, store,
    )

    assert outcome['applied'] is True
    assert written == {'description': '新简介'}, 'name 不在白名单，必须被剔除'


def test_write_card_fields_skips_when_nothing_allowed(api, store):
    outcome = run(
        [{'from': 'rewrite', 'action': 'write_card_fields', 'params': {'fields': ['x']}}],
        {'rewrite': {'description': '新简介'}},
        api, store,
    )
    assert outcome['applied'] is False
    assert outcome['actions'][0]['skipped'] == 'no_allowed_field'


def test_move_cards_creates_category_then_moves(api, store):
    created = []
    moved = []
    api.cards.create_category = lambda name, parent='': created.append(name) or True
    api.cards.move = lambda ids, target: moved.append((list(ids), target)) or {
        'count': 1, 'moved': list(ids), 'failed': [], 'category_counts': {},
    }

    outcome = run([{'from': 'category', 'action': 'move_cards'}],
                  {'category': '奇幻'}, api, store)

    assert created == ['奇幻']
    assert moved == [(['Alice.png'], '奇幻')]
    assert outcome['applied'] is True


def test_move_cards_skips_empty_category(api, store):
    outcome = run([{'from': 'category', 'action': 'move_cards'}],
                  {'category': ''}, api, store)
    assert outcome['applied'] is False
    assert outcome['actions'][0]['skipped'] == 'empty'


# ------------------------------------------------------------------ 批量语义


def test_single_action_failure_does_not_block_others(api, store):
    """概览写不进去，不该让标签也丢掉。"""
    def boom(*args, **kwargs):
        raise RuntimeError('桥炸了')

    import sys
    bridge = sys.modules['stm_ai_backend.summary_bridge']
    original = bridge.write_summary
    bridge.write_summary = boom
    try:
        outcome = run([
            {'from': 'summary', 'action': 'write_card_note'},
            {'from': 'tags', 'action': 'apply_tags'},
        ], {'summary': '概览', 'tags': []}, api, store,
            context={'accepted_tags': ['奇幻']})
    finally:
        bridge.write_summary = original

    assert outcome['actions'][0]['ok'] is False
    assert outcome['actions'][1]['applied'] is True, '后一个动作仍要执行'
    assert outcome['applied'] is True


def test_unknown_action_is_reported_not_silently_dropped(api, store):
    outcome = run([{'from': 'summary', 'action': 'teleport'}],
                  {'summary': 'x'}, api, store)

    assert outcome['actions'][0]['ok'] is False
    assert outcome['actions'][0]['skipped'] == 'unknown_action'
    assert any('未知的应用动作' in item for item in outcome['warnings'])


def test_empty_action_list_is_a_noop(api, store):
    outcome = run([], {'summary': 'x'}, api, store)
    assert outcome == {'applied': False, 'actions': [], 'warnings': []}


def test_garbage_action_list_is_a_noop(api, store):
    for garbage in (None, 'not-a-list', 42):
        outcome = actions.run_actions(
            garbage, {'summary': 'x'}, api=api, store=store, card_id='A',
        )
        assert outcome['actions'] == []


def test_missing_source_yields_empty_value(api, store):
    """`from` 指向不存在的字段时按空值处理，不该崩。"""
    outcome = run([{'from': 'nope', 'action': 'write_card_note'}],
                  {'summary': 'x'}, api, store)
    assert outcome['applied'] is False
    assert outcome['actions'][0]['skipped'] == 'empty'


# ------------------------------------------------------------------ 动作清单


def test_every_declared_action_has_a_handler():
    """`profiles.APPLY_ACTIONS` 与 `actions.ACTION_HANDLERS` 必须一致。"""
    profiles = load_backend_submodule('core.profiles')

    assert set(profiles.APPLY_ACTIONS) == set(actions.ACTION_HANDLERS), (
        '声明的动作与实现的动作必须一一对应'
    )
