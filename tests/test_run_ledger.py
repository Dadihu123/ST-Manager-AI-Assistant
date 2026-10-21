"""作业台账（`store.record_run` / `update_run` / `list_runs` / `forget_run`）。

宿主 `api.jobs` 没有 list 能力，所以插件自己记一份轻量台账，供「最近作业」
与「关掉面板后找回上次作业」使用（这也是 F10 的修复点）。这里锁定：

1. 写入即读得到，且按时间倒序；
2. 同一 `run_id` 重复写入是**合并**而不是新增一条（否则重试会留下残影）；
3. 台账只保留最近 50 条（避免 `ui_data` 无限增长）。
"""

import os
import shutil
import uuid

import pytest

from conftest import load_backend_submodule
from fake_api import FakeApi

pytest.importorskip('flask', reason='装配层依赖 flask（主程序运行环境必备）')

store_module = load_backend_submodule('store')
PluginStore = store_module.PluginStore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def store():
    """在**仓库内**建临时数据目录。

    刻意不用 `tmp_path` / `tempfile.mkdtemp`：
    - 某些受限环境里系统临时目录不可写（`PermissionError`）；
    - `mkdtemp` 在 Windows 上会给出带受限 ACL 的目录，测完**删不掉**，
      留下一堆 `.ledger-*` 残骸。改用 `os.makedirs` 建普通目录即可正常清理。
    """
    base = os.path.join(ROOT, f'.ledger-{os.getpid()}-{uuid.uuid4().hex[:8]}')
    os.makedirs(base, exist_ok=True)
    try:
        api = FakeApi(data_dir=base, secrets={'api_key': 'sk-test'})
        yield PluginStore(api)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_record_run_round_trip(store):
    store.record_run({'run_id': 'run-a', 'task': 'analyze', 'job_type': 'ai.analyze', 'total': 3})

    runs = store.list_runs()
    assert len(runs) == 1
    assert runs[0]['run_id'] == 'run-a'
    assert runs[0]['task'] == 'analyze'
    assert runs[0]['total'] == 3
    assert runs[0]['created_at'] > 0


def test_record_run_merges_same_run_id(store):
    store.record_run({'run_id': 'run-a', 'task': 'analyze', 'status': 'queued'})
    store.record_run({'run_id': 'run-a', 'status': 'completed'})

    runs = store.list_runs()
    assert len(runs) == 1, '同一 run_id 不应产生两条台账'
    assert runs[0]['status'] == 'completed'
    assert runs[0]['task'] == 'analyze', '合并不能丢掉之前写入的字段'


def test_record_run_sorts_newest_first(store):
    store.record_run({'run_id': 'old', 'created_at': 100})
    store.record_run({'run_id': 'new', 'created_at': 200})

    assert [run['run_id'] for run in store.list_runs()] == ['new', 'old']


def test_record_run_keeps_only_50(store):
    for index in range(60):
        store.record_run({'run_id': f'run-{index}', 'created_at': 1000 + index})

    runs = store.list_runs()
    assert len(runs) == 50
    assert runs[0]['run_id'] == 'run-59'


def test_record_run_ignores_empty_run_id(store):
    assert store.record_run({'run_id': ''}) == {}
    assert store.list_runs() == []


def test_update_run_patches_existing(store):
    store.record_run({'run_id': 'run-a', 'status': 'queued'})

    assert store.update_run('run-a', {'status': 'applied'}) is True
    assert store.list_runs()[0]['status'] == 'applied'


def test_update_run_unknown_id_is_noop(store):
    assert store.update_run('missing', {'status': 'applied'}) is False


def test_forget_run_removes_record(store):
    store.record_run({'run_id': 'run-a'})
    store.forget_run('run-a')
    assert store.list_runs() == []
