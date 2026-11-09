"""作业记录与用量的上限、老化与手动清除。

诊断见 `tmp/audit/ANALYSIS.md`「问题 1」。用户原话：「最近的作业记录貌似会不断
积累，希望能够添加个手动清除；如果会无限积累，请也增加上限，到了上限后自动
老化旧的记录」。

现状（改前）：

- `runs` 台账封顶 50 条 ✅（但前端没有任何删除入口）
- `usage.jsonl` 名义 8MB 压缩，但记录没有 `ui_key` → 压缩完全不去重，
  越过 8MB 后每次写入都要整文件重读重写
- `pending_reviews` 无上限、无 TTL、无清除端点 —— 只在 apply「有落地」时清，
  取消 / 失败 / 一直没审的作业永久残留
- `tag_suggestions` 每个标签的 `cards` 截 50，但**键**无上限

这里锁的是：每个结构都有一个明确的上限或老化策略，且都有手动清除入口。
"""

import json
import os

import pytest

from conftest import load_backend_submodule
from fake_api import FakeApi

pytest.importorskip('flask', reason='装配层依赖 flask（主程序运行环境必备）')
store_module = load_backend_submodule('store')

PluginStore = store_module.PluginStore


@pytest.fixture()
def store(tmp_path):
    api = FakeApi(data_dir=str(tmp_path / 'data'), secrets={'api_key': 'sk-test'})
    return PluginStore(api)


# ------------------------------------------------------------------ runs 台账


def test_clear_runs_empties_the_ledger(store):
    for index in range(5):
        store.record_run({'run_id': f'run-{index}', 'created_at': 100 + index})

    result = store.clear_runs()

    assert result['removed'] == 5
    assert store.list_runs() == []


def test_clear_runs_keeps_usage_ledger(store):
    """清作业记录不应连带清掉累计用量（那是另一件事）。"""
    store.record_run({'run_id': 'run-a'})
    store.record_usage({'prompt_tokens': 10, 'completion_tokens': 2})

    store.clear_runs()

    assert store.usage_ledger()['jobs'] == 1


def test_forget_run_removes_single_record_and_pending_review(store):
    """删一条作业记录时，它的待审批登记也要一起消失（否则永远清不掉）。"""
    store.record_run({'run_id': 'run-a'})
    store.mark_pending_review('run-a', 'ai.analyze', 3)

    assert store.forget_run('run-a') is True
    assert store.list_runs() == []
    assert store.list_pending_reviews() == {}


# ------------------------------------------------------------------ usage 明细


def test_usage_jsonl_is_trimmed_to_a_cap(store):
    """用量明细超过上限后自动裁掉最旧的，保留最近的一批。"""
    for index in range(store_module.USAGE_JSONL_MAX_RECORDS + 40):
        store.record_usage({'prompt_tokens': index, 'completion_tokens': 0})

    records = store._read_jsonl(store.usage_path())
    assert len(records) <= store_module.USAGE_JSONL_MAX_RECORDS
    # 留下的必须是最新的那批
    assert records[-1]['prompt_tokens'] == store_module.USAGE_JSONL_MAX_RECORDS + 39


def test_trimmed_usage_keeps_ledger_totals(store):
    """裁剪明细不能篡改累计总量（那是 `usage_ledger` 的职责）。"""
    for index in range(store_module.USAGE_JSONL_MAX_RECORDS + 10):
        store.record_usage({'prompt_tokens': 1, 'completion_tokens': 1})

    ledger = store.usage_ledger()
    assert ledger['jobs'] == store_module.USAGE_JSONL_MAX_RECORDS + 10
    assert ledger['total_prompt_tokens'] == store_module.USAGE_JSONL_MAX_RECORDS + 10


def test_clear_usage_records_empties_jsonl_but_keeps_ledger(store):
    for index in range(5):
        store.record_usage({'prompt_tokens': index, 'completion_tokens': 0})

    result = store.clear_usage_records()

    assert result['removed'] == 5
    assert store.recent_usage(20) == []
    assert store.usage_ledger()['jobs'] == 5, '累计用量默认保留'


def test_clear_usage_records_can_reset_ledger(store):
    for index in range(3):
        store.record_usage({'prompt_tokens': 1, 'completion_tokens': 1})

    store.clear_usage_records(reset_ledger=True)

    ledger = store.usage_ledger()
    assert ledger['jobs'] == 0
    assert ledger['total_prompt_tokens'] == 0


# ------------------------------------------------------------------ pending_reviews


def test_pending_reviews_are_capped(store):
    """待审批登记必须有上限，否则它会一直堆（取消/失败/没审的作业都留着）。"""
    for index in range(store_module.PENDING_REVIEW_MAX + 30):
        store.mark_pending_review(f'run-{index}', 'ai.analyze', 1)

    pending = store.list_pending_reviews()
    assert len(pending) <= store_module.PENDING_REVIEW_MAX
    # 保留最新的那批
    assert f'run-{store_module.PENDING_REVIEW_MAX + 29}' in pending


def test_stale_pending_reviews_age_out(store, monkeypatch):
    """超过 TTL 的待审批自动老化（宿主已清理的作业永远等不到 apply）。"""
    now = 1762560000
    monkeypatch.setattr(store_module.time, 'time', lambda: now)
    store.mark_pending_review('run-old', 'ai.analyze', 1)

    past_ttl = now + store_module.PENDING_REVIEW_TTL_SECONDS + 60
    monkeypatch.setattr(store_module.time, 'time', lambda: past_ttl)

    assert store.list_pending_reviews() == {}


def test_pending_review_within_ttl_survives(store, monkeypatch):
    now = 1762560000
    monkeypatch.setattr(store_module.time, 'time', lambda: now)
    store.mark_pending_review('run-fresh', 'ai.analyze', 2)

    just_inside = now + store_module.PENDING_REVIEW_TTL_SECONDS - 60
    monkeypatch.setattr(store_module.time, 'time', lambda: just_inside)

    assert 'run-fresh' in store.list_pending_reviews()


def test_clear_pending_reviews_can_target_one_run(store):
    store.mark_pending_review('run-a', 'ai.analyze', 1)
    store.mark_pending_review('run-b', 'ai.analyze', 1)

    result = store.clear_pending_reviews(['run-a'])

    assert result['removed'] == ['run-a']
    assert set(store.list_pending_reviews()) == {'run-b'}


def test_clear_pending_reviews_all_by_default(store):
    store.mark_pending_review('run-a', 'ai.analyze', 1)
    store.mark_pending_review('run-b', 'ai.analyze', 1)

    result = store.clear_pending_reviews()

    assert sorted(result['removed']) == ['run-a', 'run-b']
    assert store.list_pending_reviews() == {}


# ------------------------------------------------------------------ tag_suggestions


def test_tag_suggestions_are_capped(store):
    """新标签建议的**键**必须有上限（改前只限每标签的 cards 列表）。"""
    for index in range(store_module.TAG_SUGGESTIONS_MAX + 25):
        store.record_tag_suggestions(
            [{'name': f'候选标签{index}', 'reason': 'r'}], card_key=f'card-{index}',
        )

    queue = store.list_tag_suggestions()
    assert len(queue) <= store_module.TAG_SUGGESTIONS_MAX
    # 保留最近出现的那批
    assert f'候选标签{store_module.TAG_SUGGESTIONS_MAX + 24}' in queue


# ------------------------------------------------------------------ 迁移护栏


def test_clear_endpoints_are_idempotent(store):
    """空库上重复清除不应报错（前端可能点两次）。"""
    assert store.clear_runs()['removed'] == 0
    assert store.clear_usage_records()['removed'] == 0
    assert store.clear_pending_reviews()['removed'] == []
