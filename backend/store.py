"""持久化层：提示词档案、provider、卡片元数据、标签建议队列、用量台账。

设计要点（设计文档 §4.2 / §4.3）：
- 大对象一律落 `api.data_dir()` 下的文件；`ui_data` 只放索引与热缓存。
- 所有 `ui_data` 写入都通过 `_mutate()`：mutator **只原地修改子键并返回 `None`**。
  这是硬性约定——`PluginAPI.ui_data_update` 在 mutator 返回 dict 时会
  `entry.clear()` 后整体替换（`core/plugins/api.py:892-897`），会把 `config` /
  `enabled` / `load_error` 一起清掉。
- 写 `data/plugins/<id>/data/` 不需要 `suppress_fs_events()`（该目录不在扫描器监视范围）。
"""

import json
import logging
import os
import re
import shutil
import time
from urllib.parse import urlparse

from .core import profiles as profiles_core
from .core.errors import (
    CODE_INVALID_PAYLOAD,
    CODE_PROFILE_INVALID,
    CODE_PROFILE_NOT_FOUND,
    CODE_PROVIDER_NOT_FOUND,
    CODE_WRITE_FAILED,
    AiAssistantError,
)

logger = logging.getLogger(__name__)

PROMPT_DIRNAME = 'prompts'
CACHE_DIRNAME = 'cache'
USAGE_DIRNAME = 'usage'
BACKUP_DIRNAME = '.backup'

PROVIDERS_FILENAME = 'providers.json'
CARD_META_FILENAME = 'card_meta.jsonl'
USAGE_FILENAME = 'usage.jsonl'

CARD_META_UI_LIMIT = 800
CARD_META_UI_BYTES_LIMIT = 200 * 1024
JSONL_COMPACT_BYTES = 8 * 1024 * 1024

PROVIDER_ID_PATTERN = re.compile(r'^[a-z0-9][a-z0-9-]{0,31}$')
DEFAULT_BASE_URL = 'https://api.openai.com/v1'
API_STYLES = ('openai', 'openai_compatible')

API_KEY_SECRET_TEMPLATE = 'provider__{provider_id}__api_key'
EXTRA_HEADERS_SECRET_TEMPLATE = 'provider__{provider_id}__extra_headers'


def api_key_secret_name(provider_id: str) -> str:
    """provider 的 API Key 在 `secrets.json` 中的键名。"""
    return API_KEY_SECRET_TEMPLATE.format(provider_id=str(provider_id or '').strip().lower())


def extra_headers_secret_name(provider_id: str) -> str:
    """provider 的自定义请求头在 `secrets.json` 中的键名（可能含密钥，故按密钥存储）。"""
    return EXTRA_HEADERS_SECRET_TEMPLATE.format(provider_id=str(provider_id or '').strip().lower())


def _to_int(value, default, *, minimum=None, maximum=None):
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def provider_id_from_base_url(base_url: str) -> str:
    """从 `base_url` 推导 provider id（`api.openai.com` → `openai`）。"""
    text = str(base_url or '').strip()
    if '://' not in text:
        text = f'https://{text}' if text else ''
    host = urlparse(text).hostname or ''
    parts = [part for part in host.split('.') if part and part not in ('www', 'api')]
    candidate = re.sub(r'[^a-z0-9-]+', '-', (parts[0] if parts else 'default').lower()).strip('-')
    return (candidate or 'default')[:32]


def normalize_provider(raw) -> dict:
    """规范化 provider 结构（坏值回落默认，不做校验失败）。"""
    data = raw if isinstance(raw, dict) else {}
    provider_id = str(data.get('id') or '').strip().lower()
    api_style = str(data.get('api_style') or 'openai').strip().lower()
    if api_style not in API_STYLES:
        api_style = 'openai'

    return {
        'id': provider_id,
        'name': str(data.get('name') or provider_id),
        'base_url': str(data.get('base_url') or '').strip(),
        'model': str(data.get('model') or '').strip(),
        'api_style': api_style,
        'connect_timeout': _to_int(data.get('connect_timeout'), 10, minimum=1, maximum=120),
        'request_timeout': _to_int(data.get('request_timeout'), 120, minimum=5, maximum=900),
        'max_retries': _to_int(data.get('max_retries'), 3, minimum=0, maximum=6),
        'use_stream': bool(data.get('use_stream', False)),
        'allow_local': bool(data.get('allow_local', False)),
        'enabled': bool(data.get('enabled', True)),
    }


def validate_provider(provider) -> list:
    """校验 provider，返回错误信息列表。"""
    data = normalize_provider(provider)
    errors = []

    if not PROVIDER_ID_PATTERN.match(data['id']):
        errors.append(f'id 不符合 ^[a-z0-9][a-z0-9-]{{0,31}}$: {data["id"]!r}')

    base_url = data['base_url']
    if not base_url:
        errors.append('base_url 不能为空')
    elif not base_url.startswith(('http://', 'https://')):
        errors.append('base_url 必须以 http:// 或 https:// 开头')

    if not str(data['name']).strip():
        errors.append('name 不能为空')

    return errors


class PluginStore:
    """插件私有数据访问层。构造时传入 `PluginAPI`。"""

    def __init__(self, api):
        self._api = api
        self._data_dir = ''
        self._card_meta_cache = None

    # ------------------------------------------------------------ 路径

    def data_dir(self) -> str:
        if not self._data_dir:
            try:
                self._data_dir = str(self._api.data_dir() or '')
            except Exception:  # noqa: BLE001
                self._data_dir = ''
        return self._data_dir

    def prompts_dir(self) -> str:
        return os.path.join(self.data_dir(), PROMPT_DIRNAME)

    def profile_path(self, profile_id: str) -> str:
        safe = re.sub(r'[^a-z0-9-]+', '-', str(profile_id or '').strip().lower())
        return os.path.join(self.prompts_dir(), f'{safe}.json')

    def providers_path(self) -> str:
        return os.path.join(self.data_dir(), PROVIDERS_FILENAME)

    def card_meta_path(self) -> str:
        return os.path.join(self.data_dir(), CACHE_DIRNAME, CARD_META_FILENAME)

    def usage_path(self) -> str:
        return os.path.join(self.data_dir(), USAGE_DIRNAME, USAGE_FILENAME)

    # ------------------------------------------------------------ 文件工具

    @staticmethod
    def _ensure_dir(path: str) -> None:
        if path:
            os.makedirs(path, exist_ok=True)

    def _read_json(self, path: str, default=None):
        if not path or not os.path.isfile(path):
            return default
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning('读取 %s 失败: %s', path, exc)
            return default

    def _write_json(self, path: str, payload) -> bool:
        try:
            self._ensure_dir(os.path.dirname(path))
            temp_path = f'{path}.tmp'
            with open(temp_path, 'w', encoding='utf-8') as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            return True
        except (OSError, TypeError, ValueError) as exc:
            logger.error('写入 %s 失败: %s', path, exc)
            return False

    def _read_jsonl(self, path: str) -> list:
        if not path or not os.path.isfile(path):
            return []
        records = []
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                for line in handle:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        payload = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        records.append(payload)
        except OSError as exc:
            logger.warning('读取 %s 失败: %s', path, exc)
        return records

    def _append_jsonl(self, path: str, record) -> bool:
        try:
            self._ensure_dir(os.path.dirname(path))
            with open(path, 'a', encoding='utf-8') as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + '\n')
        except (OSError, TypeError, ValueError) as exc:
            logger.warning('追加 %s 失败: %s', path, exc)
            return False

        try:
            if os.path.getsize(path) > JSONL_COMPACT_BYTES:
                self._compact_jsonl(path)
        except OSError:
            pass
        return True

    def _compact_jsonl(self, path: str, key: str = 'ui_key') -> bool:
        """按 `key` 去重压缩 jsonl（保留最后一条），用于控制文件体积。"""
        records = self._read_jsonl(path)
        latest = {}
        for record in records:
            identity = str(record.get(key) or '')
            if not identity:
                identity = f'_anon_{len(latest)}'
            latest[identity] = record
        payload = list(latest.values())
        try:
            temp_path = f'{path}.compact'
            with open(temp_path, 'w', encoding='utf-8') as handle:
                for record in payload:
                    handle.write(json.dumps(record, ensure_ascii=False) + '\n')
            os.replace(temp_path, path)
        except OSError as exc:
            logger.warning('压缩 %s 失败: %s', path, exc)
            return False
        return True

    # ------------------------------------------------------------ ui_data

    def read_ui(self) -> dict:
        try:
            data = self._api.ui_data_read()
        except Exception:  # noqa: BLE001
            logger.debug('读取插件 ui_data 命名空间失败', exc_info=True)
            return {}
        return data if isinstance(data, dict) else {}

    def _mutate(self, mutator) -> bool:
        """在锁内修改命名空间；**丢弃 mutator 的返回值**以避免整体替换（C3）。"""

        def _inner(entry):
            mutator(entry)
            return None

        try:
            return bool(self._api.ui_data_update(_inner))
        except Exception:  # noqa: BLE001
            logger.warning('写入插件 ui_data 命名空间失败', exc_info=True)
            return False

    # ------------------------------------------------------------ 提示词档案

    def _read_user_profiles(self) -> list:
        directory = self.prompts_dir()
        if not os.path.isdir(directory):
            return []

        result = []
        try:
            names = sorted(os.listdir(directory))
        except OSError as exc:
            logger.warning('读取档案目录失败: %s', exc)
            return []

        for name in names:
            if not name.endswith('.json') or name.startswith('_'):
                continue
            payload = self._read_json(os.path.join(directory, name), None)
            if isinstance(payload, dict):
                result.append(payload)
        return result

    def list_profiles(self) -> list:
        """内置档案 + 用户档案（同 id 时用户覆盖优先）。"""
        return profiles_core.merge_profiles(self._read_user_profiles())

    def profile_index(self) -> list:
        return [profiles_core.profile_index_entry(item) for item in self.list_profiles()]

    def get_profile(self, profile_id: str):
        identifier = str(profile_id or '').strip().lower()
        if not identifier:
            return None

        payload = self._read_json(self.profile_path(identifier), None)
        if isinstance(payload, dict):
            return profiles_core.normalize_profile(payload)
        return profiles_core.builtin_profile(identifier)

    def resolve_profile(self, profile_id: str, model: str) -> dict:
        """按 id 或模型名解析档案；两者都不可用时回落到内置兜底档案。"""
        identifier = str(profile_id or '').strip().lower()
        if identifier:
            found = self.get_profile(identifier)
            if found is not None:
                return found
            raise AiAssistantError(
                CODE_PROFILE_NOT_FOUND, f'提示词档案不存在: {identifier}',
                http_status=404, detail={'profile_id': identifier},
            )
        return profiles_core.select_profile(self.list_profiles(), model)

    def save_profile(self, raw) -> dict:
        data = profiles_core.normalize_profile(raw)
        errors = profiles_core.validate_profile(data)
        if errors:
            raise AiAssistantError(
                CODE_PROFILE_INVALID, '档案校验失败：' + '；'.join(errors),
                detail={'errors': errors},
            )

        existing = self.get_profile(data['id'])
        if existing is not None:
            data['version'] = int(existing.get('version') or 1) + 1
            data['builtin'] = bool(existing.get('builtin', data.get('builtin')))
        data['updated_at'] = int(time.time())

        if not self._write_json(self.profile_path(data['id']), data):
            raise AiAssistantError(CODE_WRITE_FAILED, '档案写入失败', http_status=500)

        self.sync_profile_index()
        return data

    def delete_profile(self, profile_id: str) -> dict:
        identifier = str(profile_id or '').strip().lower()
        if not identifier:
            raise AiAssistantError(CODE_INVALID_PAYLOAD, '缺少 profile_id')

        path = self.profile_path(identifier)
        existed = os.path.isfile(path)
        if existed:
            try:
                os.remove(path)
            except OSError as exc:
                raise AiAssistantError(CODE_WRITE_FAILED, f'删除失败: {exc}', http_status=500) from exc

        is_builtin = profiles_core.builtin_profile(identifier) is not None
        if not existed and not is_builtin:
            raise AiAssistantError(
                CODE_PROFILE_NOT_FOUND, f'提示词档案不存在: {identifier}', http_status=404,
            )

        self.sync_profile_index()
        return {
            'id': identifier,
            'deleted': existed,
            'restored_builtin': is_builtin,
            'message': '已恢复出厂默认' if is_builtin and existed else '已删除',
        }

    def duplicate_profile(self, profile_id: str, *, new_id: str = '', new_name: str = '') -> dict:
        source = self.get_profile(profile_id)
        if source is None:
            raise AiAssistantError(
                CODE_PROFILE_NOT_FOUND, f'提示词档案不存在: {profile_id}', http_status=404,
            )

        base = str(new_id or '').strip().lower() or f"{source['id']}-copy"
        base = re.sub(r'[^a-z0-9-]+', '-', base).strip('-') or 'copy'

        candidate = base
        suffix = 2
        while self.get_profile(candidate) is not None:
            candidate = f'{base}-{suffix}'
            suffix += 1

        data = dict(source)
        data['id'] = candidate
        data['name'] = str(new_name or f"{source['name']} 副本")
        data['builtin'] = False
        data['version'] = 1
        return self.save_profile(data)

    def reset_profiles(self) -> dict:
        """把全部档案恢复为出厂状态：删除用户档案，并把被覆盖的内置档案备份后移除。"""
        directory = self.prompts_dir()
        removed = []
        if os.path.isdir(directory):
            backup_dir = os.path.join(directory, BACKUP_DIRNAME)
            stamp = int(time.time())
            for name in sorted(os.listdir(directory)):
                if not name.endswith('.json') or name.startswith('_'):
                    continue
                source = os.path.join(directory, name)
                try:
                    self._ensure_dir(backup_dir)
                    shutil.copy2(source, os.path.join(backup_dir, f'{name}.{stamp}'))
                    os.remove(source)
                    removed.append(name[:-len('.json')])
                except OSError as exc:
                    logger.warning('重置档案 %s 失败: %s', source, exc)

        self.sync_profile_index()
        return {'removed': removed, 'backup_dir': os.path.join(directory, BACKUP_DIRNAME)}

    def export_profiles(self, profile_id: str = '') -> dict:
        identifier = str(profile_id or '').strip().lower()
        if identifier:
            data = self.get_profile(identifier)
            if data is None:
                raise AiAssistantError(
                    CODE_PROFILE_NOT_FOUND, f'提示词档案不存在: {identifier}', http_status=404,
                )
            items = [data]
        else:
            items = self.list_profiles()

        return {
            'schema_version': profiles_core.SCHEMA_VERSION,
            'exported_at': int(time.time()),
            'profiles': items,
        }

    def import_profiles(self, payload) -> dict:
        items = payload
        if isinstance(payload, dict):
            items = payload.get('profiles', payload)
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            raise AiAssistantError(CODE_INVALID_PAYLOAD, '导入内容必须是档案对象或档案数组')

        imported = []
        failed = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            data = profiles_core.normalize_profile(raw)
            if not data['id']:
                continue

            if self.get_profile(data['id']) is not None:
                base = f"{data['id']}-imported"
                candidate = base
                suffix = 2
                while self.get_profile(candidate) is not None:
                    candidate = f'{base}-{suffix}'
                    suffix += 1
                data['id'] = candidate

            data['builtin'] = False
            data['version'] = 1
            try:
                imported.append(self.save_profile(data))
            except AiAssistantError as exc:
                failed.append({'id': data['id'], 'error': exc.message})

        return {'imported': imported, 'failed': failed, 'total': len(items)}

    def sync_profile_index(self) -> bool:
        """把档案索引写进 ui_data（只改 `prompt_index` 一个子键）。"""
        index = self.profile_index()

        def mutator(entry):
            entry['prompt_index'] = index

        return self._mutate(mutator)

    def active_profile_id(self) -> str:
        return str(self.read_ui().get('active_profile_id') or '')

    def set_active_profile(self, profile_id: str) -> bool:
        identifier = str(profile_id or '').strip().lower()

        def mutator(entry):
            entry['active_profile_id'] = identifier

        return self._mutate(mutator)

    # ------------------------------------------------------------ provider

    def providers_config(self) -> dict:
        payload = self._read_json(self.providers_path(), None)
        if not isinstance(payload, dict) or not isinstance(payload.get('items'), list):
            payload = self._default_providers()
            self._write_json(self.providers_path(), payload)
            return payload

        payload['items'] = [
            normalize_provider(item) for item in payload['items'] if isinstance(item, dict)
        ]
        payload.setdefault('schema_version', 1)
        if not isinstance(payload.get('default_id'), str):
            payload['default_id'] = payload['items'][0]['id'] if payload['items'] else ''
        return payload

    def _default_providers(self) -> dict:
        """首次运行：用 `secret_schema` 的默认值生成一条 provider（不复制明文密钥）。"""
        base_url = ''
        model = ''
        try:
            base_url = str(self._api.get_secret('base_url') or '')
            model = str(self._api.get_secret('model') or '')
        except Exception:  # noqa: BLE001
            logger.debug('读取默认 provider 配置失败', exc_info=True)

        base_url = base_url or DEFAULT_BASE_URL
        provider_id = provider_id_from_base_url(base_url)
        return {
            'schema_version': 1,
            'default_id': provider_id,
            'items': [normalize_provider({
                'id': provider_id,
                'name': provider_id,
                'base_url': base_url,
                'model': model,
            })],
        }

    def list_providers(self) -> list:
        return list(self.providers_config().get('items') or [])

    def get_provider(self, provider_id: str):
        identifier = str(provider_id or '').strip().lower()
        if not identifier:
            return None
        for item in self.list_providers():
            if item['id'] == identifier:
                return item
        return None

    def resolve_provider(self, provider_id: str) -> dict:
        identifier = str(provider_id or '').strip().lower()
        if identifier:
            found = self.get_provider(identifier)
            if found is None:
                raise AiAssistantError(
                    CODE_PROVIDER_NOT_FOUND, f'provider 不存在: {identifier}',
                    http_status=404, detail={'provider_id': identifier},
                )
            return found

        config = self.providers_config()
        default_id = str(config.get('default_id') or '')
        found = self.get_provider(default_id)
        if found is not None:
            return found

        items = config.get('items') or []
        if items:
            return items[0]

        raise AiAssistantError(CODE_PROVIDER_NOT_FOUND, '尚未配置任何 provider', http_status=404)

    def save_provider(self, raw) -> dict:
        data = normalize_provider(raw)
        errors = validate_provider(data)
        if errors:
            raise AiAssistantError(
                CODE_INVALID_PAYLOAD, 'provider 校验失败：' + '；'.join(errors),
                detail={'errors': errors},
            )

        config = self.providers_config()
        items = [item for item in config['items'] if item['id'] != data['id']]
        items.append(data)
        items.sort(key=lambda item: item['id'])
        config['items'] = items
        if not config.get('default_id') or config['default_id'] not in {item['id'] for item in items}:
            config['default_id'] = data['id']

        if not self._write_json(self.providers_path(), config):
            raise AiAssistantError(CODE_WRITE_FAILED, 'provider 写入失败', http_status=500)
        return data

    def delete_provider(self, provider_id: str) -> dict:
        identifier = str(provider_id or '').strip().lower()
        config = self.providers_config()

        if str(config.get('default_id') or '') == identifier:
            raise AiAssistantError(
                CODE_INVALID_PAYLOAD, '不能删除默认 provider，请先把其它 provider 设为默认',
            )

        remaining = [item for item in config['items'] if item['id'] != identifier]
        if len(remaining) == len(config['items']):
            raise AiAssistantError(
                CODE_PROVIDER_NOT_FOUND, f'provider 不存在: {identifier}', http_status=404,
            )

        config['items'] = remaining
        if not self._write_json(self.providers_path(), config):
            raise AiAssistantError(CODE_WRITE_FAILED, 'provider 删除失败', http_status=500)

        self.clear_provider_secrets(identifier)
        return {'id': identifier, 'remaining': len(remaining)}

    def set_default_provider(self, provider_id: str) -> dict:
        identifier = str(provider_id or '').strip().lower()
        config = self.providers_config()
        if identifier not in {item['id'] for item in config['items']}:
            raise AiAssistantError(
                CODE_PROVIDER_NOT_FOUND, f'provider 不存在: {identifier}', http_status=404,
            )
        config['default_id'] = identifier
        if not self._write_json(self.providers_path(), config):
            raise AiAssistantError(CODE_WRITE_FAILED, '默认 provider 写入失败', http_status=500)
        return {'default_id': identifier}

    # ---- provider 密钥 ----

    def _get_secret(self, key: str):
        try:
            value = self._api.get_secret(key)
        except Exception:  # noqa: BLE001
            logger.debug('读取密钥 %s 失败', key, exc_info=True)
            return None
        return value

    def _set_secret(self, key: str, value) -> bool:
        try:
            return bool(self._api.set_secret(key, value))
        except Exception:  # noqa: BLE001
            logger.warning('写入密钥 %s 失败', key, exc_info=True)
            return False

    def describe_secrets(self) -> dict:
        try:
            payload = self._api.list_secrets()
        except Exception:  # noqa: BLE001
            logger.debug('读取密钥元数据失败', exc_info=True)
            return {}
        return payload if isinstance(payload, dict) else {}

    def describe_providers(self) -> list:
        """provider 列表 + 密钥状态（**不含明文**）。"""
        config = self.providers_config()
        secrets_map = self.describe_secrets()
        default_id = str(config.get('default_id') or '')

        result = []
        for item in config['items']:
            api_key_info = secrets_map.get(api_key_secret_name(item['id'])) or {}
            headers_info = secrets_map.get(extra_headers_secret_name(item['id'])) or {}
            entry = dict(item)
            entry['is_default'] = item['id'] == default_id
            entry['has_api_key'] = bool(api_key_info.get('set'))
            entry['api_key_hint'] = str(api_key_info.get('hint') or '')
            entry['has_extra_headers'] = bool(headers_info.get('set'))
            entry['has_fallback_key'] = (
                item['id'] == default_id and bool((secrets_map.get('api_key') or {}).get('set'))
            )
            result.append(entry)
        return result

    def resolve_provider_key(self, provider_id: str) -> str:
        """解析 provider 的 API Key：专属密钥 → 默认 provider 回退到 `api_key` → 空。"""
        identifier = str(provider_id or '').strip().lower()
        if not identifier:
            return ''

        value = self._get_secret(api_key_secret_name(identifier))
        if value:
            return str(value)

        default_id = str(self.providers_config().get('default_id') or '')
        if identifier == default_id:
            fallback = self._get_secret('api_key')
            if fallback:
                return str(fallback)
        return ''

    def resolve_provider_headers(self, provider_id: str) -> dict:
        """解析 provider 的自定义请求头（存的是 JSON 字符串，可能含密钥）。"""
        identifier = str(provider_id or '').strip().lower()
        raw = self._get_secret(extra_headers_secret_name(identifier))
        if not raw:
            return {}
        try:
            payload = json.loads(str(raw))
        except (TypeError, ValueError):
            logger.warning('provider %s 的自定义请求头不是合法 JSON，已忽略', identifier)
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(key): str(value) for key, value in payload.items() if str(key).strip()}

    def set_provider_secrets(self, provider_id: str, *, api_key=None, extra_headers=None) -> dict:
        identifier = str(provider_id or '').strip().lower()
        if self.get_provider(identifier) is None:
            raise AiAssistantError(
                CODE_PROVIDER_NOT_FOUND, f'provider 不存在: {identifier}', http_status=404,
            )

        result = {'api_key': False, 'extra_headers': False}

        if api_key is not None:
            text = str(api_key).strip()
            result['api_key'] = self._set_secret(
                api_key_secret_name(identifier), text if text else None,
            )

        if extra_headers is not None:
            payload = extra_headers
            if isinstance(payload, str):
                text = payload.strip()
                payload = {} if not text else None
                if payload is None:
                    try:
                        payload = json.loads(text)
                    except (TypeError, ValueError) as exc:
                        raise AiAssistantError(
                            CODE_INVALID_PAYLOAD, f'extra_headers 不是合法 JSON: {exc}',
                        ) from exc
            if not isinstance(payload, dict):
                raise AiAssistantError(CODE_INVALID_PAYLOAD, 'extra_headers 必须是对象')

            serialized = json.dumps(payload, ensure_ascii=False) if payload else ''
            result['extra_headers'] = self._set_secret(
                extra_headers_secret_name(identifier), serialized or None,
            )

        return result

    def clear_provider_secrets(self, provider_id: str) -> bool:
        identifier = str(provider_id or '').strip().lower()
        ok = self._set_secret(api_key_secret_name(identifier), None)
        ok = self._set_secret(extra_headers_secret_name(identifier), None) and ok
        return ok

    # ------------------------------------------------------------ 卡片元数据

    def load_card_meta(self) -> dict:
        """全量元数据（`ui_data` 热缓存 + jsonl 全量，后者优先）。"""
        if self._card_meta_cache is not None:
            return self._card_meta_cache

        cache = {}
        hot = self.read_ui().get('card_meta')
        if isinstance(hot, dict):
            for key, value in hot.items():
                if isinstance(value, dict):
                    cache[str(key)] = value

        for record in self._read_jsonl(self.card_meta_path()):
            key = str(record.get('ui_key') or '')
            if key:
                cache[key] = record

        self._card_meta_cache = cache
        return cache

    def get_card_meta(self, ui_key: str) -> dict:
        key = str(ui_key or '').strip()
        if not key:
            return {}
        record = self.load_card_meta().get(key)
        return dict(record) if isinstance(record, dict) else {}

    def update_card_meta(self, ui_key: str, patch) -> dict:
        key = str(ui_key or '').strip()
        if not key or not isinstance(patch, dict):
            return {}

        cache = self.load_card_meta()
        record = dict(cache.get(key) or {})
        record.update(patch)
        record['ui_key'] = key
        record['updated_at'] = int(time.time())
        cache[key] = record

        self._append_jsonl(self.card_meta_path(), record)
        self.sync_card_meta_ui()
        return record

    def sync_card_meta_ui(self) -> bool:
        """把最近的元数据同步进 ui_data（带条数与体积双重裁剪）。"""
        cache = self.load_card_meta()
        records = sorted(
            cache.values(), key=lambda item: -int(item.get('updated_at') or 0),
        )

        selected = {}
        for record in records[:CARD_META_UI_LIMIT]:
            key = str(record.get('ui_key') or '')
            if key:
                selected[key] = record

        while selected:
            size = len(json.dumps(selected, ensure_ascii=False).encode('utf-8'))
            if size <= CARD_META_UI_BYTES_LIMIT:
                break
            oldest_key = min(
                selected, key=lambda item: int(selected[item].get('updated_at') or 0),
            )
            selected.pop(oldest_key, None)

        def mutator(entry):
            entry['card_meta'] = selected

        return self._mutate(mutator)

    def forget_card_meta(self, ui_key: str) -> bool:
        key = str(ui_key or '').strip()
        if not key:
            return False
        cache = self.load_card_meta()
        cache.pop(key, None)
        self.sync_card_meta_ui()
        return True

    # ------------------------------------------------------------ 标签建议队列

    def record_tag_suggestions(self, suggestions, *, card_key: str = '') -> dict:
        """把新标签建议并入待审批队列（**不自动建库**）。"""
        updated = {}
        if not suggestions:
            return updated

        def mutator(entry):
            queue = entry.get('tag_suggestions')
            if not isinstance(queue, dict):
                queue = {}
            now = int(time.time())

            for item in suggestions:
                name = str((item or {}).get('name') or '').strip()
                if not name:
                    continue
                record = queue.get(name)
                if not isinstance(record, dict):
                    record = {'count': 0, 'cards': [], 'first_seen': now}

                record['count'] = int(record.get('count') or 0) + 1
                cards = record.get('cards') if isinstance(record.get('cards'), list) else []
                if card_key and card_key not in cards:
                    cards.append(card_key)
                record['cards'] = cards[:50]
                record['reason'] = str((item or {}).get('reason') or record.get('reason') or '')
                record['last_seen'] = now

                queue[name] = record
                updated[name] = record

            entry['tag_suggestions'] = queue

        self._mutate(mutator)
        return updated

    def list_tag_suggestions(self) -> dict:
        queue = self.read_ui().get('tag_suggestions')
        if not isinstance(queue, dict):
            return {}
        return {str(key): value for key, value in queue.items() if isinstance(value, dict)}

    def clear_tag_suggestions(self, names=None) -> dict:
        removed = []

        def mutator(entry):
            queue = entry.get('tag_suggestions')
            if not isinstance(queue, dict):
                queue = {}
            if names:
                for name in names:
                    text = str(name or '').strip()
                    if text and text in queue:
                        queue.pop(text, None)
                        removed.append(text)
            else:
                removed.extend(list(queue.keys()))
                queue = {}
            entry['tag_suggestions'] = queue

        self._mutate(mutator)
        return {'removed': removed}

    # ------------------------------------------------------------ 用量台账

    def record_usage(self, record) -> None:
        payload = dict(record or {})
        payload['recorded_at'] = int(time.time())
        self._append_jsonl(self.usage_path(), payload)

        prompt_tokens = int(payload.get('prompt_tokens') or 0)
        completion_tokens = int(payload.get('completion_tokens') or 0)

        def mutator(entry):
            ledger = entry.get('usage_ledger')
            if not isinstance(ledger, dict):
                ledger = {}
            ledger['total_prompt_tokens'] = int(ledger.get('total_prompt_tokens') or 0) + prompt_tokens
            ledger['total_completion_tokens'] = int(
                ledger.get('total_completion_tokens') or 0) + completion_tokens
            ledger['jobs'] = int(ledger.get('jobs') or 0) + 1
            ledger['updated_at'] = int(time.time())
            entry['usage_ledger'] = ledger

        self._mutate(mutator)

    def usage_ledger(self) -> dict:
        ledger = self.read_ui().get('usage_ledger')
        if not isinstance(ledger, dict):
            return {'total_prompt_tokens': 0, 'total_completion_tokens': 0, 'jobs': 0}
        return {
            'total_prompt_tokens': int(ledger.get('total_prompt_tokens') or 0),
            'total_completion_tokens': int(ledger.get('total_completion_tokens') or 0),
            'jobs': int(ledger.get('jobs') or 0),
            'updated_at': int(ledger.get('updated_at') or 0),
        }

    def recent_usage(self, limit: int = 20) -> list:
        records = self._read_jsonl(self.usage_path())
        size = max(1, int(limit or 20))
        return records[-size:]

    # ------------------------------------------------------------ 审批与提示

    def mark_pending_review(self, run_id: str, job_type: str, item_count: int) -> None:
        identifier = str(run_id or '').strip()
        if not identifier:
            return

        def mutator(entry):
            pending = entry.get('pending_reviews')
            if not isinstance(pending, dict):
                pending = {}
            pending[identifier] = {
                'job_type': str(job_type or ''),
                'items': int(item_count or 0),
                'created_at': int(time.time()),
            }
            entry['pending_reviews'] = pending

        self._mutate(mutator)

    def clear_pending_review(self, run_id: str) -> None:
        identifier = str(run_id or '').strip()
        if not identifier:
            return

        def mutator(entry):
            pending = entry.get('pending_reviews')
            if isinstance(pending, dict):
                pending.pop(identifier, None)
                entry['pending_reviews'] = pending

        self._mutate(mutator)

    def list_pending_reviews(self) -> dict:
        pending = self.read_ui().get('pending_reviews')
        if not isinstance(pending, dict):
            return {}
        return {str(key): value for key, value in pending.items() if isinstance(value, dict)}

    def outbound_acknowledged(self) -> bool:
        return bool(self.read_ui().get('outbound_notice_ack'))

    def acknowledge_outbound(self) -> bool:
        def mutator(entry):
            entry['outbound_notice_ack'] = True

        return self._mutate(mutator)
