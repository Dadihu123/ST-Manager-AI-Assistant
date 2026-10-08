"""LLM 客户端：重试、退避、取消、用量、出站约束（全部打桩，不产生真实网络请求）。"""

import json

import pytest

from conftest import load_backend_submodule

pytest.importorskip('flask', reason='装配层依赖 flask（主程序运行环境必备）')
llm = load_backend_submodule('llm')
errors = load_backend_submodule('core.errors')


PROVIDER = {
    'id': 'test',
    'base_url': 'https://api.example.com/v1',
    'model': 'test-model',
    'connect_timeout': 5,
    'request_timeout': 30,
    'max_retries': 2,
    'use_stream': False,
    'allow_local': False,
}

MESSAGES = [{'role': 'user', 'content': '你好'}]


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text='', lines=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else json.dumps(payload or {}, ensure_ascii=False)
        self._lines = lines or []
        self.closed = False

    def json(self):
        if self._payload is None:
            raise ValueError('no json')
        return self._payload

    def iter_lines(self, decode_unicode=False):
        for line in self._lines:
            yield line

    def close(self):
        self.closed = True


class FakeTransport:
    """按顺序返回预设响应或抛出预设异常，并记录调用。"""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({'url': url, 'kwargs': kwargs})
        if not self._outcomes:
            raise AssertionError('FakeTransport 没有更多预设响应')
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def ok_payload(content='{"summary": "S", "tags": []}', usage=None):
    payload = {
        'choices': [{'message': {'content': content}}],
        'model': 'test-model',
    }
    if usage is not None:
        payload['usage'] = usage
    return payload


def make_sleep():
    delays = []
    return delays, delays.append


def run(transport, **overrides):
    delays, sleep = make_sleep()
    kwargs = {
        'provider': PROVIDER,
        'api_key': 'sk-test',
        'transport': transport,
        'sleep': sleep,
    }
    kwargs.update(overrides)
    return llm.chat(MESSAGES, **kwargs), delays


# ------------------------------------------------------------------ 出站约束


@pytest.mark.parametrize('base_url,allow_local,should_fail', [
    ('https://api.openai.com/v1', False, False),
    ('http://127.0.0.1:11434/v1', False, True),
    ('http://localhost:11434/v1', False, True),
    ('http://192.168.1.10/v1', False, True),
    ('http://10.0.0.5/v1', False, True),
    ('http://172.16.0.1/v1', False, True),
    ('http://169.254.169.254/v1', False, True),
    ('file:///etc/passwd', False, True),
    ('ftp://example.com/v1', False, True),
    ('http://127.0.0.1:11434/v1', True, False),
])
def test_validate_base_url(base_url, allow_local, should_fail):
    message = llm.validate_base_url(base_url, allow_local=allow_local)
    assert bool(message) is should_fail


def test_blocked_url_raises_ssrf_error():
    transport = FakeTransport([])
    with pytest.raises(errors.AiAssistantError) as excinfo:
        llm.chat(MESSAGES, provider=dict(PROVIDER, base_url='http://127.0.0.1:8080/v1'),
                 api_key='k', transport=transport)
    assert excinfo.value.code == errors.CODE_SSRF_BLOCKED
    assert transport.calls == []


def test_endpoint_for_appends_suffix_once():
    assert llm.endpoint_for('https://a/v1') == 'https://a/v1/chat/completions'
    assert llm.endpoint_for('https://a/v1/') == 'https://a/v1/chat/completions'
    assert llm.endpoint_for('https://a/v1/chat/completions') == 'https://a/v1/chat/completions'


def test_missing_api_key_is_rejected():
    with pytest.raises(errors.AiAssistantError) as excinfo:
        llm.chat(MESSAGES, provider=PROVIDER, api_key='', transport=FakeTransport([]))
    assert excinfo.value.code == errors.CODE_SECRET_MISSING


def test_missing_model_is_rejected():
    with pytest.raises(errors.AiAssistantError) as excinfo:
        llm.chat(MESSAGES, provider=dict(PROVIDER, model=''), api_key='k',
                 transport=FakeTransport([]))
    assert excinfo.value.code == errors.CODE_INVALID_PAYLOAD


# ------------------------------------------------------------------ 正常路径


def test_successful_call_returns_content_and_usage():
    transport = FakeTransport([FakeResponse(payload=ok_payload(
        usage={'prompt_tokens': 12, 'completion_tokens': 34}))])
    result, delays = run(transport)

    assert result['content'] == '{"summary": "S", "tags": []}'
    assert result['usage'] == {'prompt_tokens': 12, 'completion_tokens': 34}
    assert result['usage_estimated'] is False
    assert result['attempts'] == 1
    assert delays == []

    call = transport.calls[0]
    assert call['url'] == 'https://api.example.com/v1/chat/completions'
    assert call['kwargs']['headers']['Authorization'] == 'Bearer sk-test'
    assert call['kwargs']['json']['model'] == 'test-model'
    assert call['kwargs']['json']['messages'] == MESSAGES


def test_usage_is_estimated_when_provider_omits_it():
    transport = FakeTransport([FakeResponse(payload=ok_payload())])
    result, _delays = run(transport)
    assert result['usage_estimated'] is True
    assert result['usage']['prompt_tokens'] > 0
    assert result['usage']['completion_tokens'] > 0


def test_extra_headers_are_merged():
    transport = FakeTransport([FakeResponse(payload=ok_payload())])
    run(transport, extra_headers={'X-Title': 'ST-Manager'})
    headers = transport.calls[0]['kwargs']['headers']
    assert headers['X-Title'] == 'ST-Manager'
    assert headers['Authorization'] == 'Bearer sk-test'


def test_stop_and_generation_params_are_passed():
    transport = FakeTransport([FakeResponse(payload=ok_payload())])
    run(transport, temperature=0.3, top_p=0.9, max_tokens=128, stop=['A', 'B'])
    payload = transport.calls[0]['kwargs']['json']
    assert payload['temperature'] == 0.3
    assert payload['top_p'] == 0.9
    assert payload['max_tokens'] == 128
    assert payload['stop'] == ['A', 'B']


def test_extra_body_is_merged_into_request():
    transport = FakeTransport([FakeResponse(payload=ok_payload())])
    run(transport, extra_body={'thinking': {'type': 'enabled'}})
    assert transport.calls[0]['kwargs']['json']['thinking'] == {'type': 'enabled'}


# ------------------------------------------------------------------ 重试与退避


def test_rate_limit_is_retried_then_succeeds():
    transport = FakeTransport([
        FakeResponse(status_code=429, payload={'error': 'slow down'}),
        FakeResponse(payload=ok_payload()),
    ])
    result, delays = run(transport)

    assert result['attempts'] == 2
    assert len(delays) == 1
    assert 1.0 <= delays[0] <= 1.0 + llm.JITTER_SECONDS


def test_server_error_is_retried():
    transport = FakeTransport([
        FakeResponse(status_code=503, payload={'error': 'unavailable'}),
        FakeResponse(payload=ok_payload()),
    ])
    result, delays = run(transport)
    assert result['attempts'] == 2
    assert len(delays) == 1


def test_retry_exhaustion_raises_rate_limited():
    transport = FakeTransport([
        FakeResponse(status_code=429, payload={}),
        FakeResponse(status_code=429, payload={}),
        FakeResponse(status_code=429, payload={}),
    ])
    with pytest.raises(errors.AiAssistantError) as excinfo:
        run(transport)
    assert excinfo.value.code == errors.CODE_LLM_RATE_LIMITED
    assert len(transport.calls) == 3  # max_retries=2 → 共 3 次尝试


def test_backoff_grows_across_attempts():
    transport = FakeTransport([
        FakeResponse(status_code=500, payload={}),
        FakeResponse(status_code=500, payload={}),
        FakeResponse(payload=ok_payload()),
    ])
    _result, delays = run(transport)
    assert len(delays) == 2
    assert delays[1] > delays[0]


def test_auth_failure_is_not_retried():
    transport = FakeTransport([FakeResponse(status_code=401, payload={})])
    with pytest.raises(errors.AiAssistantError) as excinfo:
        run(transport)
    assert excinfo.value.code == errors.CODE_LLM_AUTH_FAILED
    assert len(transport.calls) == 1


def test_forbidden_is_not_retried():
    transport = FakeTransport([FakeResponse(status_code=403, payload={})])
    with pytest.raises(errors.AiAssistantError) as excinfo:
        run(transport)
    assert excinfo.value.code == errors.CODE_LLM_AUTH_FAILED
    assert len(transport.calls) == 1


def test_bad_request_is_not_retried():
    transport = FakeTransport([FakeResponse(status_code=422, payload={'error': 'bad'})])
    with pytest.raises(errors.AiAssistantError) as excinfo:
        run(transport)
    assert excinfo.value.code == errors.CODE_LLM_BAD_REQUEST
    assert len(transport.calls) == 1


def test_timeout_is_classified_and_retried():
    transport = FakeTransport([TimeoutError('read timeout'), FakeResponse(payload=ok_payload())])
    result, delays = run(transport)
    assert result['attempts'] == 2
    assert len(delays) == 1


def test_timeout_exhaustion_raises_llm_timeout():
    transport = FakeTransport([
        TimeoutError('read timeout'),
        TimeoutError('read timeout'),
        TimeoutError('read timeout'),
    ])
    with pytest.raises(errors.AiAssistantError) as excinfo:
        run(transport)
    assert excinfo.value.code == errors.CODE_LLM_TIMEOUT


def test_non_timeout_transport_error_is_bad_response():
    transport = FakeTransport([ConnectionResetError('reset')] * 3)
    with pytest.raises(errors.AiAssistantError) as excinfo:
        run(transport)
    assert excinfo.value.code == errors.CODE_LLM_BAD_RESPONSE


def test_max_retries_zero_means_single_attempt():
    transport = FakeTransport([FakeResponse(status_code=500, payload={})])
    with pytest.raises(errors.AiAssistantError):
        run(transport, provider=dict(PROVIDER, max_retries=0))
    assert len(transport.calls) == 1


# ------------------------------------------------------------------ 取消


def test_cancel_before_request_raises_cancelled():
    transport = FakeTransport([])
    with pytest.raises(errors.CancelledError):
        run(transport, cancel_check=lambda: True)
    assert transport.calls == []


def test_cancel_between_retries_stops_immediately():
    transport = FakeTransport([FakeResponse(status_code=429, payload={})])
    calls = {'count': 0}

    def cancel_check():
        calls['count'] += 1
        return calls['count'] > 1

    with pytest.raises(errors.CancelledError):
        run(transport, cancel_check=cancel_check)
    assert len(transport.calls) == 1


# ------------------------------------------------------------------ 响应畸形


def test_non_json_response_raises_bad_response():
    response = FakeResponse(payload=None, text='<html>gateway error</html>')
    transport = FakeTransport([response] * 3)
    with pytest.raises(errors.AiAssistantError) as excinfo:
        run(transport)
    assert excinfo.value.code == errors.CODE_LLM_BAD_RESPONSE
    assert 'gateway error' in json.dumps(excinfo.value.detail, ensure_ascii=False)


def test_missing_choices_raises_bad_response():
    transport = FakeTransport([FakeResponse(payload={'id': 'x'})] * 3)
    with pytest.raises(errors.AiAssistantError) as excinfo:
        run(transport)
    assert excinfo.value.code == errors.CODE_LLM_BAD_RESPONSE


def test_missing_content_raises_bad_response():
    transport = FakeTransport([FakeResponse(payload={'choices': [{'message': {}}]})] * 3)
    with pytest.raises(errors.AiAssistantError) as excinfo:
        run(transport)
    assert excinfo.value.code == errors.CODE_LLM_BAD_RESPONSE


def test_text_field_is_accepted_as_content():
    transport = FakeTransport([FakeResponse(payload={'choices': [{'text': 'legacy'}]})])
    result, _delays = run(transport)
    assert result['content'] == 'legacy'


# ------------------------------------------------------------------ 流式


def test_streaming_accumulates_deltas():
    lines = [
        'data: {"choices":[{"delta":{"content":"{\\"summary\\":"}}]}',
        'data: {"choices":[{"delta":{"content":"\\"S\\"}"}}]}',
        'data: {"usage":{"prompt_tokens":5,"completion_tokens":7}}',
        'data: [DONE]',
    ]
    transport = FakeTransport([FakeResponse(payload={}, lines=lines)])
    result, _delays = run(transport, provider=dict(PROVIDER, use_stream=True))

    assert result['content'] == '{"summary":"S"}'
    assert result['usage'] == {'prompt_tokens': 5, 'completion_tokens': 7}


def test_streaming_cancel_closes_response():
    lines = ['data: {"choices":[{"delta":{"content":"a"}}]}'] * 5
    response = FakeResponse(payload={}, lines=lines)
    transport = FakeTransport([response])
    calls = {'count': 0}

    def cancel_check():
        calls['count'] += 1
        return calls['count'] > 2

    with pytest.raises(errors.CancelledError):
        run(transport, provider=dict(PROVIDER, use_stream=True), cancel_check=cancel_check)
    assert response.closed is True


def test_streaming_empty_response_raises():
    transport = FakeTransport([FakeResponse(payload={}, lines=['data: [DONE]'])])
    with pytest.raises(errors.AiAssistantError) as excinfo:
        run(transport, provider=dict(PROVIDER, use_stream=True))
    assert excinfo.value.code == errors.CODE_LLM_BAD_RESPONSE


# ------------------------------------------------------------------ probe


def test_probe_reports_latency_and_preview():
    transport = FakeTransport([FakeResponse(payload=ok_payload(content='pong'))])
    result = llm.probe(PROVIDER, 'sk-test', transport=transport, sleep=lambda _s: None)
    assert result['ok'] is True
    assert result['preview'] == 'pong'
    assert result['latency_ms'] >= 0
    assert transport.calls[0]['kwargs']['json']['max_tokens'] == 8
