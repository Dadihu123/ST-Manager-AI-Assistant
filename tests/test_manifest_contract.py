"""manifest 契约：与主程序 `core/plugins/manifest.py` 的校验规则对齐。

白名单与正则在此**复刻**，以便在无主程序的环境下也能自检；若主程序仓库可用，
另有真实调用校验（见 `test_manifest_passes_main_app_validator`）。
"""

import json
import os
import re
import sys

import pytest

from conftest import MAIN_APP_DIR, ROOT

MANIFEST_PATH = os.path.join(ROOT, 'manifest.json')

# 复刻自 core/plugins/manifest.py:37-57
KNOWN_HOOKS = frozenset({
    'app.ready', 'card.created', 'card.updated', 'card.deleted', 'card.moved',
    'tag.applied', 'tag.deleted', 'worldbook.saved', 'worldbook.entry_changed',
    'scan.completed', 'index.job_done',
})
KNOWN_PERMISSIONS = frozenset({
    'cards.read', 'cards.write', 'worldbook.read', 'worldbook.write',
    'tags.read', 'tags.write', 'secrets.read', 'network.outbound', 'jobs.run',
})

PLUGIN_ID_PATTERN = re.compile(r'^[a-z][a-z0-9-]{1,63}$')
SEMVER_PATTERN = re.compile(r'^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$')
JOB_TYPE_PATTERN = re.compile(r'^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$')

SCHEMA_TYPES = frozenset({'string', 'integer', 'number', 'boolean', 'enum'})
SCHEMA_ALLOWED_KEYS = frozenset({
    'type', 'title', 'description', 'default', 'minimum', 'maximum',
    'enum', 'secret', 'required', 'properties',
})


@pytest.fixture(scope='module')
def manifest():
    with open(MANIFEST_PATH, 'r', encoding='utf-8') as handle:
        return json.load(handle)


def test_manifest_id_and_version(manifest):
    assert manifest['id'] == 'stm-ai-assistant'
    assert PLUGIN_ID_PATTERN.match(manifest['id'])
    assert SEMVER_PATTERN.match(manifest['version'])


def test_api_version_and_min_app_version(manifest):
    assert manifest['api_version'] == 1
    assert manifest['min_app_version'] == '1.0.0'


def test_backend_entry_declares_package_with_init(manifest):
    entry = manifest['backend']['entry']
    assert re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', entry)
    assert os.path.isfile(os.path.join(ROOT, entry, '__init__.py'))


def test_declared_jobs_are_valid_and_registered(manifest):
    jobs = manifest['backend']['jobs']
    assert jobs, '至少要声明一个作业类型'
    for job_type in jobs:
        assert JOB_TYPE_PATTERN.match(job_type), job_type
    assert 'ai.analyze' in jobs


def test_hooks_are_within_whitelist(manifest):
    for hook in manifest['backend'].get('hooks') or []:
        assert hook in KNOWN_HOOKS, hook


def test_permissions_are_within_whitelist(manifest):
    permissions = manifest.get('permissions') or []
    assert permissions, '权限声明不能为空（如实声明是审计的前提）'
    for permission in permissions:
        assert permission in KNOWN_PERMISSIONS, permission
    assert 'secrets.read' in permissions
    assert 'network.outbound' in permissions


def test_frontend_entry_and_styles_exist(manifest):
    frontend = manifest['frontend']
    assert os.path.isfile(os.path.join(ROOT, frontend['entry']))
    for style in frontend['styles']:
        assert os.path.isfile(os.path.join(ROOT, style))


def _assert_schema_subset(schema, *, where: str):
    if not schema:
        return
    assert schema.get('type') == 'object', f'{where} 顶层必须是 object'

    properties = schema.get('properties') or {}
    assert isinstance(properties, dict)

    for name, spec in properties.items():
        assert isinstance(spec, dict), f'{where}.{name} 必须是对象'
        assert spec.get('type') in SCHEMA_TYPES, f'{where}.{name} 的 type 不受支持'
        for key in spec:
            assert key in SCHEMA_ALLOWED_KEYS, f'{where}.{name} 含未支持属性: {key}'
        if spec.get('type') == 'enum':
            assert isinstance(spec.get('enum'), list) and spec['enum'], f'{where}.{name} 缺少 enum'

    for key in schema:
        assert key in SCHEMA_ALLOWED_KEYS, f'{where} 含未支持属性: {key}'


def test_config_schema_uses_supported_subset(manifest):
    _assert_schema_subset(manifest.get('config_schema'), where='config_schema')


def test_secret_schema_uses_supported_subset(manifest):
    _assert_schema_subset(manifest.get('secret_schema'), where='secret_schema')


def test_backend_modules_avoid_forbidden_import(manifest):
    """硬约束：插件内不得 `import backend`（只能用相对导入）。"""
    pattern = re.compile(r'^\s*(?:import|from)\s+backend\b', re.MULTILINE)
    offenders = []
    for dirpath, _dirnames, filenames in os.walk(os.path.join(ROOT, 'backend')):
        for filename in filenames:
            if not filename.endswith('.py'):
                continue
            path = os.path.join(dirpath, filename)
            with open(path, 'r', encoding='utf-8') as handle:
                if pattern.search(handle.read()):
                    offenders.append(os.path.relpath(path, ROOT))
    assert offenders == []


def test_core_layer_stays_free_of_flask_and_main_app(manifest):
    """纯函数层不得依赖 flask 或主程序模块（设计文档 §3）。"""
    pattern = re.compile(
        r'^\s*(?:import|from)\s+(flask|core|requests)\b', re.MULTILINE)
    offenders = []
    for filename in os.listdir(os.path.join(ROOT, 'backend', 'core')):
        if not filename.endswith('.py'):
            continue
        path = os.path.join(ROOT, 'backend', 'core', filename)
        with open(path, 'r', encoding='utf-8') as handle:
            if pattern.search(handle.read()):
                offenders.append(filename)
    assert offenders == []


def test_only_summary_bridge_imports_main_app_modules(manifest):
    """决策 Q5：只有 summary_bridge 允许导入主程序 `core.*`。"""
    pattern = re.compile(r'^\s*(?:import|from)\s+core\b', re.MULTILINE)
    offenders = []
    for dirpath, _dirnames, filenames in os.walk(os.path.join(ROOT, 'backend')):
        for filename in filenames:
            if not filename.endswith('.py'):
                continue
            path = os.path.join(dirpath, filename)
            with open(path, 'r', encoding='utf-8') as handle:
                if pattern.search(handle.read()):
                    offenders.append(os.path.relpath(path, ROOT).replace('\\', '/'))
    assert offenders == ['backend/summary_bridge.py']


@pytest.mark.skipif(not os.path.isdir(MAIN_APP_DIR), reason='主程序仓库不可用')
def test_manifest_passes_main_app_validator():
    """用主程序的真实校验器加载本插件 manifest（最强契约验证）。"""
    sys.path.insert(0, MAIN_APP_DIR)
    try:
        from core.plugins.manifest import PluginManifest  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover
        pytest.skip(f'无法导入主程序 manifest 校验器: {exc}')
    finally:
        sys.path.remove(MAIN_APP_DIR)

    parsed = PluginManifest.load(ROOT, check_dir_name=False)
    assert parsed.plugin_id == 'stm-ai-assistant'
    assert parsed.api_version == 1
    assert parsed.backend_jobs == ['ai.analyze']
    assert parsed.has_backend and parsed.has_frontend
    assert parsed.warnings == [], parsed.warnings
