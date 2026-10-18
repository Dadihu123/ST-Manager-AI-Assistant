"""世界书改写：内容校验（`core/wicheck.py`）与作业链路。

**核心立场**（设计文档 §6.3）：这是「让 AI 优先按 YAML 输出」的提示词约束，
**不是**「产出程序可解析的 YAML」。所以：

- 不引入 `pyyaml`，不做 `safe_load` 校验；
- 只校验「内容有没有被破坏」：宏、HTML、压缩比、长度比、专有名词、调度字段。

测试因此不测「YAML 合不合法」，只测「设定有没有丢、程序字段有没有被改」。
"""

import pytest

from conftest import load_backend_submodule

pytest.importorskip('flask', reason='装配层依赖 flask')

wicheck = load_backend_submodule('core.wicheck')
profiles = load_backend_submodule('core.profiles')
jobs = load_backend_submodule('jobs')
errors = load_backend_submodule('core.errors')

from fake_api import FakeApi, FakeCtx  # noqa: E402


# ------------------------------------------------------------------ 宏保真


def test_lost_macro_is_an_issue():
    """宏是程序注入用的，丢了就是功能性破坏。"""
    result = wicheck.validate_rewrite(
        '你好 {{user}}，我是 {{char}}。' + '设定内容。' * 40,
        '你好，我是角色。' + '设定内容。' * 30,
    )
    assert result['ok'] is False
    assert any('丢失宏' in item for item in result['issues'])


def test_preserved_macros_pass():
    original = '你好 {{user}}。' + '世界观设定。' * 40
    candidate = '你好 {{user}}。' + '世界观设定。' * 20
    result = wicheck.validate_rewrite(original, candidate)
    assert not any('丢失宏' in item for item in result['issues'])


def test_new_macro_is_only_a_warning():
    """AI 自己加了宏不至于判失败，但要提示。"""
    result = wicheck.validate_rewrite(
        '正文。' + '设定。' * 40, '正文 {{char}}。' + '设定。' * 20,
    )
    assert any('新增了原文没有的宏' in item for item in result['warnings'])


# ------------------------------------------------------------------ HTML


def test_html_balance_detects_unclosed():
    ok, message = wicheck.html_balance('<div><span>文字</div>')
    assert ok is False
    assert '错配' in message or '未闭合' in message


def test_html_balance_ignores_void_tags():
    ok, _message = wicheck.html_balance('<br><hr><img src="x">文字')
    assert ok is True


def test_html_balance_accepts_self_closing():
    ok, _message = wicheck.html_balance('<img src="x" /><br/>文字')
    assert ok is True


def test_unbalanced_html_is_an_issue():
    original = '<div>' + '设定。' * 40 + '</div>'
    result = wicheck.validate_rewrite(original, '<div>' + '设定。' * 20)
    assert result['ok'] is False
    assert any('HTML 未配平' in item for item in result['issues'])


# ------------------------------------------------------------------ 压缩效果


def test_no_compression_is_an_issue():
    """压缩是本能力的目的，没压缩就没意义。"""
    original = '设定。' * 40
    result = wicheck.validate_rewrite(original, original)
    assert result['ok'] is False
    assert any('没有压缩效果' in item for item in result['issues'])


def test_good_compression_passes():
    original = '设定内容。' * 50
    candidate = '设定内容。' * 20
    result = wicheck.validate_rewrite(original, candidate)
    assert not any('没有压缩效果' in item for item in result['issues'])
    assert result['metrics']['token_ratio'] < 0.5


def test_overcompression_is_an_issue():
    """整条删成一句话 = 静默丢设定。"""
    original = '这是一段很长的设定内容。' * 60
    result = wicheck.validate_rewrite(original, '设定。')
    assert result['ok'] is False
    assert any('压缩过度' in item for item in result['issues'])


# ------------------------------------------------------------------ 专有名词


def test_dropped_proper_nouns_are_reported():
    original = '魔法来自「元素之心」，共 3 级。' + '补充说明。' * 40
    candidate = '魔法来自某处。' + '补充说明。' * 20
    result = wicheck.validate_rewrite(original, candidate)
    assert '元素之心' in result['dropped_facts']


def test_dropped_numbers_are_reported():
    original = '力量 30 级。' + '设定。' * 40
    candidate = '力量很高。' + '设定。' * 20
    result = wicheck.validate_rewrite(original, candidate)
    assert any('30' in item for item in result['dropped_facts'])


def test_kept_nouns_produce_no_dropped_facts():
    original = '魔法来自「元素之心」。' + '设定。' * 40
    candidate = '魔法来自「元素之心」。' + '设定。' * 20
    result = wicheck.validate_rewrite(original, candidate)
    assert result['dropped_facts'] == []


def test_dropped_facts_are_capped():
    original = ''.join(f'「名词{i}」' for i in range(50)) + '设定。' * 40
    candidate = '设定。' * 20
    result = wicheck.validate_rewrite(original, candidate)
    assert len(result['dropped_facts']) <= wicheck.MAX_REPORTED_FACTS


# ------------------------------------------------------------------ 调度字段


def test_schedule_field_change_is_an_issue():
    """这些字段是程序读的，改了会改变触发行为。"""
    original_entry = {'constant': True, 'insertion_order': 100, 'content': 'x'}
    candidate_entry = {'constant': False, 'insertion_order': 100, 'content': 'x'}

    result = wicheck.validate_rewrite(
        '设定。' * 40, '设定。' * 20,
        original_entry=original_entry, candidate_entry=candidate_entry,
    )
    assert result['ok'] is False
    assert any('调度字段被改动' in item and 'constant' in item for item in result['issues'])


def test_unchanged_schedule_fields_pass():
    entry = {'constant': True, 'insertion_order': 100, 'content': 'x'}
    result = wicheck.validate_rewrite(
        '设定。' * 40, '设定。' * 20,
        original_entry=entry, candidate_entry=dict(entry),
    )
    assert not any('调度字段' in item for item in result['issues'])


# ------------------------------------------------------------------ 提示词


def test_yaml_instruction_asks_for_block_scalar():
    """默认用 block scalar 包裹多行文本 —— 一次性消除转义问题。"""
    text = wicheck.build_rewrite_instruction('yaml')
    assert 'block scalar' in text
    assert '|-' in text


def test_plain_instruction_has_no_yaml_requirement():
    text = wicheck.build_rewrite_instruction('text')
    assert 'yaml' not in text.lower()
    assert '保留' in text


def test_instruction_keeps_guardrails():
    for mode in ('yaml', 'text'):
        text = wicheck.build_rewrite_instruction(mode)
        assert '宏' in text or '{{' in text
        assert '专有名词' in text


# ------------------------------------------------------------------ 内置任务规格


def test_builtin_wi_rewrite_profile_is_valid():
    profile = profiles.builtin_profile('builtin-wi-rewrite')
    assert profile is not None
    assert profiles.validate_profile(profile) == []


def test_wi_rewrite_profile_does_not_hijack_model_matching():
    """它是「一个具体功能」，不该靠 `*` + priority 抢走兜底档案。"""
    profile = profiles.builtin_profile('builtin-wi-rewrite')
    assert profile['model_match'] == ''
    selected = profiles.select_profile(profiles.builtin_profiles(), 'some-model')
    assert selected['id'] == 'builtin-default'


def test_wi_rewrite_profile_disables_json_and_tag_pool():
    """正文是自由文本，不该被 JSON 约束；世界书改写也不需要标签池。"""
    profile = profiles.builtin_profile('builtin-wi-rewrite')
    assert profile['output_schema']['json_only'] is False
    assert profile['tag_pool']['budget_tokens'] == 0
    assert profile['tag_pool']['allow_new_tags'] is False
    assert profile['content_selector']['worldbook']['mode'] == 'none'


# ------------------------------------------------------------------ 作业链路


@pytest.fixture
def env(tmp_path):
    store_module = load_backend_submodule('store')
    api = FakeApi(
        data_dir=str(tmp_path / 'data'),
        secrets={'api_key': 'sk-test'},
        cards={'Alice.png': {'data': {'name': 'A', 'character_book': {'entries': []}}}},
    )
    return api, store_module.PluginStore(api)


def _book(entries):
    return {'entries': entries, 'source_revision': 'rev-1'}


def test_resolve_wi_source_defaults_to_embedded():
    source = jobs.resolve_wi_source('sub/card.json', None)
    assert source == {'source_type': 'embedded', 'card_id': 'sub/card.json'}


def test_resolve_wi_source_uses_given_source():
    given = {'source_type': 'global', 'file_path': 'lorebooks/x.json'}
    assert jobs.resolve_wi_source('ignored', given) == given


def test_run_wi_rewrite_writes_back_after_validation(env, monkeypatch):
    api, store = env
    book = _book([{
        'uid': '1', 'comment': '世界观', 'keys': ['世界'],
        'content': '魔法来自「元素之心」。' + '设定内容。' * 40,
    }])
    api.worldbook.read = lambda source: book
    writes = []
    api.worldbook.write = lambda source, data, expected_revision='': (
        writes.append({'source': source, 'data': data}) or
        {'success': True, 'source_revision': 'rev-2'}
    )
    api.backups.snapshot_card = lambda card_id, remark='': None

    monkeypatch.setattr(jobs.llm_module if hasattr(jobs, 'llm_module') else jobs.llm, 'chat',
                        lambda messages, **kwargs: {
                            'content': '魔法来自「元素之心」。' + '设定内容。' * 15,
                            'usage': {'prompt_tokens': 100, 'completion_tokens': 30},
                        })

    ctx = FakeCtx([{'key': '1', 'payload': {'card_id': 'Alice.png', 'entry_uid': '1'}}],
                  payload={'provider_id': 'openai', 'apply_mode': 'auto'})
    jobs.run_wi_rewrite(ctx, api=api, store=store)

    assert ctx.summary['applied'] == 1
    assert ctx.summary['written'] == 1
    assert writes, '应该写回一次'
    assert '设定内容。' * 15 in writes[0]['data']['entries'][0]['content']


def test_run_wi_rewrite_rejects_failed_validation(env, monkeypatch):
    """校验不过就不写回，条目状态为 failed。"""
    api, store = env
    # 原文足够长，AI 只返回一句 → 触发「压缩过度」
    book = _book([{
        'uid': '1', 'comment': 'X', 'keys': [],
        'content': '这是一段很长的世界观设定内容，包含大量细节。' * 20,
    }])
    api.worldbook.read = lambda source: book
    writes = []
    api.worldbook.write = lambda *a, **k: writes.append(a) or {'success': True}

    monkeypatch.setattr(jobs.llm, 'chat', lambda messages, **kwargs: {
        'content': '设定。',
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5},
    })

    ctx = FakeCtx([{'key': '1', 'payload': {'card_id': 'Alice.png', 'entry_uid': '1'}}],
                  payload={'provider_id': 'openai', 'apply_mode': 'auto'})
    jobs.run_wi_rewrite(ctx, api=api, store=store)

    assert ctx.summary['failed'] == 1
    assert writes == [], '校验失败不得写回'
    assert ctx.reports[0]['status'] == 'failed'
    assert any('压缩过度' in item for item in ctx.reports[0]['result']['issues'])


def test_run_wi_rewrite_review_mode_defers_writeback(env, monkeypatch):
    api, store = env
    book = _book([{
        'uid': '1', 'comment': 'X', 'keys': [],
        'content': '设定内容。' * 40,
    }])
    api.worldbook.read = lambda source: book
    writes = []
    api.worldbook.write = lambda *a, **k: writes.append(a) or {'success': True}

    monkeypatch.setattr(jobs.llm, 'chat', lambda messages, **kwargs: {
        'content': '设定内容。' * 15,
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5},
    })

    ctx = FakeCtx([{'key': '1', 'payload': {'card_id': 'Alice.png', 'entry_uid': '1'}}],
                  payload={'provider_id': 'openai', 'apply_mode': 'review'})
    jobs.run_wi_rewrite(ctx, api=api, store=store)

    assert ctx.summary['awaiting_review'] == 1
    assert writes == [], 'review 模式不得写盘'


def test_run_wi_rewrite_dry_run_writes_nothing(env, monkeypatch):
    api, store = env
    book = _book([{'uid': '1', 'comment': 'X', 'keys': [], 'content': '设定内容。' * 40}])
    api.worldbook.read = lambda source: book
    writes = []
    api.worldbook.write = lambda *a, **k: writes.append(a) or {'success': True}

    monkeypatch.setattr(jobs.llm, 'chat', lambda messages, **kwargs: {
        'content': '设定内容。' * 15,
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5},
    })

    ctx = FakeCtx([{'key': '1', 'payload': {'card_id': 'Alice.png', 'entry_uid': '1'}}],
                  payload={'provider_id': 'openai', 'apply_mode': 'auto', 'dry_run': True})
    jobs.run_wi_rewrite(ctx, api=api, store=store)

    assert writes == []
    assert ctx.reports[0]['result']['dry_run'] is True


def test_run_wi_rewrite_reports_missing_entry(env, monkeypatch):
    api, store = env
    api.worldbook.read = lambda source: _book([])

    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: {'content': 'x', 'usage': {}})

    ctx = FakeCtx([{'key': 'nope', 'payload': {'card_id': 'Alice.png', 'entry_uid': 'nope'}}],
                  payload={'provider_id': 'openai'})
    jobs.run_wi_rewrite(ctx, api=api, store=store)

    assert ctx.summary['failed'] == 1


def test_run_wi_rewrite_skips_empty_entry(env, monkeypatch):
    api, store = env
    api.worldbook.read = lambda source: _book([
        {'uid': '1', 'comment': 'X', 'keys': [], 'content': '   '},
    ])
    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: {'content': 'x', 'usage': {}})

    ctx = FakeCtx([{'key': '1', 'payload': {'card_id': 'Alice.png', 'entry_uid': '1'}}],
                  payload={'provider_id': 'openai', 'apply_mode': 'auto'})
    jobs.run_wi_rewrite(ctx, api=api, store=store)

    assert ctx.reports[0]['result']['skipped'] == 'empty_entry'
    assert ctx.summary['written'] == 0


def test_manifest_declares_wi_rewrite_job():
    import json
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'manifest.json'), encoding='utf-8') as handle:
        manifest = json.load(handle)
    assert 'ai.wi_rewrite' in manifest['backend']['jobs']
