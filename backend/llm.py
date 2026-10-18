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

from . import protocols
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


def endpoint_for(base_url: str, protocol: str = protocols.DEFAULT_PROTOCOL) -> str:
    """拼出目标协议的请求端点（协议感知，委托 `protocols.endpoint_for`）。"""
    return protocols.endpoint_for(base_url, protocol)


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


def _parse_success(response, protocol: str = protocols.DEFAULT_PROTOCOL) -> tuple:
    """解析成功响应，返回 `(content, usage_raw)`（协议差异委托 protocols 层）。"""
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

    try:
        return protocols.parse_response(protocol, payload)
    except ValueError as exc:
        raise AiAssistantError(
            CODE_LLM_BAD_RESPONSE, str(exc), http_status=502,
            detail={'excerpt': _excerpt(json.dumps(payload, ensure_ascii=False))},
        ) from exc


def _normalize_usage(usage_raw, messages, content) -> tuple:
    """返回 `(usage, estimated)`；供应商未返回用量时按公式估算。

    usage 字段名差异（`prompt_tokens` vs `input_tokens` vs `total_input_tokens`）
    由 `protocols.normalize_usage` 统一处理。
    """
    usage = protocols.normalize_usage(usage_raw)
    if usage['prompt_tokens'] or usage['completion_tokens']:
        return usage, False

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
    protocol = protocols.normalize_protocol(spec.get('api_style'))

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
    payload = protocols.build_payload(
        protocol, messages, model=target_model, temperature=temperature, top_p=top_p,
        max_tokens=max_tokens, stop=stop, extra_body=extra_body, stream=use_stream,
    )

    headers = protocols.build_headers(protocol, secret, extra_headers)

    url = protocols.endpoint_for(base_url, protocol)
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
                    started=started, attempt=attempt, protocol=protocol,
                )

            response = post(url, headers=headers, json=payload, timeout=timeout)
            status = int(getattr(response, 'status_code', 0) or 0)

            if status >= 400:
                raise _classify_status(status, response)

            content, usage_raw = _parse_success(response, protocol)
            usage, estimated = _normalize_usage(usage_raw, messages, content)
            return {
                'content': content,
                'usage': usage,
                'usage_estimated': estimated,
                'model': target_model,
                'protocol': protocol,
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
                 cancel_check, started, attempt, protocol) -> dict:
    """流式调用：逐块累积内容，并在每个数据块之间检查取消。

    协议差异（Chat SSE / Anthropic `content_block_delta` / Responses
    `response.output_text.delta`）全部由 `protocols.StreamAccumulator` 吸收。
    """
    response = post(url, headers=headers, json=payload, timeout=timeout, stream=True)
    status = int(getattr(response, 'status_code', 0) or 0)
    if status >= 400:
        raise _classify_status(status, response)

    accumulator = protocols.StreamAccumulator(protocol)

    for raw_line in response.iter_lines(decode_unicode=True):
        if cancel_check is not None and cancel_check():
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass
            raise CancelledError('已取消')

        accumulator.feed(raw_line)

        if accumulator.error:
            if accumulator.content.strip():
                # 已拿到部分正文：当作成功（与 Chat SSE 的截断行为一致）
                logger.warning('流式响应中途报错，已保留 %s 字正文', len(accumulator.content))
                break
            raise AiAssistantError(
                CODE_LLM_BAD_RESPONSE, f'流式响应报错: {accumulator.error}', http_status=502,
            )

        if accumulator.done:
            break

    content = accumulator.content
    if not content.strip():
        raise AiAssistantError(CODE_LLM_BAD_RESPONSE, '流式响应为空', http_status=502)

    usage, estimated = _normalize_usage(accumulator.usage_raw, messages, content)
    return {
        'content': content,
        'usage': usage,
        'usage_estimated': estimated,
        'model': model,
        'protocol': protocol,
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
        'protocol': result.get('protocol', protocols.DEFAULT_PROTOCOL),
        'preview': str(result['content'])[:80],
        'usage': result['usage'],
    }


def list_models(provider, api_key, *, extra_headers=None, transport=None) -> dict:
    """拉取供应商的模型列表（`GET /models`）。

    很多第三方网关**不实现** `/models`，因此本函数**永不抛异常**：
    失败时返回 `{'ok': False, 'models': [], 'message': ...}`，
    由 UI 退回手填模型名。这一点很重要 —— 拉取模型不该阻塞配置。

    Returns:
        `{'ok': bool, 'models': [str], 'protocol': str, 'message': str}`
    """
    spec = provider if isinstance(provider, dict) else {}
    base_url = str(spec.get('base_url') or '')
    allow_local = bool(spec.get('allow_local'))
    protocol = protocols.normalize_protocol(spec.get('api_style'))

    def _fail(message: str) -> dict:
        return {'ok': False, 'models': [], 'protocol': protocol, 'message': message}

    blocked = validate_base_url(base_url, allow_local=allow_local)
    if blocked:
        return _fail(blocked)

    secret = str(api_key or '').strip()
    if not secret:
        return _fail('缺少 API Key')

    url = protocols.models_endpoint_for(base_url, protocol)
    if not url:
        return _fail('base_url 为空，无法推导 /models 端点')

    headers = protocols.build_headers(protocol, secret, extra_headers)
    timeout = (
        max(1, int(spec.get('connect_timeout') or 10)),
        min(max(5, int(spec.get('request_timeout') or 120)), 30),
    )

    get = transport or _default_get
    try:
        response = get(url, headers=headers, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 拉取模型失败不应抛给调用方
        return _fail(_wrap_transport_error(exc).message)

    status = int(getattr(response, 'status_code', 0) or 0)
    if status >= 400:
        excerpt = _excerpt(getattr(response, 'text', '') or '', 200)
        return _fail(f'HTTP {status}：{excerpt or "无响应体"}')

    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        raw = getattr(response, 'text', '') or ''
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return _fail('响应不是合法 JSON')

    models = protocols.parse_models(protocol, payload)
    if not models:
        return _fail('响应里没有可识别的模型列表')

    return {'ok': True, 'models': models, 'protocol': protocol, 'message': ''}


def _default_get(url, **kwargs):
    import requests  # noqa: PLC0415 - 延迟导入，便于无网络环境下加载模块

    return requests.get(url, **kwargs)
