"""打标作业的单卡本地开销：3000 个标签时必须「一次建池、零次重复分类」。

诊断与实测见 `tmp/audit/PROMPT-tag-job-perf.md`（改前 129.8ms/卡）：

| | 改前 | 改后 |
|---|---|---|
| 单卡本地开销（3000 标签） | 129.8 ms | ~11 ms |
| `classify_tags()` | 4.0 次/卡 | 0 次/卡（作业开头算一次） |
| `build_pool_text()` | 2.0 次/卡 | 1 次/卡 |

这个文件是**端到端守卫**：走真实 `analyze_one()`（LLM 与卡片读取打桩），
钉住「本地 CPU 不随标签数爆炸」。精确数字用
`python tmp/audit/perf/bench_analyze_one_local.py 3000 7` 复跑，不在这里断言。
"""

import json
import os
import statistics
import time

import pytest

from conftest import load_backend_submodule
from fake_api import FakeApi

pytest.importorskip('flask', reason='装配层依赖 flask（主程序运行环境必备）')

jobs_module = load_backend_submodule('jobs')
store_module = load_backend_submodule('store')
tagpool_core = load_backend_submodule('core.tagpool')
profiles_core = load_backend_submodule('core.profiles')

REPLY = json.dumps(
    {'summary': '概览', 'tags': ['标签0001'], 'suggested_new_tags': []},
    ensure_ascii=False,
)

#: 比实测（~11ms）宽 ~3.5 倍。只为拦住数量级回归（重复分类会让它回到 130ms），
#: 不追求精确基准 —— 慢机器上抖动个几毫秒不该误报。
LOCAL_COST_BUDGET_MS = 40


def build_library(tag_count=3000, category_count=40):
    categories = [f'分类{index:02d}' for index in range(category_count)]
    tags = [f'标签{index:04d}' for index in range(tag_count)]
    return {
        'tags': tags,
        'taxonomy': {
            'tag_to_category': {tag: categories[i % category_count] for i, tag in enumerate(tags)},
            'category_tag_order': {name: [] for name in categories},
            'default_category': '未整理',
        },
        'prefs': {},
    }


class StubCtx:
    """`analyze_one` 只用到 ctx 的取消位与日志口。"""

    run_id = 'run-local-cost'
    items = []

    def should_cancel(self):
        return False

    def log(self, *args, **kwargs):
        pass

    def set_message(self, *args, **kwargs):
        pass

    def report(self, *args, **kwargs):
        pass


def run_analyze_one(api, store, library, usage, tag_index):
    return jobs_module.analyze_one(
        api=api, store=store, config={'apply_mode': 'review', 'tag_category_rules': {}},
        ctx=StubCtx(), card_id='card-0.png', ui_key='card-0.png', content_hash='',
        mode='both', profile=profiles_core.builtin_profile('builtin-default'),
        limits={'max_tags': 20, 'summary_max_chars': 300}, model_name='stub',
        provider={'id': 'p', 'base_url': 'http://x', 'api_style': 'openai_chat'},
        api_key='sk-test', extra_headers={}, tag_library=library,
        known_tags={str(tag) for tag in library['tags']}, usage_map=usage,
        wi_budget=800, keywords=['世界观'], apply_mode='review', dry_run=True,
        force=False, strict=False, apply_tags_enabled=False, tag_index=tag_index,
    )


def test_analyze_one_local_cost_stays_flat_at_3000_tags(tmp_path, monkeypatch):
    library = build_library()
    usage = {f'标签{index:04d}': index % 7 for index in range(3000)}
    card = {
        'name': '测试卡片',
        'description': '北境骑士，常年积雪的山谷里长大。' * 20,
        'first_mes': '（她抬头看你）你来做什么？',
        'character_book': {'entries': [
            {'comment': f'条目{index}', 'content': '世界观内容。' * 20} for index in range(10)
        ]},
    }
    api = FakeApi(
        data_dir=str(tmp_path / 'data'),
        cards={'card-0.png': card},
        secrets={'api_key': 'sk-test'},
        tag_library=library,
    )
    store = store_module.PluginStore(api)
    monkeypatch.setattr(
        jobs_module.llm, 'chat',
        lambda *args, **kwargs: {'content': REPLY, 'usage': {}},
    )

    tag_index = tagpool_core.classify_tags(library)
    # 预热：首次调用会编译正则、填缓存，不该算进计时
    run_analyze_one(api, store, library, usage, tag_index)

    samples = []
    for _ in range(3):
        started = time.perf_counter()
        run_analyze_one(api, store, library, usage, tag_index)
        samples.append((time.perf_counter() - started) * 1000)

    median_ms = statistics.median(samples)
    assert median_ms < LOCAL_COST_BUDGET_MS, (
        f'3000 标签时单卡本地开销 {median_ms:.1f}ms（预算 {LOCAL_COST_BUDGET_MS}ms）：'
        '疑似又回到了「每卡重复分类 / 重复构造标签池」'
    )
