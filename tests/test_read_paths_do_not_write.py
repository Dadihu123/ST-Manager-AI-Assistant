"""读接口不应产生 `ui_data` 写入。

诊断见 `tmp/audit/ANALYSIS.md`「问题 2」。

`GET /profiles` 每次都调 `store.sync_profile_index()`，而宿主每次
`api.private.update()` 都是「深拷贝命名空间 → 序列化测配额 → 轮转 4 代备份 →
整文件写盘 + fsync」。于是**打开一次面板就多一次全量写**；一旦命名空间顶到
262144 上限，每次打开都会多一行「写入被拒绝」，而用户只是看了一眼档案列表。

档案索引的写入口只有 `save_profile` / `delete_profile` / `reset_profiles` /
`import_profiles` / 插件启动初始化 —— 读路径不需要写。
"""

import json

import pytest

flask = pytest.importorskip('flask', reason='路由测试需要 flask（主程序运行环境必备）')

from conftest import load_backend_submodule  # noqa: E402
from fake_api import FakeApi  # noqa: E402

routes_module = load_backend_submodule('routes')
store_module = load_backend_submodule('store')


class CountingPrivate:
    """数「命名空间被重写了几次」。"""

    def __init__(self, inner):
        self._inner = inner
        self.updates = 0

    def read(self):
        return self._inner.read()

    def update(self, mutator):
        self.updates += 1
        return self._inner.update(mutator)

    def dir(self):
        return self._inner.dir()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def build_env(tmp_path):
    api = FakeApi(
        data_dir=str(tmp_path / 'data'),
        cards={'Alice.png': {'name': 'Alice', 'description': '北境骑士。'}},
        secrets={'api_key': 'sk-test'},
        tag_library={'tags': ['奇幻'], 'taxonomy': {}, 'prefs': {}},
    )
    counter = CountingPrivate(api.private)
    api.private = counter
    store = store_module.PluginStore(api)
    routes_module.register_routes(api, store)

    app = flask.Flask(__name__)
    app.config['TESTING'] = True
    for rule, methods, func in api.routes:
        app.add_url_rule(rule, endpoint=func.__name__, view_func=func, methods=list(methods))
    return app.test_client(), api, store, counter


def test_reading_profiles_does_not_write_ui_data(tmp_path):
    """打开档案列表（读接口）不应重写 ui_data。"""
    client, _api, _store, counter = build_env(tmp_path)
    counter.updates = 0

    first = client.get('/profiles')
    second = client.get('/profiles')
    third = client.get('/profiles')

    assert first.status_code == second.status_code == third.status_code == 200
    assert counter.updates == 0, (
        f'读了 3 次 /profiles，ui_data 被重写了 {counter.updates} 次；'
        '读路径不该触发全量写盘（超配额时还会每次刷一行「写入被拒绝」）'
    )


def test_profile_index_is_still_written_on_save(tmp_path):
    """写路径仍然要维护索引：保存档案后 ui_data 里能看到它。"""
    client, api, _store, _counter = build_env(tmp_path)

    listing = client.get('/profiles').get_json()
    source_id = listing['items'][0]['index']['id']
    profile = dict(client.get(f'/profiles/{source_id}').get_json()['profile'])
    profile['id'] = 'my-profile'
    profile['name'] = '我的档案'
    profile['builtin'] = False
    response = client.post('/profiles', json={'profile': profile})
    assert response.get_json()['success'] is True

    index = api.private.read().get('prompt_index') or []
    assert any(item.get('id') == 'my-profile' for item in index)


def test_card_list_is_not_written_to_ui_data(tmp_path):
    """`/cards` 是纯读接口，同样不应写命名空间。"""
    client, _api, _store, counter = build_env(tmp_path)
    counter.updates = 0

    assert client.get('/cards').status_code == 200
    assert counter.updates == 0
