"""`PluginAPI` 的测试替身。

只实现插件**实际用到**的那部分契约（签名与 `core/plugins/api.py` 对齐），
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


class FakeApi:
    """记录型 PluginAPI 替身。"""

    def __init__(self, *, data_dir, config=None, secrets=None, cards=None,
                 tag_library=None, db_rows=None):
        self.plugin_id = 'stm-ai-assistant'
        self.manifest = {'id': self.plugin_id, 'version': '0.1.0'}
        self.app_version = '1.0.0'

        self._data_dir = str(data_dir)
        self._config = dict(config or {})
        self._secrets = dict(secrets or {})
        self._ui = {}
        self._cards = dict(cards or {})
        self._tag_library = tag_library or {'tags': [], 'taxonomy': {}, 'prefs': {}}
        self._db_rows = list(db_rows or [])
        self._connections = []

        # 记录
        self.routes = []
        self.jobs = {}
        self.cli = {}
        self.migrations = []
        self.logs = []
        self.applied_tags = []
        self.submitted = None
        self.job_detail = None
        self.cancelled = []

    # ---- 配置 ----
    def get_config(self):
        return dict(self._config)

    def set_config(self, patch):
        if isinstance(patch, dict):
            self._config.update(patch)
            return True
        return False

    # ---- 密钥 ----
    def get_secret(self, key):
        return self._secrets.get(str(key))

    def set_secret(self, key, value):
        name = str(key)
        if value is None or str(value) == '':
            self._secrets.pop(name, None)
        else:
            self._secrets[name] = str(value)
        return True

    def list_secrets(self):
        result = {}
        for key, value in self._secrets.items():
            text = str(value)
            hint = text if len(text) <= 8 else f'{text[:3]}…{text[-4:]}'
            result[str(key)] = {'set': True, 'hint': hint, 'updated_at': 0, 'source': 'file'}
        return result

    # ---- ui_data ----
    def ui_data_read(self):
        return json.loads(json.dumps(self._ui))

    def ui_data_update(self, mutator):
        result = mutator(self._ui)
        if isinstance(result, dict):
            self._ui.clear()
            self._ui.update(result)
        return True

    # ---- 数据 ----
    def data_dir(self):
        os.makedirs(self._data_dir, exist_ok=True)
        return self._data_dir

    def db(self):
        connection = FakeConnection(self._db_rows)
        self._connections.append(connection)
        return connection

    def set_db_rows(self, rows):
        self._db_rows = list(rows or [])

    def migrate(self, statements):
        self.migrations.extend(statements or [])

    # ---- 注册 ----
    def route(self, rule, methods=None):
        def decorator(func):
            self.routes.append((rule, tuple(methods or ('GET',)), func))
            return func

        return decorator

    def register_job(self, job_type, handler):
        self.jobs[str(job_type)] = handler

    def register_cli(self, name, handler, description=''):
        self.cli[str(name)] = {'handler': handler, 'description': description}

    def log(self, level, message, **extra):
        self.logs.append((str(level), str(message), extra))

    def notify(self, level, message):
        self.log(str(level), str(message))

    # ---- 卡片与标签 ----
    def read_card(self, card_id):
        return self._cards.get(str(card_id))

    def read_cards(self, card_ids):
        return {str(card_id): self.read_card(card_id) for card_id in (card_ids or [])}

    def read_tag_library(self):
        return json.loads(json.dumps(self._tag_library))

    def apply_tags(self, card_ids, *, add=(), remove=(), replace=False):
        ids = [str(item) for item in (card_ids or [])]
        self.applied_tags.append({'card_ids': ids, 'add': list(add), 'replace': bool(replace)})
        return {
            'updated': len(ids),
            'added': list(add),
            'removed': list(remove),
            'failed': [],
            'skipped_unknown': [],
            'skipped_blacklist': [],
        }

    # ---- 作业 ----
    def submit_job(self, job_type, *, items, payload=None):
        self.submitted = {
            'job_type': str(job_type),
            'items': list(items or []),
            'payload': dict(payload or {}),
        }
        return 'run-test-0001'

    def get_job(self, run_id):
        return self.job_detail

    def cancel_job(self, run_id):
        self.cancelled.append(str(run_id))
        return True

    def retry_job(self, run_id, *, only_failed=True):
        return str(run_id)


class FakeCtx:
    """作业上下文替身（对应 `core/plugins/jobs.py:91-219` 的 JobContext）。"""

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
