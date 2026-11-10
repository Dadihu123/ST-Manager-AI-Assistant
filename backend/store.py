"""持久化层：提示词档案、provider、卡片元数据、标签建议队列、用量台账。

设计要点（设计文档 §4.2 / §4.3）：
- 大对象一律落 `api.private.dir()` 下的文件；`ui_data` 只放索引与热缓存。
- 所有 `ui_data` 写入都通过 `_mutate()`：mutator **只原地修改子键并返回 `None`**。
  这是硬性约定——`PluginAPI.ui_data_update` 在 mutator 返回 dict 时会
  `entry.clear()` 后整体替换（`core/plugins/api.py:892-897`），会把 `config` /
  `enabled` / `load_error` 一起清掉。
- 写 `data/plugins/<id>/data/` 不需要 `suppress_fs_events()`（该目录不在扫描器监视范围）。
"""

import hashlib
import json
import logging
import os
import re
import shutil
import time
from contextlib import contextmanager
from urllib.parse import urlparse

from . import protocols
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

JSONL_COMPACT_BYTES = 8 * 1024 * 1024

#: 热缓存最多镜像多少条卡片元数据（条数上限）。
CARD_META_UI_LIMIT = 800
#: 热缓存的自身体积上限；实际预算还会被 `card_meta_budget()` 按配额余额压低。
CARD_META_UI_BYTES_LIMIT = 200 * 1024

#: 宿主给单个插件命名空间的配额（`core/plugins/api.py:72`，超出即整笔拒写）。
HOST_UI_QUOTA_BYTES = 256 * 1024
#: 同步时给别的子键与序列化抖动留的余量。
CARD_META_UI_SAFETY_BYTES = 24 * 1024
#: 热缓存的下限：再挤也要能装几张卡（否则落地后审批页看不到状态）。
CARD_META_UI_MIN_BYTES = 16 * 1024

#: 作业台账上限（「最近作业」只显示 8 条，50 条足够找回历史）。
RUNS_MAX = 50

#: 用量明细上限：改前只有 8MB 压缩，且记录没有 `ui_key` 时压缩完全不去重
#: （`_compact_jsonl` 会退化成 `_anon_i`），文件越过 8MB 后每次写入都要整文件
#: 重读重写。改按条数裁尾，稳定在一个可预期的规模。
USAGE_JSONL_MAX_RECORDS = 2000

#: 待审批登记上限 + TTL：只在 apply「有落地」时才清，取消 / 失败 / 一直没审的
#: 作业会永久残留；而宿主自己会清理终态作业，被清掉的 run 再也等不到 apply。
PENDING_REVIEW_MAX = 50
PENDING_REVIEW_TTL_SECONDS = 14 * 24 * 3600

#: 新标签建议的**键**上限（`cards` 列表原来就截 50，但键本身无上限）。
TAG_SUGGESTIONS_MAX = 200

#: 单个标签最多记住多少张卡片（避免 `cards` 列表无限长）。
TAG_SUGGESTION_CARDS_MAX = 50

PROVIDER_ID_PATTERN = re.compile(r'^[a-z0-9][a-z0-9-]{0,31}$')
DEFAULT_BASE_URL = 'https://api.openai.com/v1'

#: 审批页里用户手写的标签：存**插件数据目录的文件**（不进 ui_data）。
#: 为什么不用 ui_data：宿主对插件命名空间有体积配额（约 256 KiB），超了会整次拒绝写入。
MANUAL_TAGS_FILENAME = 'manual_tags.json'
MANUAL_TAGS_PER_ITEM = 20
MANUAL_TAG_MAX_CHARS = 32

#: AI 打标历史记录：落地成功时写一份快照（文件存储，不进 ui_data）。
#: 为什么快照：主程序的作业条目会被清理/重跑覆盖，历史要能独立查看。
HISTORY_FILENAME = 'history.json'


def normalize_manual_tags(raw) -> list:
    """把用户输入收敛成标签列表：去空白、去重、限长、限个数。

    输入既接受数组，也接受一串文本（分隔符与 UI 一致：中英文逗号/分号/顿号/斜杠/换行）。
    """
    if isinstance(raw, str):
        parts = re.split(r'[,，;；、/\n\r]+', raw)
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(item or '') for item in raw]
    else:
        return []

    result = []
    for part in parts:
        name = str(part or '').strip()
        if not name or len(name) > MANUAL_TAG_MAX_CHARS:
            continue
        if name not in result:
            result.append(name)
        if len(result) >= MANUAL_TAGS_PER_ITEM:
            break
    return result


def _prune_manual_tags(manual, ledger) -> dict:
    """只保留仍在作业台账里的 run（手动标签的生命周期跟随作业）。"""
    if not isinstance(manual, dict):
        return {}
    known = set(ledger.keys()) if isinstance(ledger, dict) else set()
    if not known:
        return manual
    return {run_id: value for run_id, value in manual.items() if run_id in known}


def history_entry_id(run_id, item_key) -> str:
    """历史记录稳定 id：同一作业的同一条目重复落地时**更新**而不是新增。"""
    raw = f'{run_id}\x00{item_key}'.encode('utf-8')
    return hashlib.sha1(raw).hexdigest()[:16]


def history_day(applied_at) -> str:
    """落地时间 → 本地日期 `YYYY-MM-DD`（历史列表按这个分组）。"""
    try:
        stamp = int(applied_at or 0)
    except (TypeError, ValueError):
        return ''
    if stamp <= 0:
        return ''
    return time.strftime('%Y-%m-%d', time.localtime(stamp))

#: 支持的 API 协议（唯一来源是 `backend/protocols.py`，这里只做本地引用以便校验）
API_STYLES = protocols.PROTOCOLS

API_KEY_SECRET_TEMPLATE = 'provider__{provider_id}__api_key'
EXTRA_HEADERS_SECRET_TEMPLATE = 'provider__{provider_id}__extra_headers'


def api_key_secret_name(provider_id: str) -> str:
    """provider 的 API Key 在 `secrets.json` 中的键名。"""
    return API_KEY_SECRET_TEMPLATE.format(provider_id=str(provider_id or '').strip().lower())


def extra_headers_secret_name(provider_id: str) -> str:
    """provider 的自定义请求头在 `secrets.json` 中的键名（可能含密钥，故按密钥存储）。"""
    return EXTRA_HEADERS_SECRET_TEMPLATE.format(provider_id=str(provider_id or '').strip().lower())


def _keep_newest(mapping: dict, field: str, limit: int) -> dict:
    """保留 `mapping` 中 `field` 最大的 `limit` 项，并列时以后插入者优先。

    `field` 是秒级时间戳，同一秒内写入多条时全并列 —— 只按它排序结果取决于
    dict 的迭代顺序，看起来「随机保留」。dict 保序这里可以免费拿到插入顺序，
    后插入的视为更新。
    """
    indexed = list(enumerate(mapping.items()))
    indexed.sort(key=lambda pair: (-int((pair[1][1] or {}).get(field) or 0), -pair[0]))
    return dict(item for _index, item in indexed[:limit])


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

    return {
        'id': provider_id,
        'name': str(data.get('name') or provider_id),
        'base_url': str(data.get('base_url') or '').strip(),
        'model': str(data.get('model') or '').strip(),
        # 协议：未知/缺失一律回落 openai_chat（`protocols.normalize_protocol`）
        'api_style': protocols.normalize_protocol(data.get('api_style')),
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
        #: `batch_card_meta()` 的嵌套深度；>0 表示攒着不同步热缓存。
        self._card_meta_depth = 0
        #: 批量块内是否有待同步的变更。
        self._card_meta_dirty = False
        #: ui_data 写入失败统计（宿主超配额时整笔被丢弃，必须留痕）。
        self._write_attempts = 0
        self._failed_writes = 0
        self._last_write_error = ''

    # ------------------------------------------------------------ 路径

    def data_dir(self) -> str:
        if not self._data_dir:
            try:
                self._data_dir = str(self._api.private.dir() or '')
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

    def manual_tags_path(self) -> str:
        return os.path.join(self.data_dir(), MANUAL_TAGS_FILENAME)

    def history_path(self) -> str:
        return os.path.join(self.data_dir(), HISTORY_FILENAME)

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

    def _trim_jsonl_tail(self, path: str, keep: int, *, max_bytes: int = 0) -> int:
        """只保留最后 `keep` 条记录，返回丢弃条数。

        与 `_compact_jsonl` 的区别：那个按 `ui_key` 去重，对**没有**该字段的
        记录（用量明细）会退化成 `_anon_i` 从而一条都删不掉 —— 文件越过阈值后
        每次写入都要整文件重读重写。这里按出现顺序裁尾，语义是「留最近的」。

        `max_bytes` 非 0 时只有文件超过它才真的去数记录（避免每次追加都整读）。
        """
        if keep <= 0 or not path or not os.path.isfile(path):
            return 0
        try:
            size = os.path.getsize(path)
        except OSError:
            return 0
        if size <= 0:
            return 0
        if max_bytes and size <= max_bytes:
            return 0

        records = self._read_jsonl(path)
        if len(records) <= keep:
            return 0
        payload = records[-keep:]
        try:
            temp_path = f'{path}.trim'
            with open(temp_path, 'w', encoding='utf-8') as handle:
                for record in payload:
                    handle.write(json.dumps(record, ensure_ascii=False) + '\n')
            os.replace(temp_path, path)
        except OSError as exc:
            logger.warning('裁剪 %s 失败: %s', path, exc)
            return 0
        return len(records) - len(payload)

    def _truncate_jsonl(self, path: str) -> int:
        """清空 jsonl 文件（保留文件本身），返回被删条数。"""
        if not path or not os.path.isfile(path):
            return 0
        records = self._read_jsonl(path)
        try:
            temp_path = f'{path}.clear'
            with open(temp_path, 'w', encoding='utf-8') as handle:
                handle.write('')
            os.replace(temp_path, path)
        except OSError as exc:
            logger.warning('清空 %s 失败: %s', path, exc)
            return 0
        return len(records)

    # ------------------------------------------------------------ ui_data

    def read_ui(self) -> dict:
        try:
            data = self._api.private.read()
        except Exception:  # noqa: BLE001
            logger.debug('读取插件 ui_data 命名空间失败', exc_info=True)
            return {}
        return data if isinstance(data, dict) else {}

    def _mutate(self, mutator) -> bool:
        """在锁内修改命名空间；**丢弃 mutator 的返回值**以避免整体替换（C3）。

        失败必须留痕：宿主在命名空间超过配额时会**拒绝整笔写入**，而这里只返回
        `bool`。调用方（`record_run` / `record_usage` / `mark_pending_review` /
        `update_card_meta` …）历史上全部丢掉返回值，于是超限后「标签建议、作业
        台账、待审批角标、档案索引」集体静默停在旧值，接口却仍然 `success` ——
        用户只看到「界面不动了」（见 `tmp/audit/ANALYSIS.md` 问题 2）。

        现在失败会记一行 warning 并写进 `last_write_state()`，供 `/status` 与
        日志侧排查。

        返回值判定：**只有显式 `False` 才算失败**。当前宿主的
        `_ui_data_update` 返回 `bool(update_ui_data(...))`，用 `bool()` 也对；
        但某些调用路径（历史版本 / 将来的包装）成功时返回 `None`，`bool(None)`
        会把每次成功都误判成失败（真机表现：手动标签保存报「写入未生效」，
        其实已经写进去了）。用 `is not False` 同时兼容两种返回值。
        """
        self._write_attempts += 1

        def _inner(entry):
            mutator(entry)
            return None

        try:
            ok = self._api.private.update(_inner) is not False
        except Exception as exc:  # noqa: BLE001
            logger.warning('写入插件 ui_data 命名空间失败', exc_info=True)
            self._note_write_failure(f'{type(exc).__name__}: {exc}')
            return False

        if ok:
            self._last_write_error = ''
        else:
            self._note_write_failure(
                '宿主拒绝了本次写入（命名空间可能已达配额上限）'
            )
        return ok

    def _note_write_failure(self, message: str) -> None:
        self._failed_writes += 1
        self._last_write_error = str(message or '写入失败')
        logger.warning(
            'ui_data 写入未生效（第 %s 次）：%s。'
            '依赖 ui_data 的状态（最近作业台账 / 待审批 / 标签建议 / 档案索引）'
            '可能停在旧值；清理一些历史数据后可恢复。',
            self._failed_writes, self._last_write_error,
        )

    def last_write_state(self) -> dict:
        """最近一次 ui_data 写入的结果（供 `/status` 与诊断用）。"""
        return {
            'ok': not self._last_write_error,
            'message': self._last_write_error,
            'failed_writes': self._failed_writes,
            'attempts': self._write_attempts,
        }

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

    def resolve_profile(self, profile_id: str, model: str, task: str = '') -> dict:
        """按 id、任务与模型名解析档案；都不可用时回落到内置兜底档案。

        Args:
            profile_id: 显式指定的档案 id（优先级最高，不做任务校验——
                用户点名要用的档案就该用）。
            model: 模型名，用于 `model_match` 自动匹配。
            task: 任务标识（`profiles.TASK_IDS`）。**空串 = 不按任务过滤**：
                只有自动挑选时才需要它，见 `profiles.select_profile`。
        """
        identifier = str(profile_id or '').strip().lower()
        if identifier:
            found = self.get_profile(identifier)
            if found is not None:
                return found
            raise AiAssistantError(
                CODE_PROFILE_NOT_FOUND, f'提示词档案不存在: {identifier}',
                http_status=404, detail={'profile_id': identifier},
            )
        return profiles_core.select_profile(self.list_profiles(), model, task=task)

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
        """首次运行：生成一条默认 provider。

        `base_url` / `model` 已是**插件私有配置**（不再来自 `secret_schema`）：
        主程序设置页只保留回退用的 `api_key`，地址与模型一律在插件 Provider 页维护。
        """
        base_url = DEFAULT_BASE_URL
        provider_id = provider_id_from_base_url(base_url)
        return {
            'schema_version': 1,
            'default_id': provider_id,
            'items': [normalize_provider({
                'id': provider_id,
                'name': provider_id,
                'base_url': base_url,
                'model': '',
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
            value = self._api.secrets.get(key)
        except Exception:  # noqa: BLE001
            logger.debug('读取密钥 %s 失败', key, exc_info=True)
            return None
        return value

    def _set_secret(self, key: str, value) -> bool:
        try:
            return bool(self._api.secrets.set(key, value))
        except Exception:  # noqa: BLE001
            logger.warning('写入密钥 %s 失败', key, exc_info=True)
            return False

    def describe_secrets(self) -> dict:
        try:
            payload = self._api.secrets.list()
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
        """写入一张卡的元数据：jsonl 逐条追加，`ui_data` 热缓存按批同步。"""
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
        # 批量块里先攒着：宿主每次 update 都是「整文件重写 + 4 代备份 + fsync」，
        # 300 张卡的作业逐卡同步就是 300 次全量写盘（见 tmp/audit/ANALYSIS.md）。
        if self._card_meta_depth:
            self._card_meta_dirty = True
        else:
            self.sync_card_meta_ui()
        return record

    @contextmanager
    def batch_card_meta(self):
        """批量写入作用域：期间只落 jsonl，退出时统一同步一次热缓存。

        嵌套调用只在外层退出时同步；块内抛异常也照样同步已写入的部分
        （否则用户在审批页点「接受」看到的结果会与卡片状态不一致）。
        """
        self._card_meta_depth += 1
        try:
            yield self
        finally:
            self._card_meta_depth -= 1
            if not self._card_meta_depth and self._card_meta_dirty:
                self._card_meta_dirty = False
                self.sync_card_meta_ui()

    def card_meta_budget(self, entry=None) -> int:
        """热缓存这次能占多少字节：在自限与「配额余额」之间取小。

        `CARD_META_UI_BYTES_LIMIT` 是 200KB，而宿主给整个命名空间的配额只有
        256KB（`core/plugins/api.py:72`）—— 一个子键固定吃掉 78%，剩下 57KB 要
        装下标签建议、作业台账、待审批、档案索引等 8 个子键。余额一旦被吃穿，
        宿主就**整笔拒绝**所有写入（连有待审批角标都刷不动）。

        所以按「其它子键实际占用」倒算本次预算；留 `CARD_META_UI_SAFETY_BYTES`
        余量给同步过程中的抖动。`entry` 省略时读一次当前命名空间。
        """
        if entry is None:
            entry = self.read_ui()
        other = 0
        for key, value in (entry or {}).items():
            if key == 'card_meta':
                continue
            try:
                other += len(json.dumps({key: value}, ensure_ascii=False).encode('utf-8')) + 2
            except (TypeError, ValueError):
                continue
        available = HOST_UI_QUOTA_BYTES - other - CARD_META_UI_SAFETY_BYTES
        return max(CARD_META_UI_MIN_BYTES, min(CARD_META_UI_BYTES_LIMIT, available))

    def sync_card_meta_ui(self) -> bool:
        """把最近的元数据同步进 ui_data（带条数与体积双重裁剪）。

        成本必须与缓存条数**近似线性**：`update_card_meta()` 每写一张卡都会调它，
        作业里就是逐卡调用（`jobs.py:769/838`、`routes.py:1737`）。

        旧实现是「先全收，再循环删最旧，每删一条重新 `json.dumps` 整个字典」，
        成本 ∝ 条数 × 200KB —— 缓存涨到上千条后单次同步要 700–800ms，
        落地 20 张卡就是十几秒（见 `tmp/audit/ANALYSIS.md` 问题 3）。

        现在按 `updated_at` 倒序**一次遍历**累加体积，装不下就停：保留的永远是
        最新的一批，且每条只序列化一次。体积口径与宿主一致（默认分隔符
        `', '` / `': '`，`ensure_ascii=False`）。

        预算取 `card_meta_budget()`（配额余额与自限取小），不是固定 200KB ——
        否则别的子键一涨，整笔写入就会被宿主拒绝。
        """
        cache = self.load_card_meta()
        records = sorted(
            cache.values(), key=lambda item: -int(item.get('updated_at') or 0),
        )

        entry_now = self.read_ui()
        budget = self.card_meta_budget(entry_now)

        selected = {}
        size = 2  # '{}'
        for record in records[:CARD_META_UI_LIMIT]:
            key = str(record.get('ui_key') or '')
            if not key:
                continue
            # 一条记录在 JSON 对象里的字节数： "key": <value>
            # 必须是 UTF-8 字节数，不是字符数：概览以中文为主（3 字节/字），
            # 用 len(str) 会低估约 1.5 倍，热缓存直接超出预算。
            entry_size = (
                len(json.dumps(key, ensure_ascii=False).encode('utf-8'))
                + 2  # ': '
                + len(json.dumps(record, ensure_ascii=False).encode('utf-8'))
            )
            grow = entry_size + (2 if selected else 0)  # 分隔逗号
            if size + grow > budget:
                break
            selected[key] = record
            size += grow

        def mutator(entry):
            entry['card_meta'] = selected

        return self._mutate(mutator)

    def _migrate_ui_card_meta(self) -> None:
        """把只在 ui_data 镜像里、还没落文件的元数据补进文件（一次性迁移）。

        只在**确认追加成功后**才允许调用方删除镜像：`_append_jsonl` 返回 bool，
        失败时保留镜像，下次启动重试，避免「迁移失败 + 镜像已删」两边都丢。
        """
        ui_records = self.read_ui().get('card_meta')
        if not isinstance(ui_records, dict) or not ui_records:
            return
        file_records = {
            str(record.get('ui_key') or ''): record
            for record in self._read_jsonl(self.card_meta_path())
            if isinstance(record, dict)
        }
        for key, record in ui_records.items():
            if not key or not isinstance(record, dict):
                continue
            existing = file_records.get(key)
            # 文件里已有更新的版本就不要拿旧镜像覆盖它
            if isinstance(existing, dict) and (
                int(existing.get('updated_at') or 0) >= int(record.get('updated_at') or 0)
            ):
                continue
            self._append_jsonl(self.card_meta_path(), record)
    

        def mutator(entry):
            entry.pop('card_meta', None)

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
                record['cards'] = cards[:TAG_SUGGESTION_CARDS_MAX]
                record['reason'] = str((item or {}).get('reason') or record.get('reason') or '')
                record['last_seen'] = now

                queue[name] = record
                updated[name] = record

            if len(queue) > TAG_SUGGESTIONS_MAX:
                # 键本身无上限时，2000 个标签名 = 240KB+，单这一个子键就能把
                # ui_data 配额吃穿。保留最近的一批：主键 last_seen 倒序，
                # 同一秒内并列时用插入顺序兜底（dict 保序，后出现的更新）。
                queue = _keep_newest(queue, 'last_seen', TAG_SUGGESTIONS_MAX)

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

    # ------------------------------------------------------------ 作业台账
    #
    # 插件不持有作业列表接口（宿主 `api.jobs` 只有 get/items/cancel/…，没有 list），
    # 但「关掉面板后还能找回上次作业」是硬需求。因此插件自己记一份**轻量台账**：
    # 只存 run_id / task / job_type / 条目数 / 时间，实时状态仍以宿主的
    # `api.jobs.get(run_id)` 为准（台账里的状态只是最后一次已知值）。

    def list_runs(self) -> list:
        runs = self.read_ui().get('runs')
        if not isinstance(runs, dict):
            return []
        items = [value for value in runs.values() if isinstance(value, dict)]
        items.sort(key=lambda item: int(item.get('created_at') or 0), reverse=True)
        return items

    def record_run(self, record) -> dict:
        payload = dict(record or {})
        run_id = str(payload.get('run_id') or '').strip()
        if not run_id:
            return {}

        payload['run_id'] = run_id
        payload.setdefault('created_at', int(time.time()))

        def mutator(entry):
            runs = entry.get('runs')
            if not isinstance(runs, dict):
                runs = {}
            existing = runs.get(run_id) if isinstance(runs.get(run_id), dict) else {}
            merged = dict(existing)
            merged.update(payload)
            runs[run_id] = merged
            # 台账只保留最近 RUNS_MAX 条，避免 ui_data 无限增长
            if len(runs) > RUNS_MAX:
                ordered = sorted(
                    runs.items(),
                    key=lambda pair: int((pair[1] or {}).get('created_at') or 0),
                    reverse=True,
                )[:RUNS_MAX]
                runs = dict(ordered)
            entry['runs'] = runs

        self._mutate(mutator)
        # 手动标签（文件）跟随台账：台账裁掉的作业，其手动标签一并清掉
        self._prune_manual_store(self.read_ui().get('runs'))
        return payload

    def update_run(self, run_id: str, patch) -> bool:
        identifier = str(run_id or '').strip()
        if not identifier:
            return False
        changes = dict(patch or {})

        # `_mutate` 只保证「写成功」，不表示「命中了某条台账」，所以先确认存在，
        # 否则调用方无法区分「更新了」与「没这条」。
        existing = self.read_ui().get('runs')
        if not isinstance(existing, dict) or identifier not in existing:
            return False

        def mutator(entry):
            runs = entry.get('runs')
            if not isinstance(runs, dict) or identifier not in runs:
                return
            current = runs.get(identifier) if isinstance(runs.get(identifier), dict) else {}
            current.update(changes)
            runs[identifier] = current
            entry['runs'] = runs

        return self._mutate(mutator)

    def forget_run(self, run_id: str) -> bool:
        """删掉一条作业记录，**同时**清掉它的待审批登记。

        只删台账不清 pending 会留下永远清不掉的孤儿登记：宿主会自己清理终态
        作业，之后 `/jobs/<id>/apply` 直接 404，那条 pending 再没有任何清除路径。
        """
        identifier = str(run_id or '').strip()

        def mutator(entry):
            runs = entry.get('runs')
            if isinstance(runs, dict):
                runs.pop(identifier, None)
                entry['runs'] = runs
            pending = entry.get('pending_reviews')
            if isinstance(pending, dict) and identifier in pending:
                pending.pop(identifier, None)
                entry['pending_reviews'] = pending

        result = self._mutate(mutator)
        store = self._read_manual_store()
        if identifier in store:
            store.pop(identifier, None)
            self._write_manual_store(store)
        return result

    # ------------------------------------------------------------ 手动标签（审批页）
    #
    # 用户在审批页给某张卡手写的标签。存**插件数据目录的文件**（`manual_tags.json`）：
    # 不改 runs / card_meta / tag_suggestions，也不参与幂等哈希；
    # 作业被遗忘或台账裁掉时，对应条目一起清理。

    def _read_manual_store(self) -> dict:
        payload = self._read_json(self.manual_tags_path(), {})
        return payload if isinstance(payload, dict) else {}

    def _write_manual_store(self, payload) -> bool:
        return self._write_json(
            self.manual_tags_path(),
            payload if isinstance(payload, dict) else {},
        )

    def _prune_manual_store(self, ledger=None) -> None:
        store = self._read_manual_store()
        if not store:
            return
        if ledger is None:
            ledger = self.read_ui().get('runs')
        pruned = _prune_manual_tags(store, ledger)
        if pruned != store:
            self._write_manual_store(pruned)

    def manual_tags(self, run_id: str) -> dict:
        """读某个作业的全部手动标签：`{item_key: {'tags': [...], 'applied': [...]}}`。"""
        identifier = str(run_id or '').strip()
        if not identifier:
            return {}
        entry = self._read_manual_store().get(identifier)
        if not isinstance(entry, dict):
            return {}

        result = {}
        for key, value in entry.items():
            if not isinstance(value, dict):
                continue
            tags = normalize_manual_tags(value.get('tags'))
            applied = [str(item).strip() for item in (value.get('applied') or [])
                       if str(item or '').strip()]
            result[str(key)] = {'tags': tags, 'applied': applied}
        return result

    def set_manual_tags(self, run_id, item_key, tags) -> dict:
        """整组替换某个条目的手动标签（增删都走这里）。"""
        identifier = str(run_id or '').strip()
        key = str(item_key or '').strip()
        if not identifier or not key:
            return {}

        clean = normalize_manual_tags(tags)
        current = self.manual_tags(identifier).get(key) or {}
        # 已经写进卡片的那些继续保持「已写」状态（删掉的自然消失）
        applied = [name for name in (current.get('applied') or []) if name in clean]

        store = self._read_manual_store()
        run = store.get(identifier) if isinstance(store.get(identifier), dict) else {}
        run[key] = {
            'tags': clean,
            'applied': applied,
            'updated_at': int(time.time()),
        }
        store[identifier] = run
        if not self._write_manual_store(store):
            return {}

        # 回读校验（文件写入可能被权限/磁盘挡住）
        saved = self.manual_tags(identifier).get(key) or {}
        if list(saved.get('tags') or []) != clean:
            return {}
        return {'tags': clean, 'applied': applied}

    def mark_manual_applied(self, run_id, item_key, tags) -> bool:
        """把这些手动标签记为「已写进卡片」，避免重复补写。"""
        identifier = str(run_id or '').strip()
        key = str(item_key or '').strip()
        names = normalize_manual_tags(tags)
        if not identifier or not key or not names:
            return False

        store = self._read_manual_store()
        run = store.get(identifier)
        if not isinstance(run, dict):
            return False
        item = run.get(key)
        if not isinstance(item, dict):
            return False
        applied = [str(name).strip() for name in (item.get('applied') or [])
                   if str(name or '').strip()]
        for name in names:
            if name not in applied:
                applied.append(name)
        item['applied'] = applied
        run[key] = item
        store[identifier] = run
        return self._write_manual_store(store)

    # ------------------------------------------------------------ AI 打标历史记录
    #
    # 落地成功时写快照（`history.json`），独立于主程序作业条目：作业被清理后
    # 历史仍可查看。按「落地时间」的本地日期分组。

    def _read_history_store(self) -> dict:
        payload = self._read_json(self.history_path(), {})
        if not isinstance(payload, dict):
            return {}
        entries = payload.get('entries')
        return entries if isinstance(entries, dict) else {}

    def _write_history_store(self, entries) -> bool:
        return self._write_json(
            self.history_path(),
            {'version': 1, 'entries': entries if isinstance(entries, dict) else {}},
        )

    def record_history(self, entry) -> bool:
        payload = dict(entry or {})
        key = str(payload.get('entry_id') or '').strip()
        if not key or not history_day(payload.get('applied_at')):
            return False
        payload['entry_id'] = key
        entries = self._read_history_store()
        entries[key] = payload
        return self._write_history_store(entries)

    def history_dates(self) -> list:
        counts = {}
        for entry in self._read_history_store().values():
            day = history_day(entry.get('applied_at'))
            if not day:
                continue
            counts[day] = counts.get(day, 0) + 1
        return [
            {'date': day, 'count': count}
            for day, count in sorted(counts.items(), reverse=True)
        ]

    def history_entries(self, date: str) -> list:
        day = str(date or '').strip()
        if not day:
            return []
        entries = [
            entry for entry in self._read_history_store().values()
            if history_day(entry.get('applied_at')) == day
        ]
        entries.sort(key=lambda item: int(item.get('applied_at') or 0), reverse=True)
        return entries

    def history_entries_for_run(self, run_id: str) -> list:
        identifier = str(run_id or '').strip()
        if not identifier:
            return []
        return [
            entry for entry in self._read_history_store().values()
            if str(entry.get('run_id') or '') == identifier
        ]

    def get_history_entry(self, entry_id: str) -> dict:
        key = str(entry_id or '').strip()
        if not key:
            return {}
        entry = self._read_history_store().get(key)
        return entry if isinstance(entry, dict) else {}

    def delete_history_entry(self, entry_id: str) -> bool:
        key = str(entry_id or '').strip()
        entries = self._read_history_store()
        if key not in entries:
            return False
        entries.pop(key, None)
        return self._write_history_store(entries)

    def clear_history_date(self, date: str) -> int:
        day = str(date or '').strip()
        if not day:
            return 0
        entries = self._read_history_store()
        doomed = [
            key for key, entry in entries.items()
            if history_day(entry.get('applied_at')) == day
        ]
        if not doomed:
            return 0
        for key in doomed:
            entries.pop(key, None)
        self._write_history_store(entries)
        return len(doomed)

    def update_history_entry(self, entry_id: str, patch) -> dict:
        key = str(entry_id or '').strip()
        entries = self._read_history_store()
        entry = entries.get(key)
        if not isinstance(entry, dict):
            return {}
        entry.update(dict(patch or {}))
        entry['entry_id'] = key
        entries[key] = entry
        if not self._write_history_store(entries):
            return {}
        return entry

    def clear_runs(self) -> dict:
        """清空「最近作业」台账（**不**动累计用量）。"""
        removed = []

        def mutator(entry):
            runs = entry.get('runs')
            if isinstance(runs, dict):
                removed.extend(list(runs.keys()))
            entry['runs'] = {}

        ok = self._mutate(mutator)
        return {'removed': len(removed), 'run_ids': removed[:50], 'ok': ok}

    # ------------------------------------------------------------ 用量台账

    def record_usage(self, record) -> None:
        payload = dict(record or {})
        payload['recorded_at'] = int(time.time())
        self._append_jsonl(self.usage_path(), payload)
        # 用量明细没有 `ui_key`，`_compact_jsonl` 对它完全不去重（N 条压完还是 N 条），
        # 所以按条数裁尾：保留最近 USAGE_JSONL_MAX_RECORDS 条。
        #
        # 前置字节检查只为省 IO：每次追加都整读一遍文件会让写入退化成 O(n²)。
        # 阈值取「上限条数 × 每条估算字节」，因此不会出现「条数已经超了、字节还没到
        # 阈值所以永远不裁」的漏洞。
        self._trim_jsonl_tail(
            self.usage_path(), USAGE_JSONL_MAX_RECORDS,
            max_bytes=max(1, len(json.dumps(payload, ensure_ascii=False).encode('utf-8')))
            * USAGE_JSONL_MAX_RECORDS,
        )

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

    def clear_usage_records(self, *, reset_ledger: bool = False) -> dict:
        """清空用量明细（`usage.jsonl`）。

        默认**保留**累计台账（`usage_ledger` 的 token / 作业总数）：那是「总共花了
        多少」的账，清明细通常只是想甩掉历史条目。`reset_ledger=True` 才连累计一起归零。
        """
        removed = self._truncate_jsonl(self.usage_path())
        if reset_ledger:
            def mutator(entry):
                entry['usage_ledger'] = {
                    'total_prompt_tokens': 0,
                    'total_completion_tokens': 0,
                    'jobs': 0,
                    'updated_at': int(time.time()),
                }

            self._mutate(mutator)
        return {'removed': removed, 'ledger_reset': bool(reset_ledger)}

    # ------------------------------------------------------------ 审批与提示

    def mark_pending_review(self, run_id: str, job_type: str, item_count: int) -> None:
        identifier = str(run_id or '').strip()
        if not identifier:
            return

        now = int(time.time())

        def mutator(entry):
            pending = entry.get('pending_reviews')
            if not isinstance(pending, dict):
                pending = {}
            pending[identifier] = {
                'job_type': str(job_type or ''),
                'items': int(item_count or 0),
                'created_at': now,
            }
            # 上限 + TTL：这个子键原来无上限、无老化，而它只在 apply「有落地」时
            # 才被清（routes.py），取消/失败/一直没审的作业会永久残留；宿主还会
            # 自己清理终态作业，被清掉的 run 再也等不到 apply。
            pending = self._prune_pending(pending, now)
            entry['pending_reviews'] = pending

        self._mutate(mutator)

    @staticmethod
    def _prune_pending(pending: dict, now: int) -> dict:
        """按 TTL + 条数上限裁剪待审批登记（保留最近的一批）。"""
        fresh = {
            key: value for key, value in pending.items()
            if now - int((value or {}).get('created_at') or 0) <= PENDING_REVIEW_TTL_SECONDS
        }
        if len(fresh) > PENDING_REVIEW_MAX:
            fresh = _keep_newest(fresh, 'created_at', PENDING_REVIEW_MAX)
        return fresh

    def clear_pending_review(self, run_id: str) -> None:
        self.clear_pending_reviews([run_id])

    def clear_pending_reviews(self, run_ids=None) -> dict:
        """清掉待审批登记；不传 `run_ids` 就全清。

        这是「宿主已把作业清理掉、apply 永远 404」时唯一的解救通道。
        """
        targets = [
            str(item or '').strip() for item in (run_ids or []) if str(item or '').strip()
        ]
        removed = []

        def mutator(entry):
            pending = entry.get('pending_reviews')
            if not isinstance(pending, dict):
                pending = {}
            if targets:
                for identifier in targets:
                    if identifier in pending:
                        pending.pop(identifier, None)
                        removed.append(identifier)
            else:
                removed.extend(list(pending.keys()))
                pending = {}
            entry['pending_reviews'] = pending

        ok = self._mutate(mutator)
        return {'removed': removed, 'ok': ok}

    def list_pending_reviews(self) -> dict:
        """当前待审批登记（读取时惰性老化过期条目，不改写磁盘）。"""
        pending = self.read_ui().get('pending_reviews')
        if not isinstance(pending, dict):
            return {}
        now = int(time.time())
        return {
            str(key): value for key, value in pending.items()
            if isinstance(value, dict)
            and now - int((value or {}).get('created_at') or 0) <= PENDING_REVIEW_TTL_SECONDS
        }

    def outbound_acknowledged(self) -> bool:
        return bool(self.read_ui().get('outbound_notice_ack'))

    def acknowledge_outbound(self) -> bool:
        def mutator(entry):
            entry['outbound_notice_ack'] = True

        return self._mutate(mutator)
