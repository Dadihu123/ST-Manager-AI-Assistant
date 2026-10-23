"""世界书精简的多书作用域（`/wi/entries` + `/jobs/wi-rewrite` + 落地）。

「AI 世界书精简」的入口有三种：
- 批量菜单（世界书视图选中 N 本）
- 右键世界书分类（该分类下全部世界书）
- 右键卡片分类（这些卡片的内嵌书）

后两种天然是**多本**。旧实现只处理 `books[0]`，于是「分类下 12 本世界书」
实际只改了第一本，而且回写时会把候选写进第一个来源。这里钉住：

1. `/wi/entries` 接受数组来源，按书返回分组（含读不到的书）；
2. 提交时每个作业项自带 `worldbook_source`；
3. 落地时按条目自带的来源分组（不会串书）。
"""

import os
import shutil
import sys
import uuid

import flask
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (os.path.join(ROOT, 'tests'), ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

from conftest import load_backend_submodule  # noqa: E402
from fake_api import FakeApi  # noqa: E402

pytest.importorskip('flask', reason='装配层依赖 flask（主程序运行环境必备）')

routes_module = load_backend_submodule('routes')
store_module = load_backend_submodule('store')
jobs_module = load_backend_submodule('jobs')

CARD = {
    'name': 'Alice',
    'description': 'desc',
    'personality': 'p',
    'scenario': 's',
    'first_mes': 'f',
    'mes_example': '',
    'data': {},
}


@pytest.fixture()
def workdir():
    base = os.path.join(ROOT, f'.wi-probe-{os.getpid()}-{uuid.uuid4().hex[:8]}')
    os.makedirs(base, exist_ok=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def build_client(workdir, books):
    """装配只含插件路由的 Flask 测试客户端；`books` 决定世界书读取结果。"""
    api = FakeApi(
        data_dir=workdir,
        cards={'Alice.png': dict(CARD)},
        secrets={'api_key': 'sk-test'},
        tag_library={'tags': [], 'taxonomy': {}, 'prefs': {}},
    )

    def read_worldbook(source):
        if not isinstance(source, dict):
            return None
        key = source.get('file_path') or source.get('card_id') or ''
        return books.get(key)

    written = {}

    def write_worldbook(source, data, expected_revision=''):
        key = source.get('file_path') or source.get('card_id') or ''
        written[key] = [entry.get('content') for entry in data.get('entries') or []]
        return {'success': True, 'source_revision': 'r2', 'msg': '', 'error': ''}

    api.worldbook.read = read_worldbook
    api.worldbook.write = write_worldbook

    store = store_module.PluginStore(api)
    store.save_provider({
        'id': 'p1',
        'name': 'P1',
        'base_url': 'https://example.com/v1',
        'model': 'm1',
    })
    # 专属密钥：save_provider 只管 provider 元数据，密钥单独写
    store.set_provider_secrets('p1', api_key='sk-p1')
    routes_module.register_routes(api, store)

    app = flask.Flask(__name__)
    app.config['TESTING'] = True
    for rule, methods, func in api.routes:
        app.add_url_rule(rule, endpoint=func.__name__, view_func=func, methods=list(methods))

    return app.test_client(), api, store, written


def _book(entries):
    return {'entries': entries, 'source_revision': 'r1'}


BOOKS = {
    'lorebooks/a.json': _book([
        {'uid': '1', 'comment': 'A1', 'content': 'aa', 'keys': []},
        {'uid': '2', 'comment': 'A2', 'content': 'bb', 'keys': []},
    ]),
    'lorebooks/b.json': _book([
        {'uid': '1', 'comment': 'B1', 'content': 'cc', 'keys': []},
    ]),
}


# ---------------------------------------------------------------- uid = 0
#
# ST 世界书的 uid 从 **0** 开始编号，而 `entry.get('uid') or entry.get('id')` 会把
# 整数 0 当成「缺失」：第一条在 `/wi/entries` 里 uid 变成空串 → 面板上勾不动、
# 提交后被 `if not uid: continue` 跳过 → 用户看到的是「每本书第一条改不了」。
# 统一走 `trimming.entry_uid()`（它用 `is None` 判断）之后这三处都要能过。

ZERO_BOOKS = {
    'lorebooks/zero.json': _book([
        {'uid': 0, 'comment': 'Z0', 'content': 'zz0', 'keys': []},
        {'uid': 1, 'comment': 'Z1', 'content': 'zz1', 'keys': []},
    ]),
}

ZERO_SOURCE = {'source_type': 'global', 'file_path': 'lorebooks/zero.json'}


def test_entry_with_uid_zero_is_listed_and_submittable(workdir, monkeypatch):
    client, api, _store, _written = build_client(workdir, ZERO_BOOKS)

    listed = client.post('/wi/entries', json={
        'worldbook_source': [ZERO_SOURCE],
    }).get_json()
    assert [item['uid'] for item in listed['items']] == ['0', '1'], (
        'uid=0 不能被当成「没有 uid」'
    )

    captured = {}

    def fake_submit(job_type, *, items, payload=None):
        captured['items'] = items
        return 'run-zero'

    monkeypatch.setattr(api.jobs, 'submit', fake_submit)
    response = client.post('/jobs/wi-rewrite', json={
        'worldbook_source': [ZERO_SOURCE],
        'entry_uids': ['0'],
        'provider_id': 'p1',
    })

    assert response.get_json()['success'] is True
    assert [item['entry_uid'] for item in captured['items']] == ['0']


def test_apply_writes_back_to_uid_zero_entry(workdir, monkeypatch):
    """落地时要按 uid='0' 找到**第一条**，不能顺着找到第二条。"""
    client, api, store, written = build_client(workdir, ZERO_BOOKS)
    items = [{
        'key': '0', 'status': 'done',
        'result': {
            'entry_uid': '0', 'candidate': 'rewritten-0',
            'worldbook_source': ZERO_SOURCE,
        },
    }]
    monkeypatch.setattr(routes_module, '_load_job_items', lambda *a, **k: items)

    app = client.application
    with app.test_request_context('/jobs/run/apply', method='POST', json={}):
        payload = routes_module._apply_wi_rewrite(
            api, store, 'run-zero', {'payload': {}}, {},
        ).get_json()

    assert payload['success'] is True
    assert payload['applied'] == 1
    assert written['lorebooks/zero.json'] == ['rewritten-0', 'zz1']


def test_trimming_entry_uid_handles_zero_and_missing():
    """`entry_uid` 的契约：0 是有效值，缺失才退回 id / 空串。"""
    trimming = load_backend_submodule('core.trimming')

    assert trimming.entry_uid({'uid': 0}) == '0'
    assert trimming.entry_uid({'uid': '0'}) == '0'
    assert trimming.entry_uid({'uid': 12}) == '12'
    assert trimming.entry_uid({'id': 3}) == '3'
    assert trimming.entry_uid({'uid': None, 'id': 4}) == '4'
    assert trimming.entry_uid({}) == ''
    assert trimming.entry_uid(None) == ''


# ---------------------------------------------------------------- /wi/entries

def test_list_entries_returns_per_book_groups(workdir):
    """`/wi/entries` 必须按书返回，而不是把所有条目拍平。"""
    client, _api, _store, _written = build_client(workdir, BOOKS)
    response = client.post('/wi/entries', json={
        'worldbook_source': [
            {'source_type': 'global', 'file_path': 'lorebooks/a.json'},
            {'source_type': 'global', 'file_path': 'lorebooks/b.json'},
        ],
    })

    payload = response.get_json()
    assert payload['success'] is True, payload
    assert payload['sources'] == 2
    assert payload['total'] == 3
    assert [book['total'] for book in payload['books']] == [2, 1]
    # 拍平的 items 仍保留，供简单场景使用
    assert len(payload['items']) == 3


def test_list_entries_marks_unreadable_book(workdir):
    """一本书读不到时只标记那本，不让整个分类失败。"""
    client, _api, _store, _written = build_client(workdir, BOOKS)
    response = client.post('/wi/entries', json={
        'worldbook_source': [
            {'source_type': 'global', 'file_path': 'lorebooks/a.json'},
            {'source_type': 'global', 'file_path': 'lorebooks/missing.json'},
        ],
    })

    payload = response.get_json()
    assert payload['success'] is True, payload
    assert [book['readable'] for book in payload['books']] == [True, False]
    assert payload['books'][1]['message']


# ---------------------------------------------------------------- 落地分组

def test_apply_groups_by_item_level_source(workdir, monkeypatch):
    """落地必须按条目自带来源分组：同一 uid 分属不同书时不能串。"""
    client, api, store, written = build_client(workdir, BOOKS)

    items = [
        {
            'key': '1', 'status': 'done',
            'result': {
                'entry_uid': '1', 'candidate': 'candidate-A',
                'worldbook_source': {'source_type': 'global', 'file_path': 'lorebooks/a.json'},
            },
        },
        {
            'key': '1', 'status': 'done',
            'result': {
                'entry_uid': '1', 'candidate': 'candidate-B',
                'worldbook_source': {'source_type': 'global', 'file_path': 'lorebooks/b.json'},
            },
        },
    ]

    monkeypatch.setattr(routes_module, '_load_job_items', lambda _api, _run, **kw: items)
    # `_apply_wi_rewrite` 返回 `jsonify(...)`，取 JSON 需要请求上下文
    app = client.application
    with app.test_request_context('/jobs/run-x/apply', method='POST', json={}):
        result = routes_module._apply_wi_rewrite(
            api, store, 'run-x', {'payload': {'worldbook_source': None}}, {},
        )
        payload = result.get_json()

    assert payload['success'] is True, payload
    assert payload['applied'] == 2
    # 每本书只被改写「属于它的那一条」，另一条原文保持不变
    assert written['lorebooks/a.json'] == ['candidate-A', 'bb']
    assert written['lorebooks/b.json'] == ['candidate-B']


def test_submit_attaches_source_per_item(workdir, monkeypatch):
    """提交的每个作业项都要带 `worldbook_source`（多书作业的唯一归属依据）。"""
    client, api, store, _written = build_client(workdir, BOOKS)
    captured = {}

    def fake_submit(_job_type, *, items, payload=None):
        captured['items'] = items
        captured['payload'] = payload
        return 'run-multi'

    monkeypatch.setattr(api.jobs, 'submit', fake_submit)

    response = client.post('/jobs/wi-rewrite', json={
        'worldbook_source': [
            {'source_type': 'global', 'file_path': 'lorebooks/a.json'},
            {'source_type': 'global', 'file_path': 'lorebooks/b.json'},
        ],
        'entry_uids': ['1', '2'],
        'provider_id': 'p1',
    })

    payload = response.get_json()
    assert payload['success'] is True, payload
    assert payload['total'] == 3, '两本书里的 uid=1 与 uid=2 都应进入作业'

    by_source = {}
    for item in captured['items']:
        source = item['worldbook_source']
        by_source.setdefault(source['file_path'], []).append(item['entry_uid'])
    assert by_source == {
        'lorebooks/a.json': ['1', '2'],
        'lorebooks/b.json': ['1'],
    }
    # 多本时作业级来源为空（避免落地时被它覆盖）
    assert captured['payload']['worldbook_source'] is None


def test_submit_single_book_keeps_job_level_source(workdir, monkeypatch):
    """单本作业仍把来源写进作业级 payload（兼容既有链路与测试）。"""
    client, api, store, _written = build_client(workdir, BOOKS)
    captured = {}

    def fake_submit(_job_type, *, items, payload=None):
        captured['payload'] = payload
        captured['items'] = items
        return 'run-single'

    monkeypatch.setattr(api.jobs, 'submit', fake_submit)

    response = client.post('/jobs/wi-rewrite', json={
        'worldbook_source': {'source_type': 'global', 'file_path': 'lorebooks/a.json'},
        'provider_id': 'p1',
    })

    payload = response.get_json()
    assert payload['success'] is True, payload
    assert captured['payload']['worldbook_source'] == {
        'source_type': 'global', 'file_path': 'lorebooks/a.json',
    }
    assert all(item['worldbook_source'] for item in captured['items'])
