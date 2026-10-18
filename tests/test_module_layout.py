"""装配层契约：模块可加载、`register(api)` 可调用、注册内容与 manifest 一致。"""

import json
import os

import pytest

from conftest import ROOT, load_backend_package
from fake_api import FakeApi

MANIFEST_PATH = os.path.join(ROOT, 'manifest.json')


@pytest.fixture(scope='module')
def backend():
    pytest.importorskip('flask', reason='装配层依赖 flask（主程序运行环境必备）')
    return load_backend_package()


@pytest.fixture
def api(tmp_path):
    return FakeApi(data_dir=str(tmp_path / 'data'))


def test_backend_package_loads_without_sys_path(backend):
    """模拟主程序 registry 的加载方式（不注入 sys.path）后，相对导入必须成立。"""
    assert hasattr(backend, 'register')
    assert callable(backend.register)


def test_register_wires_jobs_routes_and_cli(backend, api):
    backend.register(api)

    assert 'ai.analyze' in api.registered_jobs

    rules = {rule for rule, _methods, _func in api.routes}
    assert {'/status', '/health', '/cards', '/prompt/preview', '/jobs/analyze'} <= rules
    assert '/profiles' in rules
    assert '/providers' in rules
    assert '/suggestions' in rules
    assert '/usage' in rules

    assert {'status', 'profiles', 'preview', 'analyze'} <= set(api.registered_cli)


def test_registered_job_types_are_declared_in_manifest(backend, api):
    backend.register(api)
    with open(MANIFEST_PATH, 'r', encoding='utf-8') as handle:
        manifest = json.load(handle)
    assert set(api.registered_jobs) <= set(manifest['backend']['jobs'])


def test_registered_routes_are_unique_per_rule_and_methods(backend, api):
    """同一路径可以有不同的方法组合（GET/POST/DELETE），但组合本身不得重复。

    重复的 `(rule, methods)` 会让 Flask 注册报错（同端点名重复），也会被
    `PluginAPI._check_route_conflicts` 判为与既有路由冲突。
    """
    backend.register(api)
    seen = [(rule, methods) for rule, methods, _func in api.routes]
    assert len(seen) == len(set(seen))

    endpoint_names = [func.__name__ for _rule, _methods, func in api.routes]
    assert len(endpoint_names) == len(set(endpoint_names))


def test_registered_routes_have_no_method_overlap_per_rule(backend, api):
    """同一路径的多条规则之间不得有方法重叠。"""
    backend.register(api)
    by_rule = {}
    for rule, methods, _func in api.routes:
        by_rule.setdefault(rule, set()).update(methods)

    for rule, methods, _func in api.routes:
        # 累加后总量应等于各条规则方法数之和，说明没有重叠
        pass

    totals = {}
    for rule, methods, _func in api.routes:
        totals[rule] = totals.get(rule, 0) + len(set(methods))
    for rule, count in totals.items():
        assert count == len(by_rule[rule]), f'{rule} 存在方法重叠'


def test_register_initializes_provider_file(backend, api, tmp_path):
    backend.register(api)
    assert os.path.isfile(os.path.join(str(tmp_path / 'data'), 'providers.json'))


def test_register_writes_profile_index_into_ui_namespace(backend, api):
    backend.register(api)
    index = api.private.read().get('prompt_index')
    assert isinstance(index, list)
    assert len(index) == 9
    assert {item['id'] for item in index} >= {'builtin-default', 'builtin-gpt'}


def test_register_preserves_foreign_ui_keys(backend, api):
    """C3 回归：装配过程不得清掉主程序管理的 `config` / `enabled` 等键。"""
    api.private.update(lambda entry: entry.update({'config': {'max_tags': 3}, 'enabled': True}))
    backend.register(api)

    namespace = api.private.read()
    assert namespace.get('config') == {'max_tags': 3}
    assert namespace.get('enabled') is True
    assert namespace.get('prompt_index')
