"""三种 API 协议的抹平层（`backend/protocols.py`）。

重点覆盖**协议之间的差异**，而不是「函数能跑」：
端点、鉴权头、system 位置、`max_tokens` 字段名、`stop` 映射、usage 字段名、
响应正文位置、流式事件名。每条差异都有一个用例——它们是这类代码唯一会出错的地方。
"""

import pytest

from conftest import load_backend_submodule

protocols = load_backend_submodule('protocols')

CHAT_MESSAGES = [
    {'role': 'system', 'content': '你是分析助手'},
    {'role': 'user', 'content': '分析这张卡'},
]
WITH_PREFILL = CHAT_MESSAGES + [{'role': 'assistant', 'content': '{'}]


# ------------------------------------------------------------------ 协议名


def test_normalize_protocol_falls_back_to_openai_chat():
    assert protocols.normalize_protocol('openai_chat') == 'openai_chat'
    assert protocols.normalize_protocol('openai_responses') == 'openai_responses'
    assert protocols.normalize_protocol('anthropic_messages') == 'anthropic_messages'
    # 未知 / 空 / None 一律回落默认
    assert protocols.normalize_protocol('') == 'openai_chat'
    assert protocols.normalize_protocol(None) == 'openai_chat'
    assert protocols.normalize_protocol('openai') == 'openai_chat'
    assert protocols.normalize_protocol('ANTHROPIC_MESSAGES') == 'anthropic_messages'


# ------------------------------------------------------------------ 端点


@pytest.mark.parametrize('base_url,protocol,expected', [
    ('https://api.openai.com/v1', 'openai_chat',
     'https://api.openai.com/v1/chat/completions'),
    ('https://api.openai.com/v1', 'openai_responses',
     'https://api.openai.com/v1/responses'),
    ('https://api.anthropic.com', 'anthropic_messages',
     'https://api.anthropic.com/v1/messages'),
    # 用户把完整端点直接填进来也要幂等（不能拼成 /chat/completions/responses）
    ('https://api.openai.com/v1/chat/completions', 'openai_chat',
     'https://api.openai.com/v1/chat/completions'),
    ('https://api.openai.com/v1/chat/completions', 'openai_responses',
     'https://api.openai.com/v1/responses'),
    ('https://api.anthropic.com/v1/messages', 'anthropic_messages',
     'https://api.anthropic.com/v1/messages'),
    # 第三方网关带路径前缀
    ('https://gw.example.com/openai/v1', 'openai_chat',
     'https://gw.example.com/openai/v1/chat/completions'),
    # 尾斜杠
    ('https://api.openai.com/v1/', 'openai_chat',
     'https://api.openai.com/v1/chat/completions'),
])
def test_endpoint_for_is_protocol_aware_and_idempotent(base_url, protocol, expected):
    assert protocols.endpoint_for(base_url, protocol) == expected


def test_endpoint_for_empty_base_url():
    assert protocols.endpoint_for('', 'openai_chat') == ''


@pytest.mark.parametrize('base_url,protocol,expected', [
    ('https://api.openai.com/v1', 'openai_chat', 'https://api.openai.com/v1/models'),
    ('https://api.openai.com/v1', 'openai_responses', 'https://api.openai.com/v1/models'),
    ('https://api.anthropic.com', 'anthropic_messages', 'https://api.anthropic.com/v1/models'),
    ('https://gw.example.com/openai/v1', 'openai_chat',
     'https://gw.example.com/openai/v1/models'),
])
def test_models_endpoint_shares_base_with_request_endpoint(base_url, protocol, expected):
    """`/models` 必须与真实请求同一个基址（否则第三方网关会 404）。"""
    assert protocols.models_endpoint_for(base_url, protocol) == expected


# ------------------------------------------------------------------ 鉴权头


def test_headers_bearer_for_openai_protocols():
    for protocol in ('openai_chat', 'openai_responses'):
        headers = protocols.build_headers(protocol, 'sk-abc')
        assert headers['Authorization'] == 'Bearer sk-abc'
        assert 'x-api-key' not in headers


def test_headers_use_x_api_key_and_version_for_anthropic():
    headers = protocols.build_headers('anthropic_messages', 'sk-ant')
    assert headers['x-api-key'] == 'sk-ant'
    assert headers['anthropic-version'] == '2023-06-01'
    assert 'Authorization' not in headers, 'Anthropic 不认 Bearer'


def test_extra_headers_override_defaults():
    headers = protocols.build_headers('openai_chat', 'sk-abc', {'X-Title': 'ST-Manager'})
    assert headers['X-Title'] == 'ST-Manager'

    # 用户显式配置可以覆盖鉴权头（自建网关常见）
    overridden = protocols.build_headers('anthropic_messages', 'sk-ant', {'x-api-key': 'other'})
    assert overridden['x-api-key'] == 'other'


# ------------------------------------------------------------------ 请求体：openai_chat


def test_payload_openai_chat_keeps_messages_and_stop():
    payload = protocols.build_payload(
        'openai_chat', CHAT_MESSAGES, model='gpt-4o-mini',
        temperature=0.6, top_p=1.0, max_tokens=512, stop=['###'],
    )
    assert payload['model'] == 'gpt-4o-mini'
    assert payload['messages'] == CHAT_MESSAGES
    assert payload['max_tokens'] == 512
    assert payload['stop'] == ['###']
    assert payload['temperature'] == 0.6


def test_payload_openai_chat_stream_requests_usage():
    payload = protocols.build_payload(
        'openai_chat', CHAT_MESSAGES, model='m', stream=True,
    )
    assert payload['stream'] is True
    assert payload['stream_options'] == {'include_usage': True}


# ------------------------------------------------------------------ 请求体：openai_responses


def test_payload_openai_responses_uses_input_and_max_output_tokens():
    payload = protocols.build_payload(
        'openai_responses', CHAT_MESSAGES, model='gpt-5',
        temperature=0.5, max_tokens=1024, stop=['###'],
    )
    assert 'messages' not in payload
    assert payload['input'] == CHAT_MESSAGES
    assert payload['max_output_tokens'] == 1024
    assert 'max_tokens' not in payload

    # stop 不支持 → 不得出现在顶层，且被记录到 metadata 便于排查
    assert 'stop' not in payload
    assert payload['metadata']['st_requested_stop'] == '###'


# ------------------------------------------------------------------ 请求体：anthropic


def test_payload_anthropic_extracts_system_to_top_level():
    payload = protocols.build_payload(
        'anthropic_messages', CHAT_MESSAGES, model='claude-sonnet-4-5', max_tokens=2048,
    )
    assert payload['system'] == '你是分析助手'
    assert payload['messages'] == [{'role': 'user', 'content': '分析这张卡'}]
    # messages 里不得再有 system（Anthropic 会 400）
    assert all(item['role'] != 'system' for item in payload['messages'])


def test_payload_anthropic_maps_stop_to_stop_sequences():
    payload = protocols.build_payload(
        'anthropic_messages', CHAT_MESSAGES, model='claude', max_tokens=100, stop=['###', '===###'],
    )
    assert payload['stop_sequences'] == ['###', '===###']
    assert 'stop' not in payload, 'Anthropic 的字段名是 stop_sequences'


def test_payload_anthropic_always_sends_max_tokens():
    """`max_tokens` 在 Anthropic 是必填，缺省会让请求直接 400。"""
    payload = protocols.build_payload(
        'anthropic_messages', CHAT_MESSAGES, model='claude',
    )
    assert payload['max_tokens'] == protocols.ANTHROPIC_DEFAULT_MAX_TOKENS


def test_payload_anthropic_clamps_temperature_to_one():
    """档案允许 temperature ≤ 2，但 Anthropic 上限是 1。"""
    payload = protocols.build_payload(
        'anthropic_messages', CHAT_MESSAGES, model='claude', max_tokens=100, temperature=1.8,
    )
    assert payload['temperature'] == 1.0


def test_payload_anthropic_coalesces_consecutive_same_role():
    """Anthropic 要求 role 交替；相邻同 role 必须合并而不是原样发。"""
    messages = [
        {'role': 'user', 'content': 'A'},
        {'role': 'user', 'content': 'B'},
        {'role': 'assistant', 'content': 'C'},
    ]
    payload = protocols.build_payload('anthropic_messages', messages, model='claude', max_tokens=10)
    assert payload['messages'] == [
        {'role': 'user', 'content': 'A\n\nB'},
        {'role': 'assistant', 'content': 'C'},
    ]


def test_payload_anthropic_keeps_trailing_assistant_for_prefill():
    """以 assistant 结尾是 Anthropic 官方支持的预填充写法，不能丢。"""
    payload = protocols.build_payload(
        'anthropic_messages', WITH_PREFILL, model='claude', max_tokens=10,
    )
    assert payload['messages'][-1] == {'role': 'assistant', 'content': '{'}


def test_payload_anthropic_drops_empty_messages():
    messages = [
        {'role': 'user', 'content': '有内容'},
        {'role': 'assistant', 'content': '   '},
    ]
    payload = protocols.build_payload('anthropic_messages', messages, model='claude', max_tokens=10)
    assert payload['messages'] == [{'role': 'user', 'content': '有内容'}]


def test_extra_body_merges_last():
    payload = protocols.build_payload(
        'openai_chat', CHAT_MESSAGES, model='m', extra_body={'seed': 7},
    )
    assert payload['seed'] == 7


# ------------------------------------------------------------------ 响应


def test_parse_response_openai_chat():
    content, usage = protocols.parse_response('openai_chat', {
        'choices': [{'message': {'role': 'assistant', 'content': '{"a": 1}'}}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5},
    })
    assert content == '{"a": 1}'
    assert protocols.normalize_usage(usage) == {'prompt_tokens': 10, 'completion_tokens': 5}


def test_parse_response_openai_responses_prefers_output_text():
    content, _usage = protocols.parse_response('openai_responses', {
        'output_text': '直接给正文',
        'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '被忽略'}]}],
    })
    assert content == '直接给正文'


def test_parse_response_openai_responses_walks_output_blocks():
    content, usage = protocols.parse_response('openai_responses', {
        'output': [
            {'type': 'reasoning', 'content': []},
            {'type': 'message', 'content': [
                {'type': 'output_text', 'text': '前半'},
                {'type': 'output_text', 'text': '后半'},
            ]},
        ],
        'usage': {'input_tokens': 20, 'output_tokens': 8},
    })
    assert content == '前半后半'
    assert protocols.normalize_usage(usage) == {'prompt_tokens': 20, 'completion_tokens': 8}


def test_parse_response_anthropic_reads_content_blocks():
    content, usage = protocols.parse_response('anthropic_messages', {
        'content': [{'type': 'text', 'text': '{"summary": "S"}'}],
        'stop_reason': 'end_turn',
        'usage': {'input_tokens': 30, 'output_tokens': 12},
    })
    assert content == '{"summary": "S"}'
    assert protocols.normalize_usage(usage) == {'prompt_tokens': 30, 'completion_tokens': 12}


def test_parse_response_raises_on_upstream_error():
    with pytest.raises(ValueError, match='上游返回错误'):
        protocols.parse_response('anthropic_messages', {
            'error': {'message': 'invalid api key'},
        })


@pytest.mark.parametrize('protocol,payload', [
    ('openai_chat', {'choices': []}),
    ('openai_responses', {'output': []}),
    ('anthropic_messages', {'content': []}),
])
def test_parse_response_rejects_missing_text(protocol, payload):
    with pytest.raises(ValueError):
        protocols.parse_response(protocol, payload)


# ------------------------------------------------------------------ usage 归一


def test_normalize_usage_covers_all_field_names():
    assert protocols.normalize_usage({'prompt_tokens': 1, 'completion_tokens': 2}) == {
        'prompt_tokens': 1, 'completion_tokens': 2}
    assert protocols.normalize_usage({'input_tokens': 3, 'output_tokens': 4}) == {
        'prompt_tokens': 3, 'completion_tokens': 4}
    assert protocols.normalize_usage({'total_input_tokens': 5, 'total_output_tokens': 6}) == {
        'prompt_tokens': 5, 'completion_tokens': 6}
    assert protocols.normalize_usage(None) == {'prompt_tokens': 0, 'completion_tokens': 0}
    # 负数 / 非数字一律归零，不抛错
    assert protocols.normalize_usage({'input_tokens': -5, 'output_tokens': 'x'}) == {
        'prompt_tokens': 0, 'completion_tokens': 0}


# ------------------------------------------------------------------ 模型列表


def test_parse_models_openai_shape():
    assert protocols.parse_models('openai_chat', {
        'data': [{'id': 'gpt-4o-mini'}, {'id': 'gpt-4o'}],
    }) == ['gpt-4o', 'gpt-4o-mini']


def test_parse_models_anthropic_shape_strips_prefix():
    assert protocols.parse_models('anthropic_messages', {
        'data': [{'id': 'models/claude-sonnet-4-5'}, {'id': 'claude-haiku-4'}],
    }) == ['claude-haiku-4', 'claude-sonnet-4-5']


def test_parse_models_accepts_bare_list_and_dedupes():
    assert protocols.parse_models('openai_chat', ['a', 'a', 'b']) == ['a', 'b']


def test_parse_models_returns_empty_on_garbage():
    assert protocols.parse_models('openai_chat', {'unexpected': True}) == []
    assert protocols.parse_models('openai_chat', None) == []


# ------------------------------------------------------------------ 流式


def test_stream_openai_chat_accumulates_and_reads_usage():
    acc = protocols.StreamAccumulator('openai_chat')
    acc.feed('data: {"choices":[{"delta":{"content":"你"}}]}')
    acc.feed('data: {"choices":[{"delta":{"content":"好"}}]}')
    acc.feed('data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":2}}')
    acc.feed('data: [DONE]')

    assert acc.content == '你好'
    assert acc.done is True
    assert protocols.normalize_usage(acc.usage_raw) == {
        'prompt_tokens': 7, 'completion_tokens': 2}


def test_stream_anthropic_reads_text_delta_and_usage():
    acc = protocols.StreamAccumulator('anthropic_messages')
    acc.feed('data: {"type":"message_start","message":{"usage":{"input_tokens":9}}}')
    acc.feed('data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"{\\"a\\""}}')
    acc.feed('data: {"type":"content_block_delta","delta":{"type":"text_delta","text":":1}"}}')
    acc.feed('data: {"type":"message_delta","usage":{"output_tokens":3},"delta":{"stop_reason":"end_turn"}}')

    assert acc.content == '{"a":1}'
    assert acc.done is True
    # message_start 的 input_tokens 与 message_delta 的 output_tokens 要合并
    assert protocols.normalize_usage(acc.usage_raw) == {
        'prompt_tokens': 9, 'completion_tokens': 3}


def test_stream_anthropic_ignores_non_text_deltas():
    acc = protocols.StreamAccumulator('anthropic_messages')
    # thinking delta 不该混进正文
    acc.feed('data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"内部"}}')
    acc.feed('data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"正文"}}')
    assert acc.content == '正文'


def test_stream_responses_reads_output_text_delta():
    acc = protocols.StreamAccumulator('openai_responses')
    acc.feed('data: {"type":"response.created"}')
    acc.feed('data: {"type":"response.output_text.delta","delta":"概览"}')
    acc.feed('data: {"type":"response.completed","response":{"usage":{"input_tokens":5,"output_tokens":2}}}')

    assert acc.content == '概览'
    assert acc.done is True
    assert protocols.normalize_usage(acc.usage_raw) == {
        'prompt_tokens': 5, 'completion_tokens': 2}


@pytest.mark.parametrize('protocol,error_line', [
    ('anthropic_messages', 'data: {"type":"error","error":{"message":"限流"}}'),
    ('openai_responses', 'data: {"type":"response.failed","error":{"message":"失败"}}'),
    ('openai_chat', 'data: {"error":{"message":"坏了"}}'),
])
def test_stream_records_upstream_errors(protocol, error_line):
    acc = protocols.StreamAccumulator(protocol)
    acc.feed(error_line)
    assert acc.done is True
    assert acc.error


def test_stream_ignores_malformed_lines():
    acc = protocols.StreamAccumulator('openai_chat')
    acc.feed('')
    acc.feed('event: ping')
    acc.feed('data: not-json')
    acc.feed('data: {"choices":[{"delta":{"content":"ok"}}]}')
    assert acc.content == 'ok'
    assert acc.error == ''


def test_stream_accepts_bytes_lines():
    acc = protocols.StreamAccumulator('openai_chat')
    acc.feed(b'data: {"choices":[{"delta":{"content":"\xe4\xbd\xa0"}}]}')
    assert acc.content == '你'
