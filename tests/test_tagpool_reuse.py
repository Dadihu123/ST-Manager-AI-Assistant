"""作业内复用标签分类结果（0.4.0 的性能放大器）。

诊断见 `tmp/audit/ANALYSIS.md`「问题 3 · 放大器」。

0.4.0 让 `analyze_one()`（每张卡都跑）做了两次**全量**标签分类：

- `tagpool.classify_tags(tag_library)`（`jobs.py:574`）
- `tagpool.protected_tags(tag_library, pool_spec)`（`jobs.py:723`，内部又调一次）

成本随标签数近似平方增长。实测（`tmp/audit/perf/bench_tagpool_040.py`）：
本机 76 个标签时合计 0.53ms/卡（无感），但 3000 个标签时 **89.9ms/卡**，
300 卡作业凭空多 27 秒。

标签库在一次作业里是**同一份**（`read_tag_library(api)` 在作业开头读一次就
传给每张卡），所以这些分类结果完全可以在作业范围内复用。
"""

import os
import shutil
import uuid

import pytest

from conftest import ROOT, load_backend_submodule

store_module = load_backend_submodule('store')
prompt_core = load_backend_submodule('core.prompt')
tagpool_core = load_backend_submodule('core.tagpool')


def build_library(tag_count=400):
    categories = [f'分类{i:02d}' for i in range(10)]
    tags = [f'标签{i:04d}' for i in range(tag_count)]
    return {
        'tags': tags,
        'order': [],
        'order_enabled': False,
        'order_enabled_keys': None,
        'taxonomy': {
            'tag_to_category': {tag: categories[i % len(categories)] for i, tag in enumerate(tags)},
            'category_tag_order': {name: [] for name in categories},
            'default_category': '未整理',
        },
        'prefs': {},
    }


def test_protected_tags_reuses_precomputed_index():
    """`protected_tags` 应当能接收现成索引，不再自己重算一遍分类。"""
    library = build_library()
    index = tagpool_core.classify_tags(library)
    spec = prompt_core.profile_tag_pool({}, {})

    calls = {'count': 0}
    real = tagpool_core.classify_tags

    def counting(lib):
        calls['count'] += 1
        return real(lib)

    tagpool_core.classify_tags = counting
    try:
        result = tagpool_core.protected_tags(library, spec, index=index)
    finally:
        tagpool_core.classify_tags = real

    assert isinstance(result, set)
    assert calls['count'] == 0, (
        '传入 index 后 protected_tags 不该再自己 classify_tags 一遍：'
        '0.4.0 让它在每张卡上都跑一次，大标签库单次 27ms'
    )


def test_protected_tags_still_works_without_index():
    """不传 index 时保持原行为（其它调用点不受影响）。"""
    library = build_library()
    spec = prompt_core.profile_tag_pool({}, {'tag_pool_categories_exclude': '分类00'})
    result = tagpool_core.protected_tags(library, spec)
    assert isinstance(result, set)


def _count_classify_calls(monkeypatch):
    """把 `classify_tags` 包一层计数；返回可读的计数字典。"""
    calls = {'count': 0}
    real = tagpool_core.classify_tags

    def counting(library):
        calls['count'] += 1
        return real(library)

    monkeypatch.setattr(tagpool_core, 'classify_tags', counting)
    return calls


def test_resolve_candidates_reuses_precomputed_index(monkeypatch):
    """`resolve_candidates` 接受现成索引后，不该再自己 classify 一遍。"""
    library = build_library()
    spec = prompt_core.profile_tag_pool({}, {})
    index = tagpool_core.classify_tags(library)
    calls = _count_classify_calls(monkeypatch)

    reused = tagpool_core.resolve_candidates(library, spec, index=index)
    assert calls['count'] == 0, '传了 index 还在重算：这是每卡两次里的一半'

    legacy = tagpool_core.resolve_candidates(library, spec)
    assert calls['count'] == 1, '不传 index 时必须保持原行为（自己算一遍）'

    # 两条路径的输出必须一模一样
    assert reused['allowed'] == legacy['allowed']
    assert reused['excluded'] == legacy['excluded']
    assert reused['blocked'] == legacy['blocked']
    assert reused['isolated_categories'] == legacy['isolated_categories']


def test_build_pool_text_reuses_precomputed_index(monkeypatch):
    """`build_pool_text` 接受现成索引后，内部两次 classify 都该省掉。"""
    library = build_library()
    spec = prompt_core.profile_tag_pool({}, {})
    card = {'name': '测试卡', 'description': '校园里的恋爱故事'}
    usage = {f'标签{i:04d}': i % 5 for i in range(400)}
    index = tagpool_core.classify_tags(library)
    calls = _count_classify_calls(monkeypatch)

    reused = tagpool_core.build_pool_text(library, spec, card=card, usage=usage, index=index)
    assert calls['count'] == 0, '传了 index 还在重算（自己和 resolve_candidates 各一次）'

    legacy = tagpool_core.build_pool_text(library, spec, card=card, usage=usage)
    assert calls['count'] == 1, (
        '不传 index 时自己算一次即可；内层 resolve_candidates 复用同一份，不该再算第二次'
    )

    # `tags.library` 的文本是唯一不可动的契约：两条路径必须逐字节相同
    assert reused['text'] == legacy['text']
    for key in ('outline', 'listed', 'dropped', 'excluded', 'available', 'tokens'):
        assert reused[key] == legacy[key], f'{key} 不一致'

