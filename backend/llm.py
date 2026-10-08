"""服务端 LLM 客户端（OpenAI 兼容）。

设计文档 §7。要点：

- 密钥只在服务端使用：不进作业 payload、不进日志、不进响应。
- 重试策略：`429` / `5xx` / 超时按 `1s, 2s, 4s` + 抖动退避；`401/403` 与其它 `4xx` 不重试。
- `transport` / `sleep` 可注入，使重试、超时、取消、畸形响应都能在单测中确定性地复现。
- 出站约束：协议白名单 + 私有网段拦截，provider 可显式 `allow_local` 放行本地模型。
- 依赖：`requests` 已在主程序 `requirements.txt` 中（`requests>=2.28.0`），插件不引入新依赖。
"""

import json
import logging
import random
import re
import time
from urllib.parse import urlparse

from .core import tokens
from .core.errors import (
    CODE_INVALID_PAYLOAD,
    CODE_LLM_AUTH_FAILED,
    CODE_LLM_BAD_REQUEST,
    CODE_LLM_BAD_RESPONSE,
    CODE_LLM_RATE_LIMITED,
    CODE_LLM_TIMEOUT,
    CODE_SECRET_MISSING,
    CODE_SSRF_BLOCKED,
    AiAssistantError,
    CancelledError,
)

logger = logging.getLogger(__name__)

RETRY_BASE_DELAYS = (1.0, 2.0, 4.0)
JITTER_SECONDS = 0.3
RESPONSE_EXCERPT_LIMIT = 2048
MAX_STOP_SEQUENCES = 4

RETRYABLE_CODES = frozenset({CODE_LLM_RATE_LIMITED, CODE_LLM_TIMEOUT, CODE_LLM_BAD_RESPONSE})

BLOCKED_HOSTS = frozenset({'localhost', 'localhost.localdomain', '0.0.0.0', '::1'})
PRIVATE_HOST_PATTERNS = (
    re.compile(r'^127\.'),
    re.compile(r'^10\.'),
    re.compile(r'^192\.168\.'),
    re.compile(r'^169\.254\.'),
    re.compile(r'^172\.(1[6-9]|2\d|3[01])\.'),
)


# ------------------------------------------------------------------ 出站约束


def validate_base_url(base_url, *, allow_local: bool = False) -> str:
    """校验 `base_url`；返回错误消息（空串表示通过）。"""
    text = str(base_url or '').strip()
    if not text:
        return 'base_url 不能为空'

    parsed = urlparse(text)
    if parsed.scheme not in ('http', 'https'):
        return f'不支持的协议: {parsed.scheme or "(空)"}（仅允许 http/https）'

    host = (parsed.hostname or '').lower()
    if not host:
        return 'base_url 缺少主机名'

    if allow_local:
        return ''

    if host in BLOCKED_HOSTS or host.endswith('.local') or host.endswith('.internal'):
        return f'已阻止本地/内网地址: {host}（本地模型请在 provider 中开启「允许本地地址」）'

    for pattern in PRIVATE_HOST_PATTERNS:
        if pattern.search(host):
            return f'已阻止私有网段地址: {host}（本地模型请在 provider 中开启「允许本地地址」）'

    if ':' in host:
        return f'已阻止 IPv6 字面量地址: {host}（如确需，请开启「允许本地地址」）'

    return ''


def endpoint_for(base_url: str) -> str:
    """拼出 chat completions 端点（允许用户直接填完整端点）。"""
    text = str(base_url or '').strip().rstrip('/')
    if not text:
        return ''
    if text.endswith('/chat/completions'):
        return text
    return f'{text}/chat/completions'


# ------------------------------------------------------------------ 内部工具


def _excerpt(text, limit: int = RESPONSE_EXCERPT_LIMIT) -> str:
    source = '' if text is None else str(text)
    return source[:limit]


def _default_transport(url, **kwargs):
    import requests  # noqa: PLC0415 - 延迟导入，便于无网络环境下加载模块

    return requests.post(url, **kwargs)


def _delay_for(attempt: int) -> float:
    base = RETRY_BASE_DELAYS[min(max(0, attempt), len(RETRY_BASE_DELAYS) - 1)]
    return base + random.uniform(0, JITTER_SECONDS)


def _wrap_transport_error(exc: Exception) -> AiAssistantError:
    name = type(exc).__name__
    text = str(exc)
    lowered = f'{name} {text}'.lower()
    if 'timeout' in lowered or 'timed out' in lowered:
        return AiAssistantError(CODE_LLM_TIMEOUT, f'请求超时: {name}: {text}', http_status=504)
    return AiAssistantError(CODE_LLM_BAD_RESPONSE, f'网络错误: {name}: {text}', http_status=502)


def _classify_status(status: int, response) -> AiAssistantError:
    body = _excerpt(getattr(response, 'text', '') or '')

    if status in (401, 403):
        return AiAssistantError(
            CODE_LLM_AUTH_FAILED, f'认证失败（HTTP {status}），请检查 API Key',
            http_status=401, detail={'status': status},
        )
    if status == 429:
        return AiAssistantError(
            CODE_LLM_RATE_LIMITED, '触发供应商限流（HTTP 429）', http_status=429,
            detail={'excerpt': body},
        )
    if status >= 500:
        return AiAssistantError(
            CODE_LLM_BAD_RESPONSE, f'供应商服务端错误（HTTP {status}）', http_status=502,
            detail={'status': status, 'excerpt': body},
        )
    return AiAssistantError(
        CODE_LLM_BAD_REQUEST, f'请求被拒绝（HTTP {status}）',
        detail={'status': status, 'excerpt': body},
    )


def _build_payload(messages, *, model: str, temperature, top_p, max_tokens, stop,
                   extra_body, stream: bool) -> dict:
    payload = {'model': model, 'messages': list(messages or [])}

    if temperature is not None:
        payload['temperature'] = float(temperature)
    if top_p is not None:
        payload['top_p'] = float(top_p)
    if max_tokens:
        payload['max_tokens'] = int(max_tokens)
    if stop:
        payload['stop'] = [str(item) for item in stop][:MAX_STOP_SEQUENCES]
    if stream:
        payload['stream'] = True
        payload['stream_options'] = {'include_usage': True}
    if isinstance(extra_body, dict):
        payload.update(extra_body)

    return payload


def _parse_success(response) -> tuple:
    """解析成功响应，返回 `(content, usage_raw)`。"""
    payload = None
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - 有些网关返回非 JSON
        raw = getattr(response, 'text', '') or ''
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise AiAssistantError(
                CODE_LLM_BAD_RESPONSE, f'响应不是合法 JSON: {exc}', http_status=502,
                detail={'excerpt': _excerpt(raw)},
            ) from exc

    if not isinstance(payload, dict):
        raise AiAssistantError(
            CODE_LLM_BAD_RESPONSE, '响应结构异常（不是对象）', http_status=502,
            detail={'excerpt': _excerpt(json.dumps(payload, ensure_ascii=False))},
        )

    choices = payload.get('choices')
    if not isinstance(choices, list) or not choices:
        raise AiAssistantError(
            CODE_LLM_BAD_RESPONSE, '响应缺少 choices', http_status=502,
            detail={'excerpt': _excerpt(json.dumps(payload, ensure_ascii=False))},
        )

    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get('message') if isinstance(first.get('message'), dict) else {}
    content = message.get('content')
    if content is None:
        content = first.get('text')
    if content is None:
        raise AiAssistantError(CODE_LLM_BAD_RESPONSE, '响应缺少 message.content', http_status=502)

    usage_raw = payload.get('usage') if isinstance(payload.get('usage'), dict) else {}
    return str(content), usage_raw


def _normalize_usage(usage_raw, messages, content) -> tuple:
    """返回 `(usage, estimated)`；供应商未返回用量时按公式估算。"""
    prompt_tokens = int(
        usage_raw.get('prompt_tokens') or usage_raw.get('input_tokens') or 0
    )
    completion_tokens = int(
        usage_raw.get('completion_tokens') or usage_raw.get('output_tokens') or 0
    )

    if prompt_tokens or completion_tokens:
        return {'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens}, False

    return {
        'prompt_tokens': tokens.estimate_messages(messages)['total'],
        'completion_tokens': tokens.estimate(content),
    }, True


# ------------------------------------------------------------------ 主入口


def chat(messages, *, provider, api_key, extra_headers=None, model: str = '',
         temperature=None, top_p=None, max_tokens=None, stop=None, extra_body=None,
         cancel_check=None, transport=None, sleep=None) -> dict:
    """发起一次 chat 调用。

    Args:
        messages: OpenAI 风格的 messages。
        provider: provider 配置（含 `base_url` / `model` / 超时 / `max_retries` / `use_stream`）。
        api_key: 明文密钥（仅在本进程内使用）。
        extra_headers: provider 的自定义请求头。
        cancel_check: 无参回调，返回 True 表示应尽快中止。
        transport: 注入点，签名与 `requests.post` 一致。
        sleep: 注入点，签名与 `time.sleep` 一致。

    Returns:
        `{'content', 'usage', 'usage_estimated', 'model', 'latency_ms', 'attempts'}`
    """
    spec = provider if isinstance(provider, dict) else {}
    base_url = str(spec.get('base_url') or '')
    allow_local = bool(spec.get('allow_local'))

    blocked = validate_base_url(base_url, allow_local=allow_local)
    if blocked:
        raise AiAssistantError(CODE_SSRF_BLOCKED, blocked, detail={'base_url': base_url})

    secret = str(api_key or '').strip()
    if not secret:
        raise AiAssistantError(CODE_SECRET_MISSING, '缺少 API Key', http_status=409)

    target_model = str(model or spec.get('model') or '').strip()
    if not target_model:
        raise AiAssistantError(CODE_INVALID_PAYLOAD, '缺少模型名（provider 与请求都未指定）')

    use_stream = bool(spec.get('use_stream'))
    payload = _build_payload(
        messages, model=target_model, temperature=temperature, top_p=top_p,
        max_tokens=max_tokens, stop=stop, extra_body=extra_body, stream=use_stream,
    )

    headers = {'Authorization': f'Bearer {secret}', 'Content-Type': 'application/json'}
    if isinstance(extra_headers, dict):
        for key, value in extra_headers.items():
            headers[str(key)] = str(value)

    url = endpoint_for(base_url)
    max_retries = max(0, int(spec.get('max_retries') or 0))
    timeout = (
        max(1, int(spec.get('connect_timeout') or 10)),
        max(5, int(spec.get('request_timeout') or 120)),
    )

    post = transport or _default_transport
    do_sleep = sleep or time.sleep
    started = time.monotonic()
    last_error = None

    for attempt in range(max_retries + 1):
        if cancel_check is not None and cancel_check():
            raise CancelledError('已取消')

        try:
            if use_stream:
                return _chat_stream(
                    post, url, headers=headers, payload=payload, timeout=timeout,
                    messages=messages, model=target_model, cancel_check=cancel_check,
                    started=started, attempt=attempt,
                )

            response = post(url, headers=headers, json=payload, timeout=timeout)
            status = int(getattr(response, 'status_code', 0) or 0)

            if status >= 400:
                raise _classify_status(status, response)

            content, usage_raw = _parse_success(response)
            usage, estimated = _normalize_usage(usage_raw, messages, content)
            return {
                'content': content,
                'usage': usage,
                'usage_estimated': estimated,
                'model': target_model,
                'latency_ms': int((time.monotonic() - started) * 1000),
                'attempts': attempt + 1,
            }

        except CancelledError:
            raise
        except AiAssistantError as exc:
            if exc.code in RETRYABLE_CODES and attempt < max_retries:
                last_error = exc
                logger.info('LLM 调用失败（%s），第 %s 次重试', exc.code, attempt + 1)
                do_sleep(_delay_for(attempt))
                continue
            raise
        except Exception as exc:  # noqa: BLE001 - 传输层异常统一归类
            wrapped = _wrap_transport_error(exc)
            if attempt < max_retries:
                last_error = wrapped
                logger.info('LLM 传输异常（%s），第 %s 次重试', wrapped.code, attempt + 1)
                do_sleep(_delay_for(attempt))
                continue
            raise wrapped from exc

    raise last_error or AiAssistantError(CODE_LLM_BAD_RESPONSE, 'LLM 调用失败', http_status=502)


def _chat_stream(post, url, *, headers, payload, timeout, messages, model,
                 cancel_check, started, attempt) -> dict:
    """流式调用：逐块累积内容，并在每个数据块之间检查取消。"""
    response = post(url, headers=headers, json=payload, timeout=timeout, stream=True)
    status = int(getattr(response, 'status_code', 0) or 0)
    if status >= 400:
        raise _classify_status(status, response)

    chunks = []
    usage_raw = {}

    for raw_line in response.iter_lines(decode_unicode=True):
        if cancel_check is not None and cancel_check():
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass
            raise CancelledError('已取消')

        line = raw_line.decode('utf-8', errors='ignore') if isinstance(raw_line, bytes) else raw_line
        line = str(line or '').strip()
        if not line or not line.startswith('data:'):
            continue

        data = line[len('data:'):].strip()
        if data == '[DONE]':
            break

        try:
            chunk = json.loads(data)
        except (TypeError, ValueError):
            continue

        if isinstance(chunk.get('usage'), dict):
            usage_raw = chunk['usage']

        choices = chunk.get('choices')
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            delta = choices[0].get('delta') if isinstance(choices[0].get('delta'), dict) else {}
            text = delta.get('content')
            if text:
                chunks.append(str(text))

    content = ''.join(chunks)
    if not content.strip():
        raise AiAssistantError(CODE_LLM_BAD_RESPONSE, '流式响应为空', http_status=502)

    usage, estimated = _normalize_usage(usage_raw, messages, content)
    return {
        'content': content,
        'usage': usage,
        'usage_estimated': estimated,
        'model': model,
        'latency_ms': int((time.monotonic() - started) * 1000),
        'attempts': attempt + 1,
    }


def probe(provider, api_key, *, model: str = '', transport=None, sleep=None) -> dict:
    """连通性自检：发一条最小请求，返回耗时与响应片段。"""
    spec = dict(provider or {})
    spec['max_retries'] = 0
    spec['use_stream'] = False
    spec['request_timeout'] = min(int(spec.get('request_timeout') or 120), 30)

    started = time.monotonic()
    result = chat(
        [{'role': 'user', 'content': 'ping'}],
        provider=spec, api_key=api_key, model=model, max_tokens=8,
        transport=transport, sleep=sleep,
    )

    return {
        'ok': True,
        'latency_ms': int((time.monotonic() - started) * 1000),
        'model': result['model'],
        'preview': str(result['content'])[:80],
        'usage': result['usage'],
    }
