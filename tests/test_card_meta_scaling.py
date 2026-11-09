"""`card_meta` 热缓存的规模行为：同步成本与保留策略。

诊断记录见 `tmp/audit/ANALYSIS.md`「问题 3」。

`update_card_meta()` 每写一张卡就要把热缓存同步进 `ui_data`
（`store.py:792-793`）。旧实现的裁剪是「每删一条就 `json.dumps` 整个字典」，
成本 ∝ 缓存条数 × 200KB，落地一批卡时逐卡放大——用户实测「用 30 分钟后
每次保存都要等很久，重启才缓解」。

这些测试锁的是**行为与规模特性**，不是实现细节：只要「同步一次的开销
不随缓存条数爆炸」且「保留的永远是最新的那批」，怎么实现都行。
"""

import json
import os
import time

import pytest

from conftest import load_backend_submodule
from fake_api import FakeApi

pytest.importorskip('flask', reason='装配层依赖 flask（主程序运行环境必备）')
store_module = load_backend_submodule('store')

PluginStore = store_module.PluginStore


def seed_card_meta_jsonl(store, count, *, summary_chars=150):
    """按真实落盘格式预置 `card_meta.jsonl`，模拟「用了很久」的现场。

    刻意走公开的 `store.card_meta_path()`，而不是直接摸内部缓存：
    用户机器上的缓存就是这么从 jsonl 重建出来的（`load_card_meta()`）。
    """
    path = store.card_meta_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    summary = ('角色概览样本：外貌、性格、关系、世界观与叙事要求。' * 12)[:summary_chars]
    with open(path, 'w', encoding='utf-8') as handle:
        for index in range(count):
            record = {
                'ui_key': f'未分类/卡片_{index:05d}.png',
                'content_hash': f'{index:016x}{index:016x}',
                'profile_id': 'builtin-analyze',
                'model': 'gpt-4o-mini',
                'analyzed_at': 1762560000 + index,
                'updated_at': 1762560000 + index,
                'summary': summary,
                'tags_applied': ['女仆', '傲娇', '校园', '奇幻', '日常'],
                'applied_runs': [f'run-{index:04d}'],
            }
            handle.write(json.dumps(record, ensure_ascii=False) + '\n')


def build_store(tmp_path, count):
    api = FakeApi(data_dir=str(tmp_path / 'data'), secrets={'api_key': 'sk-test'})
    store = PluginStore(api)
    seed_card_meta_jsonl(store, count)
    store.load_card_meta()  # 公开方法：从 jsonl 重建缓存（重启后的真实路径）
    return store, api


@pytest.fixture()
def workdir(tmp_path):
    return tmp_path


def test_card_meta_ui_sync_cost_does_not_explode_with_cache_size(workdir):
    """同步一次热缓存，耗时不能随缓存条数爆炸。

    改前实测（`tmp/audit/perf/bench_sync_card_meta_quadratic.py`）：
    缓存 1200 条时单次同步 634ms；缓存 2000 条时 723ms。
    目标是把这次同步压到与条目数近似线性的量级（改后约 5ms）。

    阈值取 150ms：比目标值宽 20 倍，只为拦住数量级回归，
    不让慢机器上的抖动误报。
    """
    store, _api = build_store(workdir, 1200)

    started = time.perf_counter()
    store.sync_card_meta_ui()
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert elapsed_ms < 150, (
        f'1200 条缓存的单次同步用了 {elapsed_ms:.0f}ms；'
        '裁剪循环退化成 O(N²) 了（每删一条都重新序列化整个字典）'
    )


def test_card_meta_ui_snapshot_keeps_newest_records(workdir):
    """超预算时被挤掉的必须是**最旧**的记录。"""
    store, api = build_store(workdir, 600)
    store.sync_card_meta_ui()

    snapshot = api.private.read().get('card_meta') or {}
    assert snapshot, '热缓存不应为空'

    kept_at = [int(record.get('updated_at') or 0) for record in snapshot.values()]
    # 保留的是 updated_at 最大的一批：最小值应当大于被丢弃的最大值
    dropped = [
        int(record['updated_at'])
        for key, record in store.load_card_meta().items()
        if key not in snapshot
    ]
    assert dropped, '600 条必然超出预算，应当有记录被挤掉'
    assert min(kept_at) > max(dropped), '挤掉的应是最旧记录，不是随机/最新记录'


def test_card_meta_ui_snapshot_never_exceeds_budget(workdir):
    """裁剪结果必须落在预算内（既有契约，回归保护）。"""
    store, api = build_store(workdir, 400)
    store.sync_card_meta_ui()

    snapshot = api.private.read().get('card_meta') or {}
    size = len(json.dumps(snapshot, ensure_ascii=False).encode('utf-8'))
    assert size <= store_module.CARD_META_UI_BYTES_LIMIT
    assert len(snapshot) <= store_module.CARD_META_UI_LIMIT
