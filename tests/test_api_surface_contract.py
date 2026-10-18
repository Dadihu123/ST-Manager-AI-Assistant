"""插件 API 形态契约：后端只能使用域命名空间，不得回退到已删除的扁平名。

主程序在「插件 API 全面放开」改造里**不保留扁平别名**（插件系统未发布），
旧名已在 `core/plugins/api.py` 中物理删除。一旦本插件某处又写了旧名，
运行时会直接 `AttributeError`，而单元测试用的是 `tests/fake_api.py` 替身，
**不会发现**——所以需要一条静态扫描把这类回退挡在 CI 里。

同时锁定 `api.db()` 与 `api.suppress_fs_events()` 这两个**机制泄漏**成员
不得被重新引入（它们已被 `api.private.query` / 域内机制取代）。
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 已从主程序 PluginAPI 删除的扁平名 → 现在应使用的域路径
REMOVED_FLAT_NAMES = {
    'get_config': 'api.config.get()',
    'set_config': 'api.config.set()',
    'get_secret': 'api.secrets.get()',
    'set_secret': 'api.secrets.set()',
    'list_secrets': 'api.secrets.list()',
    'notify': 'api.log.info() / api.log.warn()',
    'log': 'api.log.info() / api.log.warn() / api.log.error()',
    'route': 'api.http.route()',
    'blueprint': 'api.http.blueprint()',
    'register_job': 'api.jobs.register()',
    'submit_job': 'api.jobs.submit()',
    'get_job': 'api.jobs.get()',
    'cancel_job': 'api.jobs.cancel()',
    'retry_job': 'api.jobs.retry()',
    'pause_job': 'api.jobs.pause()',
    'resume_job': 'api.jobs.resume()',
    'register_cli': 'api.cli.register()',
    'data_dir': 'api.private.dir()',
    'db': 'api.private.query() / api.jobs.items() / api.cards.list()',
    'migrate': 'api.private.migrate()',
    'table_prefix': 'api.private.table_prefix()',
    'ui_data_read': 'api.private.read()',
    'ui_data_update': 'api.private.update()',
    'read_card': 'api.cards.read()',
    'read_cards': 'api.cards.read_many()',
    'write_card_fields': 'api.cards.write_fields()',
    'apply_tags': 'api.cards.apply_tags()',
    'read_tag_library': 'api.tags.library()',
    'read_worldbook': 'api.worldbook.read()',
    'write_worldbook': 'api.worldbook.write()',
    'snapshot_card': 'api.backups.snapshot_card()',
    'suppress_fs_events': '（已内部化，插件无需也不可调用）',
}

# 只匹配 `api.<name>(`，避免误伤 `self._api.*`、注释里的历史说明与
# 域路径（如 `api.cards.read(`）——后者在 `api.` 与 `(` 之间还有一段。
FLAT_CALL_PATTERN = re.compile(
    r'\bapi\.(' + '|'.join(sorted(REMOVED_FLAT_NAMES, key=len, reverse=True)) + r')\s*\('
)


def _iter_backend_files():
    for dirpath, _dirnames, filenames in os.walk(os.path.join(ROOT, 'backend')):
        for filename in filenames:
            if filename.endswith('.py'):
                yield os.path.join(dirpath, filename)


def _strip_docstrings_and_comments(source: str) -> str:
    """剔除 docstring 与行注释，只留下真正的代码行。

    迁移过程中文档会提到旧名（「曾经的 `api.route()` 现在叫 `api.http.route()`」），
    那不是调用，不该被判为违规。
    """
    # 三引号 docstring（含单行形式）
    without_docstrings = re.sub(r'"""[\s\S]*?"""', '', source)
    without_docstrings = re.sub(r"'''[\s\S]*?'''", '', without_docstrings)
    lines = []
    for line in without_docstrings.splitlines():
        stripped = line.lstrip()
        if stripped.startswith('#'):
            lines.append('')
            continue
        lines.append(line)
    return '\n'.join(lines)


def _collect_offenders():
    offenders = []
    for path in _iter_backend_files():
        with open(path, 'r', encoding='utf-8') as handle:
            source = handle.read()
        code = _strip_docstrings_and_comments(source)
        for line_number, line in enumerate(code.splitlines(), start=1):
            for match in FLAT_CALL_PATTERN.finditer(line):
                offenders.append((
                    os.path.relpath(path, ROOT).replace('\\', '/'),
                    line_number,
                    match.group(1),
                ))
    return offenders


def test_backend_uses_only_domain_namespaced_api():
    """后端不得调用任何已删除的扁平 API。"""
    offenders = _collect_offenders()
    detail = '；'.join(
        f'{path}:{line} 仍在用 api.{name}() → 改用 {REMOVED_FLAT_NAMES[name]}'
        for path, line, name in offenders
    )
    assert offenders == [], detail


def test_no_bare_db_connection_is_used():
    """`api.db()` 已被删除：裸连接是机制泄漏。"""
    offenders = [
        (path, line, name) for path, line, name in _collect_offenders() if name == 'db'
    ]
    assert offenders == [], (
        '插件不得再使用 api.db()：读作业条目用 api.jobs.items()，'
        f'查卡片用 api.cards.list()；违规点: {offenders}'
    )


def test_no_fs_event_suppression_is_used():
    """`api.suppress_fs_events()` 已内部化：域方法会自己处理。"""
    offenders = [
        (path, line, name)
        for path, line, name in _collect_offenders()
        if name == 'suppress_fs_events'
    ]
    assert offenders == [], (
        '插件不得调用 api.suppress_fs_events()：写卡片/世界书走域方法时由域内部抑制；'
        f'违规点: {offenders}'
    )


def test_summary_bridge_no_longer_imports_main_app_modules():
    """概览读写已改走 `api.cards.read_note` / `write_note`，bridge 不得再越界。"""
    path = os.path.join(ROOT, 'backend', 'summary_bridge.py')
    with open(path, 'r', encoding='utf-8') as handle:
        source = handle.read()

    assert not re.search(r'^\s*(?:import|from)\s+core\b', source, re.MULTILINE), (
        'summary_bridge 仍导入主程序内部模块；应改用 api.cards.read_note / write_note'
    )


def test_manifest_secret_schema_only_has_fallback_api_key():
    """密钥入口已收敛：`secret_schema` 只留回退用的 `api_key`。

    `base_url` / `model` 曾经放在这里，但只有「首次运行生成 providers.json」
    时被读取一次，之后就是**死输入**（改了不起作用，UI 还显示已设置）。
    地址与模型现在只在插件的 Provider 页维护。
    """
    import json

    with open(os.path.join(ROOT, 'manifest.json'), 'r', encoding='utf-8') as handle:
        manifest = json.load(handle)

    properties = (manifest.get('secret_schema') or {}).get('properties') or {}
    assert set(properties) == {'api_key'}, (
        f'secret_schema 应只剩 api_key（回退密钥），实际: {sorted(properties)}'
    )
