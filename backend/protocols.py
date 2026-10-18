"""三种 API 协议的抹平层（纯函数，无网络、无 flask）。

支持：
- `openai_chat`        —— OpenAI Chat Completions（`/chat/completions`）
- `openai_responses`   —— OpenAI Responses API（`/responses`）
- `anthropic_messages` —— Anthropic Messages（`/messages`）

**设计立场**：内部统一用 OpenAI Chat Completions 的 `messages` 作为中间表示
（`render_messages` 产出的就是它），本模块负责**入站转换**（Chat → 目标协议）
与**出站归一化**（目标协议 → `{content, usage}`）。这样 `prompt` / `parsing` /
`tokens` 三层完全不需要知道协议差异。

差异清单（每一条都有对应实现与本模块内的测试）：

| 维度 | openai_chat | openai_responses | anthropic_messages |
|---|---|---|---|
| 端点 | `/chat/completions` | `/responses` | `/messages`（基址补 `/v1`） |
| 鉴权头 | `Authorization: Bearer` | 同左 | `x-api-key` + `anthropic-version` |
| 消息字段 | `messages[]` | `input[]` | `messages[]`（**无 system**） |
| system | 在 `messages` 内 | 在 `input` 内 | **抽出顶层 `system`** |
| 最大 token | `max_tokens` | `max_output_tokens` | `max_tokens`（**必填**） |
| 停止序列 | `stop` | **不支持（丢弃）** | `stop_sequences` |
| usage | `prompt_tokens`/`completion_tokens` | 同左 | `input_tokens`/`output_tokens` |
| 响应正文 | `choices[0].message.content` | `output[].content[].text` | `content[].text` |
| 流式 | Chat SSE | `response.output_text.delta` | `content_block_delta` |

**`stop` 的坑**：Anthropic 不认 `stop`（它叫 `stop_sequences`），
若只做「其余字段透传」会让 profile 里的停止序列静默失效。
"""

import json
import math
import re
from urllib.parse import urlparse

PROTOCOL_OPENAI_CHAT = 'openai_chat'
PROTOCOL_OPENAI_RESPONSES = 'openai_responses'
PROTOCOL_ANTHROPIC = 'anthropic_messages'

PROTOCOLS = (PROTOCOL_OPENAI_CHAT, PROTOCOL_OPENAI_RESPONSES, PROTOCOL_ANTHROPIC)
DEFAULT_PROTOCOL = PROTOCOL_OPENAI_CHAT

#: 协议 → 端点后缀（拼在去掉已知后缀的基址后面）
ENDPOINT_SUFFIXES = {
    PROTOCOL_OPENAI_CHAT: '/chat/completions',
    PROTOCOL_OPENAI_RESPONSES: '/responses',
    PROTOCOL_ANTHROPIC: '/messages',
}

#: 用户可能把完整端点直接填进 `base_url`，拼之前先剥掉（幂等）
_KNOWN_SUFFIXES = ('/chat/completions', '/responses', '/messages', '/completions')

#: Anthropic 必需的协议版本头
ANTHROPIC_VERSION = '2023-06-01'

#: Anthropic `max_tokens` 必填；缺省时用这个值（远大于典型输出）
ANTHROPIC_DEFAULT_MAX_TOKENS = 4096

#: Responses 的 `max_output_tokens`；缺省时显式给值，避免被网关默认值截断
DEFAULT_MAX_OUTPUT_TOKENS = 4096

#: Anthropic `temperature` 取值域是 [0, 1]，而档案允许 [0, 2]
ANTHROPIC_MAX_TEMPERATURE = 1.0

MAX_STOP_SEQUENCES = 4


def normalize_protocol(value) -> str:
    """把任意输入收敛为合法协议名。未知值回落 `openai_chat`。"""
    text = str(value or '').strip().lower()
    return text if text in PROTOCOLS else DEFAULT_PROTOCOL


# ------------------------------------------------------------------ 端点与鉴权


def _strip_known_suffix(path: str) -> str:
    trimmed = path.rstrip('/')
    for suffix in _KNOWN_SUFFIXES:
        if trimmed.endswith(suffix):
            return trimmed[: -len(suffix)]
    return trimmed


def endpoint_for(base_url, protocol) -> str:
    """拼出目标协议端点（允许用户直接把完整端点填进 `base_url`）。

    - 剥掉已知协议后缀（避免 `/chat/completions/responses` 这类拼接事故）；
    - 路径为空时按协议补默认前缀（Anthropic 要 `/v1`，与官方 SDK 一致）。
    """
    text = str(base_url or '').strip()
    if not text:
        return ''

    target = normalize_protocol(protocol)
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        # 不是完整 URL（例如用户只填了主机名）→ 按字符串拼，交给出站校验拦
        base = _strip_known_suffix(text)
        return f'{base}{ENDPOINT_SUFFIXES[target]}'

    path = _strip_known_suffix(parsed.path or '')
    if not path and target == PROTOCOL_ANTHROPIC:
        path = '/v1'

    rebuilt = parsed._replace(path=f'{path}{ENDPOINT_SUFFIXES[target]}', params='', query='', fragment='')
    return rebuilt.geturl()


def models_endpoint_for(base_url, protocol) -> str:
    """拼出模型列表端点。

    复用 `endpoint_for` 的基址语义：先算出协议端点，再把尾部换掉。
    这样保证 `/models` 与真实请求**同一个基址**（第三方网关常见
    `https://host/v1` 与 `https://host/openai/v1` 两种形态）。
    """
    request_url = endpoint_for(base_url, protocol)
    if not request_url:
        return ''

    parsed = urlparse(request_url)
    path = _strip_known_suffix(parsed.path or '')
    rebuilt = parsed._replace(path=f'{path}/models', params='', query='', fragment='')
    return rebuilt.geturl()


def build_headers(protocol, api_key, extra_headers=None) -> dict:
    """按协议构造鉴权头；`extra_headers` 最后覆盖（用户显式配置优先）。"""
    target = normalize_protocol(protocol)
    secret = str(api_key or '').strip()

    headers = {'Content-Type': 'application/json'}

    if target == PROTOCOL_ANTHROPIC:
        if secret:
            headers['x-api-key'] = secret
        headers['anthropic-version'] = ANTHROPIC_VERSION
    elif secret:
        headers['Authorization'] = f'Bearer {secret}'

    if isinstance(extra_headers, dict):
        for key, value in extra_headers.items():
            name = str(key or '').strip()
            if name:
                headers[name] = str(value)

    return headers


# ------------------------------------------------------------------ 请求体


def _clamp(value, low, high):
    return max(low, min(high, value))


def _anthropic_system_and_messages(messages):
    """把 Chat 形态拆成 Anthropic 的 `(system_blocks, messages)`。

    Anthropic **不接受** `messages` 里的 `system`：必须抽到顶层 `system`。
    另外要求 role 交替，因此相邻同 role 消息会合并（用换行连接）。
    """
    system_texts = []
    converted = []

    for item in messages or ():
        if not isinstance(item, dict):
            continue
        role = str(item.get('role') or 'user').strip().lower()
        content = item.get('content')
        text = '' if content is None else str(content)

        if role == 'system':
            if text.strip():
                system_texts.append(text)
            continue

        # 只有 user / assistant 合法；其它（tool 等）压成 user
        target_role = 'assistant' if role == 'assistant' else 'user'
        if converted and converted[-1]['role'] == target_role:
            converted[-1]['content'] += '\n\n' + text
        else:
            converted.append({'role': target_role, 'content': text})

    # 空消息会被 Anthropic 拒绝
    converted = [
        {'role': item['role'], 'content': item['content']}
        for item in converted if str(item['content']).strip()
    ]

    return system_texts, converted


def build_payload(protocol, messages, *, model, temperature=None, top_p=None,
                  max_tokens=None, stop=None, extra_body=None, stream=False) -> dict:
    """按协议构造请求体（内部中间表示是 Chat `messages`）。"""
    target = normalize_protocol(protocol)
    stops = [str(item) for item in (stop or ()) if str(item or '').strip()][:MAX_STOP_SEQUENCES]

    if target == PROTOCOL_OPENAI_RESPONSES:
        payload = {
            'model': model,
            'input': [
                {
                    'role': str(item.get('role') or 'user'),
                    'content': '' if item.get('content') is None else str(item.get('content')),
                }
                for item in (messages or ()) if isinstance(item, dict)
            ],
        }
        if temperature is not None:
            payload['temperature'] = float(temperature)
        if top_p is not None:
            payload['top_p'] = float(top_p)
        # 不设上限时模型可能被网关默认值截断；显式给一个与 max_tokens 同源的默认
        payload['max_output_tokens'] = int(max_tokens) if max_tokens else DEFAULT_MAX_OUTPUT_TOKENS
        if stream:
            payload['stream'] = True
        # `stop` 在 Responses API 里没有对应字段 → 显式丢弃（不可静默透传）
        if stops:
            payload.setdefault('metadata', {})['st_requested_stop'] = ','.join(stops)

    elif target == PROTOCOL_ANTHROPIC:
        system_texts, converted = _anthropic_system_and_messages(messages)
        payload = {
            'model': model,
            'messages': converted,
            # 必填：缺省会让 Anthropic 直接 400
            'max_tokens': int(max_tokens) if max_tokens else ANTHROPIC_DEFAULT_MAX_TOKENS,
        }
        if system_texts:
            payload['system'] = '\n\n'.join(system_texts)
        if temperature is not None:
            payload['temperature'] = _clamp(
                float(temperature), 0.0, ANTHROPIC_MAX_TEMPERATURE,
            )
        if top_p is not None:
            payload['top_p'] = _clamp(float(top_p), 0.0, 1.0)
        if stops:
            payload['stop_sequences'] = stops
        if stream:
            payload['stream'] = True

    else:
        payload = {
            'model': model,
            'messages': [
                {
                    'role': str(item.get('role') or 'user'),
                    'content': '' if item.get('content') is None else str(item.get('content')),
                }
                for item in (messages or ()) if isinstance(item, dict)
            ],
        }
        if temperature is not None:
            payload['temperature'] = float(temperature)
        if top_p is not None:
            payload['top_p'] = float(top_p)
        if max_tokens:
            payload['max_tokens'] = int(max_tokens)
        if stops:
            payload['stop'] = stops
        if stream:
            payload['stream'] = True
            payload['stream_options'] = {'include_usage': True}

    if isinstance(extra_body, dict):
        payload.update(extra_body)

    return payload


# ------------------------------------------------------------------ 响应归一化


def _text_from_content(value) -> str:
    """把可能是字符串 / 内容块数组的 `content` 压成纯文本。"""
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                # responses: output_text / text；anthropic: text
                text = block.get('text')
                if isinstance(text, str):
                    parts.append(text)
        return ''.join(parts)
    return str(value)


def _usage_pair(usage_raw) -> tuple:
    """从任意协议的 `usage` 里取 `(prompt_tokens, completion_tokens)`。"""
    data = usage_raw if isinstance(usage_raw, dict) else {}

    prompt = data.get('prompt_tokens')
    if prompt is None:
        prompt = data.get('input_tokens')
    if prompt is None:
        prompt = data.get('total_input_tokens')

    completion = data.get('completion_tokens')
    if completion is None:
        completion = data.get('output_tokens')
    if completion is None:
        completion = data.get('total_output_tokens')

    def _to_int(value):
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    return _to_int(prompt), _to_int(completion)


def normalize_usage(usage_raw) -> dict:
    """把任意协议的 usage 归一为 `{prompt_tokens, completion_tokens}`。"""
    prompt, completion = _usage_pair(usage_raw)
    return {'prompt_tokens': prompt, 'completion_tokens': completion}


def parse_response(protocol, payload) -> tuple:
    """解析成功响应，返回 `(content, usage_raw)`；结构不符时抛 `ValueError`。"""
    target = normalize_protocol(protocol)
    data = payload if isinstance(payload, dict) else {}

    if isinstance(data.get('error'), dict):
        message = data['error'].get('message') or json.dumps(data['error'], ensure_ascii=False)
        raise ValueError(f'上游返回错误: {message}')

    usage_raw = data.get('usage') if isinstance(data.get('usage'), dict) else {}

    if target == PROTOCOL_ANTHROPIC:
        blocks = data.get('content')
        if not isinstance(blocks, list):
            raise ValueError('响应缺少 content')
        content = _text_from_content(blocks)
        if not content.strip():
            # 空 content 数组是上游异常（例如被内容过滤拦下），
            # 静默返回空串会让调用方把「空概览」当成有效结果写盘。
            stop_reason = str(data.get('stop_reason') or '')
            raise ValueError(
                f'响应没有文本内容（stop_reason={stop_reason or "未知"}）',
            )
        return content, usage_raw

    if target == PROTOCOL_OPENAI_RESPONSES:
        # 优先用便捷字段（官方会返回），再退回 output 块
        text = data.get('output_text')
        if isinstance(text, str) and text.strip():
            return text, usage_raw

        blocks = data.get('output')
        if not isinstance(blocks, list):
            raise ValueError('响应缺少 output')
        parts = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'message':
                parts.append(_text_from_content(block.get('content')))
            elif block.get('type') == 'output_text' and isinstance(block.get('text'), str):
                parts.append(block['text'])
        content = ''.join(parts)
        if not content:
            raise ValueError('响应缺少文本输出')
        return content, usage_raw

    # openai_chat
    choices = data.get('choices')
    if not isinstance(choices, list) or not choices:
        raise ValueError('响应缺少 choices')
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get('message') if isinstance(first.get('message'), dict) else {}
    content = message.get('content')
    if content is None:
        content = first.get('text')
    if content is None:
        raise ValueError('响应缺少 message.content')
    return _text_from_content(content), usage_raw


def parse_models(protocol, payload) -> list:
    """从 `/models` 响应里取模型 id 列表（兼容三种形态，失败返回空列表）。"""
    target = normalize_protocol(protocol)
    data = payload if isinstance(payload, dict) else {}

    items = data.get('data')
    if not isinstance(items, list):
        items = data.get('models')
    if not isinstance(items, list):
        items = payload if isinstance(payload, list) else []

    result = []
    seen = set()
    for item in items:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = item.get('id') or item.get('name') or item.get('model')
        else:
            continue
        text = str(name or '').strip()
        if not text:
            continue
        # Anthropic / Gemini 会带 `models/` 前缀
        if text.startswith('models/'):
            text = text[len('models/'):]
        if text and text not in seen:
            seen.add(text)
            result.append(text)

    return sorted(result)


# ------------------------------------------------------------------ 流式归一化


class StreamAccumulator:
    """逐行喂入 SSE，累积正文与 usage；协议差异全部收在这里。

    用法：

    ```python
    acc = StreamAccumulator(protocol)
    for line in response.iter_lines(...):
        acc.feed(line)
        if acc.done:
            break
    content, usage_raw = acc.content, acc.usage_raw
    ```
    """

    def __init__(self, protocol):
        self.protocol = normalize_protocol(protocol)
        self._chunks = []
        self.usage_raw = {}
        self.done = False
        self.error = ''
        self._saw_text = False

    # ---- 公共 ----

    @property
    def content(self) -> str:
        return ''.join(self._chunks)

    def feed(self, raw_line) -> None:
        """喂一行 SSE；非 `data:` 行与解析失败的行都被忽略（不抛错）。"""
        if isinstance(raw_line, bytes):
            line = raw_line.decode('utf-8', errors='ignore')
        else:
            line = '' if raw_line is None else str(raw_line)

        line = line.strip()
        if not line or not line.startswith('data:'):
            return

        data = line[len('data:'):].strip()
        if not data:
            return
        if data == '[DONE]':
            self.done = True
            return

        try:
            chunk = json.loads(data)
        except (TypeError, ValueError):
            return
        if not isinstance(chunk, dict):
            return

        if self.protocol == PROTOCOL_ANTHROPIC:
            self._feed_anthropic(chunk)
        elif self.protocol == PROTOCOL_OPENAI_RESPONSES:
            self._feed_responses(chunk)
        else:
            self._feed_openai_chat(chunk)

    # ---- 各协议 ----

    def _feed_openai_chat(self, chunk) -> None:
        if isinstance(chunk.get('usage'), dict):
            self.usage_raw = chunk['usage']
        if isinstance(chunk.get('error'), dict):
            self.error = str(chunk['error'].get('message') or '')
            self.done = True
            return

        choices = chunk.get('choices')
        if not isinstance(choices, list) or not choices:
            return
        first = choices[0] if isinstance(choices[0], dict) else {}
        delta = first.get('delta') if isinstance(first.get('delta'), dict) else {}
        text = delta.get('content')
        if text:
            self._chunks.append(str(text))
            self._saw_text = True
        if first.get('finish_reason'):
            # Chat SSE 的终态由 `[DONE]` 表示；这里不提前收，但要记 usage
            pass

    def _feed_anthropic(self, chunk) -> None:
        event_type = str(chunk.get('type') or '')

        if event_type == 'error':
            error = chunk.get('error') if isinstance(chunk.get('error'), dict) else {}
            self.error = str(error.get('message') or '上游返回错误')
            self.done = True
            return

        if event_type == 'content_block_delta':
            delta = chunk.get('delta') if isinstance(chunk.get('delta'), dict) else {}
            if delta.get('type') == 'text_delta' and isinstance(delta.get('text'), str):
                self._chunks.append(delta['text'])
                self._saw_text = True
            return

        if event_type == 'message_start':
            message = chunk.get('message') if isinstance(chunk.get('message'), dict) else {}
            if isinstance(message.get('usage'), dict):
                self.usage_raw = dict(message['usage'])
            return

        if event_type == 'message_delta':
            usage = chunk.get('usage') if isinstance(chunk.get('usage'), dict) else {}
            if usage:
                merged = dict(self.usage_raw)
                merged.update(usage)
                self.usage_raw = merged
            if chunk.get('delta', {}).get('stop_reason'):
                self.done = True
            return

        if event_type == 'message_stop':
            self.done = True

    def _feed_responses(self, chunk) -> None:
        event_type = str(chunk.get('type') or '')

        if event_type in ('error', 'response.failed'):
            error = chunk.get('error') if isinstance(chunk.get('error'), dict) else {}
            self.error = str(error.get('message') or '上游返回错误')
            self.done = True
            return

        if event_type == 'response.output_text.delta':
            delta = chunk.get('delta')
            if isinstance(delta, str) and delta:
                self._chunks.append(delta)
                self._saw_text = True
            return

        if event_type in ('response.completed', 'response.incomplete'):
            response = chunk.get('response') if isinstance(chunk.get('response'), dict) else {}
            if isinstance(response.get('usage'), dict):
                self.usage_raw = response['usage']
            self.done = True
