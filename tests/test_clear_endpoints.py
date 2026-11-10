"""清除端点的 HTTP 契约。

用户要的「手动清除」最终落在这些端点上（前端只负责调用）：

- `DELETE /jobs/<run_id>`   删一条作业记录（连带它的待审批登记）
- `POST   /jobs/clear`      清空最近作业台账
- `POST   /usage/clear`     清空用量明细（默认保留累计台账）
- `POST   /pending/clear`   清空 / 定点清除待审批登记

形状约定（与既有端点一致）：`success: true` + 被清除的数量/清单。
"""

import json

import pytest

flask = pytest.importorskip('flask', reason='路由测试需要 flask（主程序运行环境必备）')

from conftest import load_backend_submodule  # noqa: E402
from fake_api import FakeApi  # noqa: E402

routes_module = load_backend_submodule('routes')
store_module = load_backend_submodule('store')


@pytest.fixture()
def env(tmp_path):
    api = FakeApi(
        data_dir=str(tmp_path / 'data'),
        secrets={'api_key': 'sk-test'},
        tag_library={'tags': ['奇幻'], 'taxonomy': {}, 'prefs': {}},
    )
    store = store_module.PluginStore(api)
    routes_module.register_routes(api, store)

    app = flask.Flask(__name__)
    app.config['TESTING'] = True
    for rule, methods, func in api.routes:
        app.add_url_rule(rule, endpoint=func.__name__, view_func=func, methods=list(methods))
    return app.test_client(), api, store


def test_delete_job_removes_record_and_pending(env):
    client, _api, store = env
    store.record_run({'run_id': 'run-a', 'task': 'analyze'})
    store.mark_pending_review('run-a', 'ai.analyze', 2)

    response = client.delete('/jobs/run-a')
    payload = response.get_json()

    assert response.status_code == 200
    assert payload['success'] is True
    assert store.list_runs() == []
    assert store.list_pending_reviews() == {}, '删作业记录必须连带清掉它的待审批登记'


def test_delete_unknown_job_is_not_an_error(env):
    """前端可能对已经消失的作业点删除，不应 500。"""
    client, _api, _store = env
    response = client.delete('/jobs/does-not-exist')
    assert response.status_code == 200
    assert response.get_json()['success'] is True


def test_clear_jobs_empties_ledger(env):
    client, _api, store = env
    for index in range(5):
        store.record_run({'run_id': f'run-{index}', 'created_at': 100 + index})

    payload = client.post('/jobs/clear').get_json()

    assert payload['success'] is True
    assert payload['removed'] == 5
    assert store.list_runs() == []


def test_clear_usage_keeps_ledger_by_default(env):
    client, _api, store = env
    for index in range(4):
        store.record_usage({'prompt_tokens': index, 'completion_tokens': 0})

    payload = client.post('/usage/clear').get_json()

    assert payload['success'] is True
    assert payload['removed'] == 4
    assert payload['ledger_reset'] is False
    assert store.recent_usage(20) == []
    assert store.usage_ledger()['jobs'] == 4, '默认保留累计用量'


def test_clear_usage_can_reset_ledger(env):
    client, _api, store = env
    store.record_usage({'prompt_tokens': 10, 'completion_tokens': 2})

    payload = client.post('/usage/clear', json={'reset_ledger': True}).get_json()

    assert payload['success'] is True
    assert payload['ledger_reset'] is True
    assert store.usage_ledger()['jobs'] == 0


def test_clear_pending_all_and_targeted(env):
    client, _api, store = env
    store.mark_pending_review('run-a', 'ai.analyze', 1)
    store.mark_pending_review('run-b', 'ai.analyze', 1)

    targeted = client.post('/pending/clear', json={'run_ids': ['run-a']}).get_json()
    assert targeted['removed'] == ['run-a']
    assert set(store.list_pending_reviews()) == {'run-b'}

    everything = client.post('/pending/clear').get_json()
    assert everything['removed'] == ['run-b']
    assert store.list_pending_reviews() == {}


def test_status_reflects_cleared_pending(env):
    """清空待审批后，`/status` 里 hub 用的那个计数必须归零。

    合并后语义：`/status` 的 `pending_reviews` 以**真实条目状态**为准
    （`_live_pending_reviews`），不再直接照抄登记表——因为登记表本身的写入
    会被 ui_data 配额整次拒绝。所以这里必须造出真实作业条目，才能测到
    「有待审批 → 清空 → 归零」这条链路。
    """
    client, api, store = env

    api.job_detail = {'run_id': 'run-a', 'job_type': 'ai.analyze'}
    api.job_items = [
        {'key': 'card-1', 'status': 'awaiting_review'},
        {'key': 'card-2', 'status': 'awaiting_review'},
    ]
    store.mark_pending_review('run-a', 'ai.analyze', 2)

    pending = client.get('/status').get_json()['pending_reviews']
    assert pending, '有待审批条目时必须出现在 /status'
    assert pending['run-a']['items'] == 2

    client.post('/pending/clear')

    assert client.get('/status').get_json()['pending_reviews'] == {}



def test_jobs_list_reflects_cleared_ledger(env):
    client, _api, store = env
    store.record_run({'run_id': 'run-a', 'task': 'analyze', 'created_at': 100})

    client.post('/jobs/clear')

    payload = client.get('/jobs').get_json()
    assert payload['jobs'] == []


def test_clear_suggestions_endpoint_still_works(env):
    """既有端点不能被这轮改动破坏。"""
    client, _api, store = env
    store.record_tag_suggestions([{'name': 'A'}, {'name': 'B'}])

    payload = client.post('/suggestions/clear').get_json()

    assert payload['success'] is True
    assert store.list_tag_suggestions() == {}
