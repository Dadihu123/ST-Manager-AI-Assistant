"""模型能力：上下文/输出/推理设置的解析（`backend/core/modelcap.py`）。"""

from conftest import load_backend_submodule

modelcap = load_backend_submodule('core.modelcap')
agent = load_backend_submodule('core.agent')


# ---------------------------------------------------------------- 归一化


def test_defaults_are_256k_context_and_32k_output():
    caps = modelcap.normalize_capabilities({})
    assert caps['context_length'] == 262144
    assert caps['max_output_tokens'] == 32768
    assert caps['reasoning_enabled'] is True
    assert caps['reasoning_effort'] == 'auto'


def test_output_never_exceeds_context():
    caps = modelcap.normalize_capabilities({
        'context_length': 8192, 'max_output_tokens': 32768,
    })
    assert caps['max_output_tokens'] == 8192


def test_values_are_clamped_and_bad_values_fall_back():
    caps = modelcap.normalize_capabilities({
        'context_length': 1,
        'max_output_tokens': '不是数字',
        'reasoning_effort': 'ultra',
    })
    assert caps['context_length'] == modelcap.MIN_CONTEXT_LENGTH
    # 回落到默认 32K，但仍要服从上下文（8K）——输出不可能大于上下文
    assert caps['max_output_tokens'] == modelcap.MIN_CONTEXT_LENGTH
    assert caps['reasoning_effort'] == 'auto'


# ---------------------------------------------------------------- max_tokens


def test_profile_max_tokens_zero_follows_global_output():
    caps = {'max_output_tokens': 65536}
    assert modelcap.resolve_max_tokens({'max_tokens': 0}, caps) == 65536


def test_profile_explicit_max_tokens_is_respected_but_capped():
    caps = {'max_output_tokens': 32768}
    assert modelcap.resolve_max_tokens({'max_tokens': 4096}, caps) == 4096
    assert modelcap.resolve_max_tokens({'max_tokens': 999999}, caps) == 32768
    assert modelcap.resolve_max_tokens({'max_tokens': 0}, caps) == 32768


# ---------------------------------------------------------------- 推理


def test_reasoning_disabled_injects_none_for_openai_protocols():
    caps = {'reasoning_enabled': False}
    assert modelcap.resolve_extra_body({}, caps, 'openai_chat') == {'reasoning_effort': 'none'}
    assert modelcap.resolve_extra_body({}, caps, 'openai_responses') == {
        'reasoning_effort': 'none',
    }


def test_reasoning_effort_is_injected_only_when_not_auto():
    assert modelcap.resolve_extra_body({}, {'reasoning_effort': 'auto'}, 'openai_chat') == {}
    assert modelcap.resolve_extra_body({}, {'reasoning_effort': 'high'}, 'openai_chat') == {
        'reasoning_effort': 'high',
    }


def test_profile_explicit_reasoning_setting_wins():
    profile = {'extra_body': {'reasoning_effort': 'none'}}
    caps = {'reasoning_enabled': True, 'reasoning_effort': 'high'}
    assert modelcap.resolve_extra_body(profile, caps, 'openai_chat') == {
        'reasoning_effort': 'none',
    }


def test_anthropic_does_not_get_reasoning_effort():
    caps = {'reasoning_enabled': False, 'reasoning_effort': 'high'}
    assert modelcap.resolve_extra_body({}, caps, 'anthropic_messages') == {}


# ---------------------------------------------------------------- 提示词预算


def test_prompt_budget_default_is_unchanged():
    assert modelcap.resolve_prompt_budget({}) == 8000


def test_prompt_budget_is_clamped_by_small_context():
    caps = {'context_length': 8192, 'max_output_tokens': 2048}
    assert modelcap.resolve_prompt_budget(caps, 8000) == 2048


# ---------------------------------------------------------------- Agent 预算


def test_adaptive_iterations_follow_library_size():
    assert modelcap.adaptive_iterations(40) == 7
    assert modelcap.adaptive_iterations(8) == 4
    assert modelcap.adaptive_iterations(200) == 12


def test_resolve_agent_budget_applies_adaptive_iterations_and_context():
    budget = modelcap.resolve_agent_budget({}, 40, {'context_length': 131072,
                                                    'max_output_tokens': 16384})
    assert budget['max_iterations'] == 7, '缺省时按 40 张卡 → 7 轮'
    assert budget['context_budget'] == 131072 - 16384 - 8192


def test_resolve_agent_budget_respects_explicit_values():
    budget = modelcap.resolve_agent_budget(
        {'max_iterations': 9, 'context_budget': 12000}, 40, {},
    )
    assert budget['max_iterations'] == 9
    assert budget['context_budget'] == 12000


def test_resolve_agent_budget_clamps_read_budgets_to_small_context():
    caps = {'context_length': 8192, 'max_output_tokens': 4096}
    budget = modelcap.resolve_agent_budget({}, 40, caps)
    available = modelcap.context_available(caps)
    assert budget['catalog_tokens'] <= available
    assert budget['read_batch_tokens'] <= available


def test_agent_meta_max_iterations_matches_resolved_budget():
    """公式口径与 Agent 内部默认预算表对齐（`max(4, min(12, ceil(卡数/6)))`）。"""
    assert agent.default_budget()['max_iterations'] == 6  # 显式值兜底仍是 6
    assert modelcap.adaptive_iterations(40) == 7
