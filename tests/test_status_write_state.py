"""`/status` 要能报告「本地记账写不进去了」。

用户看不到宿主日志，只能对着不动的界面猜。`/status` 是前端唯一每次都读的
接口，把写入状态挂在这里，设置页与 hub 才有办法提示。
"""

import json

import pytest

flask = pytest.importorskip('flask', reason='路由测试需要 flask（主程序运行环境必备）')

from conftest import load_backend_submodule  # noqa: E402
from fake_api import FakeApi  # noqa: E402

routes_module = load_backend_submodule('routes')
store_module = load_backend_submodule('store')

HOST_QUOTA = 256 * 1024


class QuotaHost:
    def __init__(self, data_dir, quota=HOST_QUOTA):
        self.ns = {}
        self.dir = data_dir
        self.quota = quota

    def read(self):
        return json.loads(json.dumps(self.ns, ensure_ascii=False))

    def update(self, mutator):
        entry = json.loads(json.dumps(self.ns, ensure_ascii=False))
        mutator(entry)
        if len(json.dumps(entry, ensure_ascii=False).encode('utf-8')) > self.quota:
            return False
        self.ns = entry
        return True


class QuotaPrivate:
    def __init__(self, host):
        self._host = host

    def read(self):
        return self._host.read()

    def update(self, mutator):
        return self._host.update(mutator)

    def dir(self):
        return self._host.dir


def build(tmp_path):
    api = FakeApi(data_dir=str(tmp_path / 'data'), secrets={'api_key': 'sk-test'},
                  tag_library={'tags': ['奇幻'], 'taxonomy': {}, 'prefs': {}})
    host = QuotaHost(str(tmp_path / 'data'))
    api.private = QuotaPrivate(host)
    store = store_module.PluginStore(api)
    routes_module.register_routes(api, store)

    app = flask.Flask(__name__)
    app.config['TESTING'] = True
    for rule, methods, func in api.routes:
        app.add_url_rule(rule, endpoint=func.__name__, view_func=func, methods=list(methods))
    return app.test_client(), host, store


def test_status_reports_healthy_writes(tmp_path):
    client, _host, _store = build(tmp_path)
    payload = client.get('/status').get_json()
    assert payload['ui_data']['ok'] is True


def test_status_reports_failed_writes(tmp_path):
    client, host, store = build(tmp_path)
    host.ns = {'tag_suggestions': {'占位': {'reason': 'r' * HOST_QUOTA}}}
    store.record_run({'run_id': 'run-a'})  # 这次写入会被宿主拒绝

    payload = client.get('/status').get_json()
    assert payload['ui_data']['ok'] is False
    assert payload['ui_data']['failed_writes'] >= 1
    assert payload['ui_data']['message']


def test_status_keeps_existing_contract(tmp_path):
    """既有字段不能丢：前端 hub 靠它们渲染计数。"""
    client, _host, _store = build(tmp_path)
    payload = client.get('/status').get_json()
    for key in ('plugin', 'config', 'profiles', 'providers', 'usage',
                'pending_reviews', 'suggestions', 'outbound_acknowledged'):
        assert key in payload, f'/status 缺少既有字段 {key}'
