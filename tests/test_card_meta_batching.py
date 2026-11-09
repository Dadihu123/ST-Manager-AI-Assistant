"""批量写入时的 `ui_data` 同步次数（写放大）。

诊断见 `tmp/audit/ANALYSIS.md`「问题 3」与「问题 2」。

宿主每次 `api.private.update()` 都要：深拷贝整个命名空间 → 序列化测配额 →
轮转 4 代备份 → 整文件写盘 + fsync。所以「每写一张卡就同步一次热缓存」
等于「每卡一次全量重写 ui_data.json」：300 张卡的作业就是 300 次，
每次几百 KB 的 IO（宿主自己的保存也会跟着变慢，见
`tmp/audit/perf/bench_host_ui_store.py`）。

这里锁的行为是：**批量写入只在收尾同步一次**，不是逐条同步；
而单条交互式写入仍然立刻可见（用户点一次「接受」就该看到结果）。
"""

import json

import pytest

from conftest import load_backend_submodule
from fake_api import FakeApi

pytest.importorskip('flask', reason='装配层依赖 flask（主程序运行环境必备）')
store_module = load_backend_submodule('store')

PluginStore = store_module.PluginStore


class CountingPrivate:
    """包一层 `api.private`，数「命名空间被重写了几次」。"""

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


@pytest.fixture()
def env(tmp_path):
    api = FakeApi(data_dir=str(tmp_path / 'data'), secrets={'api_key': 'sk-test'})
    counter = CountingPrivate(api.private)
    api.private = counter
    return PluginStore(api), api, counter


def test_interactive_update_is_visible_immediately(env):
    """单条写入（用户点一次接受）必须立刻出现在热缓存里。"""
    store, api, _counter = env

    store.update_card_meta('card-a', {'summary': '新概览'})

    snapshot = api.private.read().get('card_meta') or {}
    assert 'card-a' in snapshot, '交互式单条写入不应延迟到下次手动同步'
    assert snapshot['card-a']['summary'] == '新概览'


def test_batch_updates_write_namespace_once(env):
    """一批 50 张卡的元数据写入，只应重写命名空间一次。"""
    store, _api, counter = env

    with store.batch_card_meta():
        for index in range(50):
            store.update_card_meta(f'card-{index}', {'n': index})

    assert counter.updates == 1, (
        f'50 张卡的批量写入重写了 {counter.updates} 次 ui_data；'
        '应只在收尾同步一次（每次都是整文件写盘 + 4 代备份轮转）'
    )


def test_batch_flushes_everything_at_exit(env):
    """收尾后，批量里写过的所有卡都应在热缓存中。"""
    store, api, _counter = env

    with store.batch_card_meta():
        for index in range(30):
            store.update_card_meta(f'card-{index}', {'n': index})

    snapshot = api.private.read().get('card_meta') or {}
    assert len(snapshot) == 30
    assert sorted(snapshot) == sorted(f'card-{index}' for index in range(30))


def test_batch_still_persists_every_record_to_jsonl(env):
    """批量优化不能牺牲落盘：jsonl 仍要逐条追加（重启后靠它重建缓存）。"""
    store, _api, _counter = env

    with store.batch_card_meta():
        for index in range(12):
            store.update_card_meta(f'card-{index}', {'n': index})

    records = store._read_jsonl(store.card_meta_path())
    assert len(records) == 12
    assert {record['ui_key'] for record in records} == {f'card-{index}' for index in range(12)}


def test_batch_survives_exception_and_flushes(env):
    """批量块里抛异常时也要把已写的落进热缓存，不能吞掉整批。"""
    store, api, _counter = env

    with pytest.raises(RuntimeError):
        with store.batch_card_meta():
            store.update_card_meta('card-a', {'n': 1})
            raise RuntimeError('boom')

    snapshot = api.private.read().get('card_meta') or {}
    assert 'card-a' in snapshot


def test_nested_batch_flushes_once_at_outermost_exit(env):
    """嵌套调用只在外层收尾时同步一次。"""
    store, _api, counter = env

    with store.batch_card_meta():
        store.update_card_meta('card-a', {'n': 1})
        with store.batch_card_meta():
            store.update_card_meta('card-b', {'n': 2})

    assert counter.updates == 1
