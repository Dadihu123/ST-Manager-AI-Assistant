"""写入失败必须可见，以及热缓存的预算不能吃掉整个命名空间配额。

诊断见 `tmp/audit/ANALYSIS.md`「问题 2」。

两个坑：

1. **静默失败**。宿主在命名空间超过 262144 字节时会拒绝整笔写入，
   `store._mutate()` 只返回 `False`，而所有调用方都丢掉了返回值 ——
   标签建议、作业台账、待审批角标、档案索引全部停在旧值，接口仍然 `success`，
   用户只看到「界面不动了」。至少要留下可查的日志。
2. **预算冲突**。`CARD_META_UI_BYTES_LIMIT` 是 200KB，而整个命名空间只有
   256KB：一个子键吃掉 78%，其余 8 个子键只剩 57KB。热缓存应当按「配额减去
   其它子键实际占用」给自己定预算，而不是固定占 200KB。
"""

import json

import pytest

from conftest import load_backend_submodule
from fake_api import FakeApi

pytest.importorskip('flask', reason='装配层依赖 flask（主程序运行环境必备）')
store_module = load_backend_submodule('store')

PluginStore = store_module.PluginStore

#: 宿主配额（core/plugins/api.py:72），复刻在此以免依赖主程序。
HOST_QUOTA = 256 * 1024


class QuotaHost:
    """复刻宿主 `_ui_data_update`：超配额整笔拒绝（原 entry 不变）。"""

    def __init__(self, data_dir, quota=HOST_QUOTA):
        self.ns = {}
        self.dir = data_dir
        self.quota = quota
        self.rejected = 0

    def read(self):
        return json.loads(json.dumps(self.ns, ensure_ascii=False))

    def update(self, mutator):
        entry = json.loads(json.dumps(self.ns, ensure_ascii=False))
        mutator(entry)
        size = len(json.dumps(entry, ensure_ascii=False).encode('utf-8'))
        if size > self.quota:
            self.rejected += 1
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


@pytest.fixture()
def env(tmp_path):
    api = FakeApi(data_dir=str(tmp_path / 'data'), secrets={'api_key': 'sk-test'})
    host = QuotaHost(str(tmp_path / 'data'))
    api.private = QuotaPrivate(host)
    return PluginStore(api), api, host


def fill_beyond_quota(host):
    """把命名空间塞到刚好超过宿主配额，使任何新增写入都被拒绝。"""
    host.ns = {'tag_suggestions': {'占位标签': {'reason': 'r' * HOST_QUOTA}}}
    probe = dict(host.ns)
    probe['__probe__'] = 1
    assert len(json.dumps(probe, ensure_ascii=False).encode('utf-8')) > HOST_QUOTA


def make_record(index, blob_size=4000):
    return {
        'ui_key': f'card-{index}',
        'updated_at': 1762560000 + index,
        'summary': '概' * (blob_size // 3),
        'tags_applied': ['女仆', '傲娇', '校园'],
    }


# ------------------------------------------------------------------ 失败可见


def test_mutate_failure_is_logged(env, caplog):
    """命名空间超配额时，写入失败至少要留下一条 warning。"""
    store, api, host = env
    fill_beyond_quota(host)

    with caplog.at_level('WARNING', logger='stm_ai_backend.store'):
        store.record_run({'run_id': 'run-a', 'task': 'analyze'})

    assert any('ui_data' in record.message or '写入' in record.message for record in caplog.records), (
        '写入被拒时必须记日志；改前 _mutate 只返回 False，所有调用方都丢掉返回值，'
        '用户只能看到「界面不动了」'
    )


def test_mutate_failure_is_recorded_for_health_surface(env):
    """失败要能被 /status 之类的地方读到（而不是只在日志里）。"""
    store, api, host = env
    fill_beyond_quota(host)

    store.record_run({'run_id': 'run-a'})

    state = store.last_write_state()
    assert state['ok'] is False
    assert state['failed_writes'] >= 1
    assert state['message']


def test_successful_write_clears_failure_state(env):
    store, api, host = env
    fill_beyond_quota(host)
    store.record_run({'run_id': 'run-a'})
    assert store.last_write_state()['ok'] is False

    host.ns = {}  # 用户清掉了体积，写入恢复
    store.record_run({'run_id': 'run-b'})
    assert store.last_write_state()['ok'] is True


# ------------------------------------------------------------------ 动态预算


def test_card_meta_budget_respects_other_subkeys(env):
    """其它子键占得多时，热缓存要自动缩小，不能固定占 200KB。"""
    store, api, host = env
    # 其它子键先占约 150KB
    api.private.update(lambda entry: entry.update({
        'tag_suggestions': {'候选%03d' % i: {'reason': 'r' * 200} for i in range(600)},
    }))
    other_bytes = len(json.dumps(host.ns, ensure_ascii=False).encode('utf-8'))
    assert other_bytes > 120 * 1024, f'前置条件：其它子键应占掉一大块，实际 {other_bytes}'

    store._card_meta_cache = {
        f'card-{index}': make_record(index) for index in range(200)
    }
    assert store.sync_card_meta_ui() is True

    total = len(json.dumps(host.ns, ensure_ascii=False).encode('utf-8'))
    assert total <= HOST_QUOTA, f'同步后命名空间 {total} 字节仍超过宿主配额'
    snapshot = host.ns.get('card_meta') or {}
    assert snapshot, '预算变小也要保留能放下的那部分'
    assert len(snapshot) < 200, '其它子键占满时，热缓存应被裁得更小'


def test_card_meta_budget_can_still_use_full_allowance(env):
    """其它子键几乎没占地方时，热缓存不该被无谓压小。"""
    store, api, host = env
    api.private.update(lambda entry: entry.update({'runs': {}}))

    store._card_meta_cache = {
        f'card-{index}': make_record(index, blob_size=300) for index in range(300)
    }
    store.sync_card_meta_ui()

    snapshot = host.ns.get('card_meta') or {}
    assert len(snapshot) == 300, '预算充足时应把全部记录放进去'


def test_syncing_twice_is_stable(env):
    """重复同步不应因为「上一轮自己写进去的体积」而越裁越小。"""
    store, api, host = env
    store._card_meta_cache = {
        f'card-{index}': make_record(index, blob_size=600) for index in range(150)
    }

    store.sync_card_meta_ui()
    first = len(host.ns.get('card_meta') or {})
    store.sync_card_meta_ui()
    second = len(host.ns.get('card_meta') or {})

    assert first == second, f'两次同步保留条数不同（{first} → {second}），预算计算不稳定'
