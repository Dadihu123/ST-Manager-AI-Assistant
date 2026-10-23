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
#
# 语义变化（设计文档 §0.1 / §6.3）：**「变长」是 note，「压坏」是 block。**
# 「压缩有效」和「长度比下限」都不是本能力的目的本身——短条目本来最多只能省
# 几个字，要求它「必须变短」等于逼模型加壳。所以这两条从 `issues` 里移出。


def test_unchanged_text_is_not_an_issue():
    """原样返回是**合法结果**（`[MVU] InitVar` 实测就是这么返回的）。

    修之前这里断言 `ok=False`（「没有压缩效果」），逼得模型对已经足够精炼的
    条目扩写或加壳。作业级的 `skipped: unchanged` 由 C25 负责，本用例只管
    校验器不再把它判成失败。
    """
    original = '设定。' * 40
    result = wicheck.validate_rewrite(original, original)
    assert result['ok'] is True
    assert not any('没有压缩效果' in item for item in result['issues'])
    assert not any('压缩' in item for item in result['issues'])
    assert result['metrics']['token_ratio'] == 1.0


def test_good_compression_passes():
    original = '设定内容。' * 50
    candidate = '设定内容。' * 20
    result = wicheck.validate_rewrite(original, candidate)
    assert not any('没有压缩效果' in item for item in result['issues'])
    assert result['metrics']['token_ratio'] < 0.5


def test_overcompression_is_only_a_note():
    """整条删成一句话 = 可能丢设定，但**只提示、不拦**（note 级）。"""
    original = '这是一段很长的设定内容。' * 60
    result = wicheck.validate_rewrite(original, '设定。')
    assert result['ok'] is True, '删得狠不是「内容被破坏」'
    assert not any('压缩过度' in item for item in result['issues'])
    assert any('删得偏多' in item for item in result['warnings'])


def test_longer_candidate_is_only_a_note():
    """变长只是一种结果，照实显示即可（§6.4：不出现「失败」字样）。"""
    original = '设定。' * 40
    candidate = '设定。' * 40 + '补充一点内容。'
    result = wicheck.validate_rewrite(original, candidate)
    assert result['ok'] is True
    assert result['issues'] == []
    assert any('精简不明显' in item for item in result['warnings'])


# ------------------------------------------------------------------ HTML（相对原文）
#
# 修之前：候选不配平就判失败。实测 `[MVU] 变量更新规则`、`[核心] 中央统合系统`
# 两条**原文本身就不配平**，候选延续了同样的状态却被判失败——注释写的是
# 「延续原状不判失败」，代码做的是相反的。


def test_unbalanced_original_does_not_fail_the_candidate():
    """原文本来就不配平 + 候选保持相同的不平衡 → 通过（T6）。"""
    original = '<pokemon_custom_start>\n设定内容。' + '细节。' * 40
    candidate = '<pokemon_custom_start>\n设定内容。' + '细节。' * 20
    result = wicheck.validate_rewrite(original, candidate)
    assert result['ok'] is True
    assert not any('HTML 未配平' in item for item in result['issues'])


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
#
# 「改写格式」这个可选项已删除（C3/C4，设计文档 §2.2）：格式意图写在固定规则里。


def test_instruction_forbids_outer_keys():
    """明确禁止外层键 —— 那层壳会被原样写进世界书条目正文（§0.2）。

    快照：53 条 yaml 候选里 39 条（74%）带「名称/关键词/原文」信封，
    不带壳的 14 条都是「原文本身已是 YAML」，说明壳是格式指令逼出来的。
    """
    text = wicheck.build_rewrite_instruction()
    assert '外层键' in text
    assert '名称' in text and '原文' in text
    # 不再要求任何一种「容器」
    assert 'block scalar' not in text
    assert '|-' not in text


def test_instruction_keeps_code_intact():
    """代码/模板/表格/INI 类正文要保持原样，不转 YAML。"""
    text = wicheck.build_rewrite_instruction()
    assert '代码' in text
    assert '保持原样' in text
    assert '不要转成 YAML' in text


def test_instruction_is_single_fixed_rule():
    """只有一条固定规则，不再按格式分支（传参数不再有任何作用）。"""
    import inspect

    assert list(inspect.signature(wicheck.build_rewrite_instruction).parameters) == []
    assert wicheck.build_rewrite_instruction() == wicheck.REWRITE_OUTPUT_RULE


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
    """校验不过就不写回，条目状态为 failed。

    触发方式刻意用**自定义标签被改成不平衡**，而不是「压缩过度」：
    后者现在只是 note（C1），测不出失败路径。
    也不能用布局 HTML（`<div>`）或宏——那些会被前置过滤挡在批次外（C5）。
    """
    api, store = env
    body = '这是一段很长的世界观设定内容，包含大量细节。'
    # 原文配平、候选丢掉闭合标签 → `block`
    book = _book([{
        'uid': '1', 'comment': 'X', 'keys': [],
        'content': '<analysis>主线：魔法来自「元素之心」。</analysis>' + body * 20,
    }])
    api.worldbook.read = lambda source: book
    writes = []
    api.worldbook.write = lambda *a, **k: writes.append(a) or {'success': True}

    monkeypatch.setattr(jobs.llm, 'chat', lambda messages, **kwargs: {
        'content': '<analysis>主线：魔法来自「元素之心」。' + body * 10,
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5},
    })

    ctx = FakeCtx([{'key': '1', 'payload': {'card_id': 'Alice.png', 'entry_uid': '1'}}],
                  payload={'provider_id': 'openai', 'apply_mode': 'auto'})
    jobs.run_wi_rewrite(ctx, api=api, store=store)

    assert ctx.summary['failed'] == 1
    assert writes == [], '校验失败不得写回'
    assert ctx.reports[0]['status'] == 'failed'
    assert any('HTML 未配平' in item for item in ctx.reports[0]['result']['issues'])


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


# ------------------------------------------------------------------ 前置过滤（C5 / §7）


def test_protected_entry_skips_without_llm_call(env, monkeypatch):
    """T1：`[MVU] InitVar` 命中前置过滤，一次模型调用都不发。

    注：设计文档 §13 的 T1 把期望值写成 `protected:structured`，而同一份文档
    §7 的判定顺序是「名字 → 结构化 → INI → 标记」，`InitVar` 名字先命中，
    实际返回 `protected:name`。两者都表示「不送模型」，验收口径以 §14.1 的
    「不产生 LLM 调用」为准。
    """
    api, store = env
    api.worldbook.read = lambda source: _book([{
        'uid': '1', 'comment': '[MVU] InitVar', 'keys': [],
        'content': '变量:\n  好感度: 0\n  信任度: 0\n  体力: 100\n',
    }])
    writes = []
    api.worldbook.write = lambda *a, **k: writes.append(a) or {'success': True}
    calls = []
    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: calls.append(1) or {
        'content': 'x', 'usage': {},
    })

    ctx = FakeCtx([{'key': '1', 'payload': {'card_id': 'Alice.png', 'entry_uid': '1'}}],
                  payload={'provider_id': 'openai', 'apply_mode': 'auto'})
    jobs.run_wi_rewrite(ctx, api=api, store=store)

    assert calls == [], '被前置过滤命中的条目不得调用模型'
    assert ctx.reports[0]['result']['skipped'].startswith('protected:')
    assert ctx.summary['written'] == 0
    assert ctx.summary['failed'] == 0, '跳过不是失败'


def test_protected_by_structure_even_with_innocent_name(env, monkeypatch):
    """名字不含任何信号，但正文是一棵结构化树 → 仍然跳过。

    §7 的阈值：「≥3 个顶层键且有嵌套」或「≥6 个顶层键」。这里给 3 个顶层键
    + 两层嵌套，是 MVU 变量树最常见的形状。
    """
    api, store = env
    api.worldbook.read = lambda source: _book([{
        'uid': '1', 'comment': '任务 - 藤村智美', 'keys': [],
        'content': (
            '角色:\n  姓名: 藤村智美\n  年龄: 17\n'
            '状态:\n  好感度: 0\n  信任度: 0\n'
            '关系:\n  学校: 第三城高中\n  社团: 摄影部\n'
        ),
    }])
    calls = []
    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: calls.append(1) or {
        'content': 'x', 'usage': {},
    })

    ctx = FakeCtx([{'key': '1', 'payload': {'card_id': 'Alice.png', 'entry_uid': '1'}}],
                  payload={'provider_id': 'openai', 'apply_mode': 'auto'})
    jobs.run_wi_rewrite(ctx, api=api, store=store)

    assert calls == []
    assert ctx.reports[0]['result']['skipped'] == 'protected:structured'


def test_force_bypasses_prefilter(env, monkeypatch):
    """`force=True` 可以强制处理被过滤的条目（面板里单独勾选）。"""
    api, store = env
    api.worldbook.read = lambda source: _book([{
        'uid': '1', 'comment': '[MVU] InitVar', 'keys': [],
        'content': '设定。' * 40,
    }])
    api.worldbook.write = lambda *a, **k: {'success': True}
    calls = []
    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: calls.append(1) or {
        'content': '设定。' * 20, 'usage': {},
    })

    ctx = FakeCtx([{'key': '1', 'payload': {'card_id': 'Alice.png', 'entry_uid': '1'}}],
                  payload={'provider_id': 'openai', 'apply_mode': 'auto', 'force': True})
    jobs.run_wi_rewrite(ctx, api=api, store=store)

    assert calls == [1], 'force 必须绕过前置过滤'


# ------------------------------------------------------------------ 提示词的守卫现在在档案里
#
# C3 把「保留宏/专有名词」这类约束从输出规则移回**档案的 system**（§2.1）：
# 输出规则只管「怎么输出」，内容守恒由提示词主体负责。


def test_profile_system_carries_the_fidelity_rules():
    system = profiles.builtin_profile('builtin-wi-rewrite')['system']
    assert '双花括号' in system and 'getvar' in system
    assert 'EJS' in system and 'script' in system
    assert '专有名词' in system
    assert '如果删不动，就保持原样' in system


def test_profile_template_does_not_demand_length_window():
    """「长度控制在原文的 30%–95%」已经删掉——这就是用户要去掉的门限。"""
    template = profiles.builtin_profile('builtin-wi-rewrite')['user_template']
    assert '30%' not in template
    assert '95%' not in template
    assert '允许不改' in template
    assert '外层键' in template


def test_profile_is_renamed_to_plain_polish():
    """「（YAML）」这个名字会误导用户以为格式可选（§2.2）。"""
    profile = profiles.builtin_profile('builtin-wi-rewrite')
    assert profile['name'] == '世界书精简'
    assert 'YAML' not in profile['name']
