"""`PluginAPI` 的测试替身（域命名空间形态）。

只实现插件**实际用到**的那部分契约（签名与主程序
`core/plugins/api.py` + `core/plugins/domains/*` 对齐），
用于在无 Flask、无主程序的环境下验证装配层与作业逻辑。
"""

import json
import os


class FakeConnection:
    """极简 sqlite 连接替身：忽略 SQL，直接返回预设行。"""

    def __init__(self, rows):
        self._rows = list(rows or [])
        self.statements = []
        self.closed = False

    def execute(self, sql, params=()):
        self.statements.append((str(sql), tuple(params or ())))
        return self

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        self.closed = True


class _FakeLog:
    def __init__(self, api):
        self._api = api

    def _write(self, level, message, **extra):
        self._api.logs.append((str(level), str(message), extra))

    def debug(self, message, **extra):
        self._write('debug', message, **extra)

    def info(self, message, **extra):
        self._write('info', message, **extra)

    def warn(self, message, **extra):
        self._write('warning', message, **extra)

    def error(self, message, **extra):
        self._write('error', message, **extra)


class _FakeConfig:
    def __init__(self, api):
        self._api = api

    def get(self):
        return dict(self._api._config)

    def set(self, patch):
        if isinstance(patch, dict):
            self._api._config.update(patch)
            return True
        return False


class _FakeSecrets:
    def __init__(self, api):
        self._api = api

    def get(self, key):
        return self._api._secrets.get(str(key))

    def set(self, key, value):
        name = str(key)
        if value is None or str(value) == '':
            self._api._secrets.pop(name, None)
        else:
            self._api._secrets[name] = str(value)
        return True

    def list(self):
        result = {}
        for key, value in self._api._secrets.items():
            text = str(value)
            hint = text if len(text) <= 8 else f'{text[:3]}…{text[-4:]}'
            result[str(key)] = {'set': True, 'hint': hint, 'updated_at': 0, 'source': 'file'}
        return result


class _FakeHttp:
    def __init__(self, api):
        self._api = api

    def route(self, rule, methods=None):
        def decorator(func):
            self._api.routes.append((rule, tuple(methods or ('GET',)), func))
            return func

        return decorator

    def blueprint(self, bp):
        return True


class _FakeJobs:
    def __init__(self, api):
        self._api = api

    def register(self, job_type, handler):
        self._api.registered_jobs[str(job_type)] = handler

    def submit(self, job_type, *, items, payload=None):
        self._api.submitted = {
            'job_type': str(job_type),
            'items': list(items or []),
            'payload': dict(payload or {}),
        }
        return 'run-test-0001'

    def get(self, run_id):
        return self._api.job_detail

    def items(self, run_id, *, status=None, limit=0, offset=0):
        """返回已解析的条目形状（与主程序 `job_store._row_to_item` 一致）。"""
        rows = self._api.job_items or self._api._db_rows or []
        selected = [row for row in rows if isinstance(row, dict)]
        if status:
            selected = [row for row in selected if str(row.get('status') or '') == str(status)]
        start = max(0, int(offset or 0))
        end = start + int(limit) if int(limit or 0) > 0 else None
        return selected[start:end]

    def cancel(self, run_id):
        self._api.cancelled.append(str(run_id))
        return True

    def retry(self, run_id, *, only_failed=True):
        return str(run_id)

    def pause(self, run_id):
        return True

    def resume(self, run_id):
        return True


class _FakeCli:
    def __init__(self, api):
        self._api = api

    def register(self, name, handler, description=''):
        self._api.registered_cli[str(name)] = {'handler': handler, 'description': description}


class _FakePrivate:
    def __init__(self, api):
        self._api = api

    def read(self):
        return json.loads(json.dumps(self._api._ui))

    def update(self, mutator):
        result = mutator(self._api._ui)
        if isinstance(result, dict):
            self._api._ui.clear()
            self._api._ui.update(result)
        return True

    def dir(self):
        os.makedirs(self._api._data_dir, exist_ok=True)
        return self._api._data_dir

    def table_prefix(self):
        return 'plugin_stm_ai_assistant_'

    def migrate(self, statements):
        self._api.migrations.extend(statements or [])

    def query(self, sql, params=()):
        self._api.queries.append((str(sql), tuple(params or ())))
        return list(self._api._db_rows or [])


class _FakeCards:
    def __init__(self, api):
        self._api = api

    def read(self, card_id):
        return self._api._cards.get(str(card_id))

    def read_many(self, card_ids):
        return {str(cid): self.read(cid) for cid in (card_ids or [])}

    def read_extensions(self, card_id):
        return {}

    def write_fields(self, card_id, patch):
        return True

    def apply_tags(self, card_ids, *, add=(), remove=(), replace=False):
        ids = [str(item) for item in (card_ids or [])]
        self._api.applied_tags.append({'card_ids': ids, 'add': list(add), 'replace': bool(replace)})
        return {
            'updated': len(ids),
            'added': list(add),
            'removed': list(remove),
            'failed': [],
            'skipped_unknown': [],
            'skipped_blacklist': [],
        }

    def read_note(self, card_id):
        return str(self._api._notes.get(str(card_id)) or '')

    def read_notes(self, card_ids):
        return {str(cid): self.read_note(cid) for cid in (card_ids or [])}

    def write_note(self, card_id, text):
        self._api._notes[str(card_id)] = str(text or '')
        return True

    def move(self, card_ids, target_category):
        return {'count': 0, 'moved': [], 'failed': [], 'category_counts': {}}

    def list_categories(self):
        return []

    def create_category(self, name, *, parent=''):
        return True

    def rename_category(self, old_path, new_path):
        return True

    def list(self, *, query='', category='', tags=(), exclude_tags=(),
             limit=100, offset=0, sort='', **filters):
        """按 `db_rows` 模拟索引查询（`query` 做大小写不敏感的名字/路径匹配）。"""
        rows = []
        for row in (self._api._db_rows or []):
            if not isinstance(row, dict):
                continue
            card_id = str(row.get('id') or '')
            name = str(row.get('char_name') or card_id)
            needle = str(query or '').strip().lower()
            if needle and needle not in name.lower() and needle not in card_id.lower():
                continue
            raw_tags = row.get('tags') or '[]'
            if isinstance(raw_tags, str):
                try:
                    parsed = json.loads(raw_tags or '[]')
                except (TypeError, ValueError):
                    parsed = []
            else:
                parsed = list(raw_tags)
            rows.append({
                'id': card_id,
                'char_name': name,
                'tags': parsed if isinstance(parsed, list) else [],
            })

        total = len(rows)
        start = max(0, int(offset or 0))
        end = start + int(limit) if int(limit or 0) > 0 else None
        return {'items': rows[start:end], 'total': total, 'index_ready': True}

    def count(self, **filters):
        return int(self.list(limit=0, **filters)['total'])


class _FakeTags:
    def __init__(self, api):
        self._api = api

    def library(self):
        return json.loads(json.dumps(self._api._tag_library))

    def taxonomy(self):
        return dict(self._api._tag_library.get('taxonomy') or {})

    def update_taxonomy(self, patch):
        return True

    def prefs(self):
        return dict(self._api._tag_library.get('prefs') or {})

    def update_prefs(self, patch):
        return True

    def create(self, tags):
        return {'created': [], 'existing': list(tags or []), 'failed': []}

    def merge(self, replace_rules):
        return {'tags': [], 'changed': False, 'replacements': [], 'cycles': []}


class _FakeWorldbook:
    def __init__(self, api):
        self._api = api

    def read(self, source):
        return None

    def write(self, source, data, *, expected_revision=''):
        return {'success': True, 'source_revision': ''}

    def list_entries(self, source):
        return []

    def history(self, source, entry_uid):
        return []


class _FakeBackups:
    def __init__(self, api):
        self._api = api

    def snapshot_card(self, card_id, *, remark=''):
        self._api.snapshots.append((str(card_id), str(remark)))
        return None

    def list_card_snapshots(self, card_id):
        return []

    def restore_card_snapshot(self, card_id, snapshot_path):
        return {'success': False}


class _FakeIndex:
    def __init__(self, api):
        self._api = api

    def status(self):
        return {'state': 'ready'}

    def search(self, query, *, scope='cards', limit=50, offset=0,
               category='', tags=(), search_mode='fast'):
        return {'items': [], 'total': 0, 'index_ready': True}

    def rebuild(self, *, scope='cards'):
        return 'index-job-0001'


class _FakeChats:
    def __init__(self, api):
        self._api = api

    def list(self, *, search='', favorite=None, limit=0):
        return []

    def get(self, chat_id):
        return None


class _FakePresets:
    def __init__(self, api):
        self._api = api

    def list(self, *, search='', limit=0):
        return []

    def read(self, file_path):
        return None


class _FakeExtensions:
    def __init__(self, api):
        self._api = api

    def regex_scripts(self):
        return []

    def scripts(self):
        return []


class FakeApi:
    """记录型 PluginAPI 替身（域命名空间形态）。"""

    def __init__(self, *, data_dir, config=None, secrets=None, cards=None,
                 tag_library=None, db_rows=None):
        self.plugin_id = 'stm-ai-assistant'
        self.manifest = {'id': self.plugin_id, 'version': '0.1.0'}
        self.app_version = '1.0.0'

        self._data_dir = str(data_dir)
        self._config = dict(config or {})
        self._secrets = dict(secrets or {})
        self._ui = {}
        self._notes = {}
        self._cards = dict(cards or {})
        self._tag_library = tag_library or {'tags': [], 'taxonomy': {}, 'prefs': {}}
        self._db_rows = list(db_rows or [])

        # 记录（注意：`jobs` / `cli` / `log` / `cards` … 已被域占用，
        # 记录容器一律加 `registered_` 前缀，避免命名冲突）
        self.routes = []
        self.registered_jobs = {}
        self.registered_cli = {}
        self.migrations = []
        self.logs = []
        self.queries = []
        self.applied_tags = []
        self.snapshots = []
        self.submitted = None
        self.job_detail = None
        self.job_items = []
        self.cancelled = []

        # 域（实例属性，与主程序一致）
        self.log = _FakeLog(self)
        self.config = _FakeConfig(self)
        self.secrets = _FakeSecrets(self)
        self.http = _FakeHttp(self)
        self.jobs = _FakeJobs(self)
        self.cli = _FakeCli(self)
        self.private = _FakePrivate(self)
        self.cards = _FakeCards(self)
        self.tags = _FakeTags(self)
        self.worldbook = _FakeWorldbook(self)
        self.backups = _FakeBackups(self)
        self.index = _FakeIndex(self)
        self.chats = _FakeChats(self)
        self.presets = _FakePresets(self)
        self.extensions = _FakeExtensions(self)

    # ---- 事件 ----
    def on(self, event, handler):
        return None

    def emit(self, event, payload):
        return None

    # ---- 工具 ----
    def estimate_tokens(self, text):
        source = '' if text is None else str(text)
        if not source:
            return 0
        cjk = sum(1 for char in source if '\u4e00' <= char <= '\u9fa5')
        import math

        return math.ceil(cjk + (len(source) - cjk) / 3.5)

    # ---- 测试便利 ----
    def set_db_rows(self, rows):
        self._db_rows = list(rows or [])


class FakeCtx:
    """作业上下文替身（对应 `core/plugins/jobs.py` 的 JobContext）。"""

    def __init__(self, items, payload=None, run_id='run-test-0001'):
        self.run_id = str(run_id)
        self.plugin_id = 'stm-ai-assistant'
        self.job_type = 'ai.analyze'
        self.payload = dict(payload or {})
        self.items = list(items or [])
        self.reports = []
        self.logs = []
        self.total = 0
        self.message = ''
        self.summary = {}
        self._cancel = False

    def should_cancel(self):
        return bool(self._cancel)

    def cancel(self):
        self._cancel = True

    def report(self, item_key, *, status, result=None, error=''):
        self.reports.append({
            'item_key': str(item_key), 'status': str(status),
            'result': result, 'error': str(error or ''),
        })

    def set_message(self, text):
        self.message = str(text)

    def set_total(self, total):
        self.total = int(total)

    def save_state(self, state):
        self.summary['state'] = state

    def load_state(self):
        return self.summary.get('state', {})

    def set_summary(self, summary):
        self.summary.update(summary or {})

    def api(self):
        return None

    def resolve_secret(self, key):
        return None

    def log(self, level, message):
        self.logs.append((str(level), str(message)))
