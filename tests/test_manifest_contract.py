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

# 复刻自 core/plugins/manifest.py
KNOWN_HOOKS = frozenset({
    'app.ready', 'card.created', 'card.updated', 'card.deleted', 'card.moved',
    'tag.applied', 'tag.deleted', 'worldbook.saved', 'worldbook.entry_changed',
    'scan.completed', 'index.job_done',
})
KNOWN_PERMISSIONS = frozenset({
    'cards.read', 'cards.write', 'worldbook.read', 'worldbook.write',
    'tags.read', 'tags.write', 'secrets.read', 'network.outbound', 'jobs.run',
})
KNOWN_TOP_LEVEL_KEYS = frozenset({
    'id', 'name', 'version', 'api_version', 'manifest_version', 'author',
    'description', 'homepage', 'license', 'icon', 'min_app_version',
    'backend', 'frontend', 'permissions', 'requires', 'config_schema',
    'secret_schema', 'i18n',
})
KNOWN_BACKEND_KEYS = frozenset({'entry', 'register', 'jobs', 'hooks'})
KNOWN_FRONTEND_KEYS = frozenset({'entry', 'styles', 'entries'})
LEGACY_FRONTEND_KEYS = ('panels', 'settings_tab', 'detail_tab')

# 复刻自 core/plugins/entries.py
KNOWN_ENTRY_KEYS = frozenset({
    'id', 'kind', 'label', 'description', 'icon', 'contexts',
    'requires_selection', 'surface', 'priority', 'badge', 'shortcut',
    'when', 'disabledHint',
    # 菜单分组：同一插件的 context/batch 入口折叠成一个父项（如「AI 助手」）
    'group',
})
KNOWN_WHEN_KEYS = frozenset({
    'mode', 'selection', 'indexState', 'config', 'hostVersion', 'feature', 'target',
})
ENTRY_KINDS = frozenset({
    'dock', 'nav', 'settings', 'context', 'command', 'status', 'batch',
})
SURFACE_TYPES = frozenset({'panel', 'modal', 'page', 'sheet'})
SELECTION_MODES = frozenset({'none', 'any', 'single', 'multi'})
TARGET_KINDS = frozenset({'folder', 'card', 'any'})
KNOWN_MODES = frozenset({
    'cards', 'worldinfo', 'chats', 'presets',
    'regex', 'scripts', 'quick_replies', 'beautify',
})
MAX_ENTRIES_PER_PLUGIN = 16
ENTRY_ID_PATTERN = re.compile(r'^[a-z][a-z0-9-]{0,63}$')

# 归属分级（D5）：由主程序仓库的 `static/plugins/catalog.json` 决定，manifest 无权声明。
# 本插件已登记为 official（`catalog.json` 里 `tier: "official"`）。
# official 可用全部 7 类 kind；若将来降级为 community，则 nav / status 不可用。
OFFICIAL_ENTRY_KINDS = frozenset({
    'dock', 'nav', 'settings', 'context', 'command', 'status', 'batch',
})
COMMUNITY_ENTRY_KINDS = frozenset({'dock', 'command', 'context', 'settings', 'batch'})
# 本插件实际使用的 kind（保守集合：即使被降级为 community 也仍然合法）
USED_ENTRY_KINDS = COMMUNITY_ENTRY_KINDS

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


@pytest.fixture(scope='module')
def entries(manifest):
    return manifest['frontend'].get('entries') or []


@pytest.fixture(scope='module')
def tasks_source():
    """`frontend/tasks.js` 源码：任务注册表是入口文案与主按钮的单一来源。"""
    with open(os.path.join(ROOT, 'frontend', 'tasks.js'), 'r', encoding='utf-8') as handle:
        return handle.read()


def test_manifest_id_and_version(manifest):
    assert manifest['id'] == 'stm-ai-assistant'
    assert PLUGIN_ID_PATTERN.match(manifest['id'])
    assert SEMVER_PATTERN.match(manifest['version'])


def test_api_version_and_min_app_version(manifest):
    """D2：`api_version` 是宿主契约版本，`manifest_version` 是清单结构版本。"""
    assert manifest['api_version'] == 2
    assert manifest['manifest_version'] == 2
    assert manifest['min_app_version'] == '1.0.0'


def test_manifest_has_no_unknown_structural_keys(manifest):
    """D3：结构键严格校验——多一个键主程序会**拒绝加载**整个插件。"""
    assert set(manifest) <= KNOWN_TOP_LEVEL_KEYS, sorted(set(manifest) - KNOWN_TOP_LEVEL_KEYS)
    assert set(manifest.get('backend') or {}) <= KNOWN_BACKEND_KEYS
    assert set(manifest.get('frontend') or {}) <= KNOWN_FRONTEND_KEYS


def test_frontend_drops_legacy_keys(manifest):
    """D1：旧三字段已彻底删除且不保留别名，出现即拒绝加载。"""
    frontend = manifest['frontend']
    assert [key for key in LEGACY_FRONTEND_KEYS if key in frontend] == []


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


# --------------------------------------------------------------- entries 结构


def test_entries_declared(entries):
    """重写后入口由 manifest 声明（旧版是自绘浮动按钮）。"""
    assert entries, 'frontend.entries 不能为空'
    assert len(entries) <= MAX_ENTRIES_PER_PLUGIN


def test_entry_keys_are_within_whitelist(entries):
    """D3：`entries[]` 子键也是严格白名单，多一个键会拒绝加载整个插件。"""
    for index, entry in enumerate(entries):
        unknown = sorted(set(entry) - KNOWN_ENTRY_KEYS)
        assert unknown == [], f'entries[{index}] 含未知键: {unknown}'


def test_entry_ids_match_pattern_and_are_unique(entries):
    ids = [entry.get('id') for entry in entries]
    for entry_id in ids:
        assert ENTRY_ID_PATTERN.match(str(entry_id or '')), entry_id
    assert len(ids) == len(set(ids)), '入口 id 不得重复'


def test_entry_kinds_are_valid_and_within_community_subset(entries):
    """D5：kind 必须合法，且只用 community 也允许的那 5 类。

    本插件已登记为 official（可用全部 7 类），但实际只用了
    `dock` / `context` / `batch` / `settings`——这是**刻意**的保守选择：
    即使将来从 catalog 摘掉（降级为 community），入口也不会被静默忽略。
    """
    for entry in entries:
        kind = entry.get('kind')
        assert kind in ENTRY_KINDS, kind
        assert kind in USED_ENTRY_KINDS, (
            f'入口 {entry.get("id")} 使用了 {kind}；'
            f'本插件只应使用 {sorted(USED_ENTRY_KINDS)}（community 子集）'
        )


def test_entries_land_on_distinct_host_locations(entries):
    """入口必须覆盖宿主真实存在的落点，且每条入口都有自己的落点。

    重构后（任务化）不再「一个 kind 只准出现一次」——一个任务需要同时从右键、
    批量菜单进入。真正要守的是：
    - 每个 kind 都在宿主的可渲染集合里（不会被静默丢弃）；
    - 同 kind 的入口靠 `surface` 区分，指向不同任务面板；
    - 入口数不超过宿主上限（`MAX_ENTRIES_PER_PLUGIN = 16`）。
    """
    kinds = [entry.get('kind') for entry in entries]
    assert set(kinds) == {'dock', 'context', 'batch', 'command', 'settings'}, kinds
    assert len(entries) <= 16, f'入口数 {len(entries)} 超过宿主上限 16'

    # 同一 kind 下不应有两条入口在**同样的上下文**里打开同一个 surface
    seen = {}
    for entry in entries:
        kind = entry.get('kind')
        surface = (entry.get('surface') or {}).get('id')
        if not surface:
            continue
        when = entry.get('when') or {}
        pair = (kind, surface, when.get('mode') and tuple(when['mode']), when.get('target'))
        assert pair not in seen, (
            f'入口 {entry.get("id")} 与 {seen.get(pair)} 在同样上下文里都打开 {surface}，属于重复入口'
        )
        seen[pair] = entry.get('id')

    # 单插件坞槽位上限 2
    dock_entries = [entry for entry in entries if entry.get('kind') == 'dock']
    assert len(dock_entries) <= 2, f'坞入口 {len(dock_entries)} 超过单插件上限 2'


def test_every_task_has_at_least_one_entry(entries):
    """每个任务都必须至少有一个入口，否则「功能存在但进不去」。

    任务的唯一声明处是 `frontend/tasks.js`；这里解析它的 `surface:` 取值，
    再做双向核对：任务 → 入口、入口 → 已注册 surface。
    """
    with open(os.path.join(ROOT, 'frontend', 'tasks.js'), 'r', encoding='utf-8') as handle:
        source = handle.read()

    task_surfaces = re.findall(r"surface:\s*'([a-z-]+)'", source)
    assert task_surfaces, '未能从 tasks.js 解析出任务 surface'
    assert len(task_surfaces) == len(set(task_surfaces)), '两个任务指向了同一个 surface'

    entry_surfaces = {
        (entry.get('surface') or {}).get('id')
        for entry in entries
    }
    for surface in task_surfaces:
        assert any(
            (entry.get('surface') or {}).get('id') == surface or entry.get('id') == surface
            for entry in entries
        ), f'任务面板 {surface} 没有任何入口'

    # 反向：每个入口的 surface 必须在 panel.js 的 SURFACES 里注册过
    with open(os.path.join(ROOT, 'frontend', 'panel.js'), 'r', encoding='utf-8') as handle:
        panel_source = handle.read()
    registered = set(re.findall(r"^\s{2}([a-z-]+):\s*\{ title:", panel_source, re.M))
    assert registered, '未能从 panel.js 解析出 SURFACES'
    for entry in entries:
        surface = (entry.get('surface') or {}).get('id')
        if surface:
            assert surface in registered, f'入口 {entry.get("id")} 指向未注册的 surface: {surface}'


def test_entry_labels_use_user_vocabulary(tasks_source):
    """入口文案必须用用户词（打标 / 精简 / 归类 / 提议），而不是内部术语。

    这是 F2/F3 的回归护栏：三个功能此前完全没有入口，命令面板也搜不到。
    `label` 决定命令面板能否被搜到，`verb` 决定主按钮文案——
    两者必须说同一套词。
    """
    labels = tasks_source
    for word in ('打标', '精简', '归类', '提议'):
        assert word in labels, f'任务注册表里缺少用户词「{word}」'


def test_folder_context_entry_declares_folder_target(entries):
    """`analyze-folder` 必须声明 `when.target: "folder"`。

    没有它，这个入口会出现在**卡片**右键菜单里（那才是旧的 `analyze-selected`
    行为），而右键分类 → 分析整个分类这条路径就丢了。
    """
    by_id = {entry.get('id'): entry for entry in entries}
    folder_entry = by_id.get('analyze-folder')
    assert folder_entry is not None, '缺少 analyze-folder 入口'
    assert folder_entry['kind'] == 'context'
    assert (folder_entry.get('when') or {}).get('target') == 'folder'
    assert str(folder_entry.get('disabledHint') or '').strip()

    # 右键菜单专属：不得再声明 selection（右键分类的作用域与全局选中集无关）
    assert 'selection' not in (folder_entry.get('when') or {})


def test_selected_batch_entry_requires_selection_with_hint(entries):
    """`analyze-selected` 是批量菜单项：`kind: "batch"` + `when.selection: "any"`。"""
    by_id = {entry.get('id'): entry for entry in entries}
    entry = by_id.get('analyze-selected')
    assert entry is not None, '缺少 analyze-selected 入口'
    assert entry['kind'] == 'batch'
    assert (entry.get('when') or {}).get('selection') == 'any'
    assert str(entry.get('disabledHint') or '').strip()


def test_obsolete_single_surface_entries_are_gone(entries):
    """四个入口全部指向同一个 `workbench` surface 的旧结构必须消失。

    旧版 `analyze-selected` / `analyze-folder` / `assistant-settings` 都开同一个
    四页签面板，于是「入口很多、点开都一样」。现在每条入口都有自己的 surface。
    """
    by_id = {entry.get('id'): entry for entry in entries}
    workbench_targets = [
        entry for entry in entries
        if (entry.get('surface') or {}).get('id') == 'workbench'
    ]
    assert workbench_targets == [], '不应再有入口直接指向 workbench surface'
    assert by_id['analyze-selected']['surface']['id'] == 'analyze'
    assert by_id['assistant-settings']['surface']['id'] == 'settings'


def test_pending_badge_targets_the_workbench_entry():
    """待审批角标必须挂在仍然存在的入口上。

    `setBadge` 查不到条目会静默返回 false，角标就丢了。重构后角标同时写
    `workbench`（坞）与 `open-review`（命令面板），两处都要存在。
    """
    with open(os.path.join(ROOT, 'frontend', 'plugin.js'), 'r', encoding='utf-8') as handle:
        source = _strip_js_comments(handle.read())

    assert 'setBadge' in source, 'plugin.js 不再写入口角标'
    assert "'workbench'" in source, '角标必须覆盖 workbench 入口'
    assert 'open-review' in source, '角标必须覆盖 open-review 入口'


def test_workbench_surface_routes_to_the_hub(entries):
    """坞入口必须落在「任务中心」而不是某个具体任务面板。

    任务化之后坞是统一出发页；若它指向 `analyze`，用户点「AI 助手」会直接进
    打标面板，其余三个任务不可达。
    """
    by_id = {entry.get('id'): entry for entry in entries}
    dock = by_id['workbench']
    assert dock['kind'] == 'dock'
    assert dock['surface']['id'] == 'hub'
    assert dock['surface']['type'] == 'modal'
    assert str(dock.get('shortcut') or '').strip(), '坞入口必须保留快捷键'

    with open(os.path.join(ROOT, 'frontend', 'plugin.js'), 'r', encoding='utf-8') as handle:
        source = handle.read()
    assert "api.surfaces.register(surfaceId" in source, (
        'plugin.js 应按 SURFACES 表批量注册，而不是手写四条 register'
    )
    assert "'modal'" in source, 'surface 类型必须是 modal'


def test_entry_labels_present(entries):
    """缺 label 的入口会被宿主丢弃 + 警告。"""
    for entry in entries:
        assert str(entry.get('label') or '').strip(), entry.get('id')


def test_entry_surface_types_are_valid(entries):
    for entry in entries:
        surface = entry.get('surface')
        if surface is None:
            continue
        assert isinstance(surface, dict), entry.get('id')
        assert set(surface) <= {'id', 'type'}, sorted(surface)
        surface_type = surface.get('type')
        if surface_type is not None:
            assert surface_type in SURFACE_TYPES, surface_type


def test_entry_when_expression_is_valid(entries):
    """D7：`when` 只声明，求值归宿主。非法子键会被忽略 + 警告。"""
    for entry in entries:
        when = entry.get('when')
        if when is None:
            continue
        assert isinstance(when, dict), entry.get('id')
        assert set(when) <= KNOWN_WHEN_KEYS, sorted(set(when) - KNOWN_WHEN_KEYS)

        for mode in when.get('mode') or []:
            assert mode in KNOWN_MODES, mode

        if 'selection' in when:
            assert when['selection'] in SELECTION_MODES, when['selection']

        if 'target' in when:
            assert when['target'] in TARGET_KINDS, when['target']

        if 'config' in when:
            assert isinstance(when['config'], dict)
            assert set(when['config']) <= {'requireKeys'}, sorted(when['config'])


def test_entries_require_selection_have_disabled_hint(entries):
    """置灰必须给出理由（D7：不是无反应，而是「可见但置灰 + 提示」）。"""
    for entry in entries:
        when = entry.get('when') or {}
        needs_selection = bool(entry.get('requires_selection')) or 'selection' in when
        if needs_selection:
            assert str(entry.get('disabledHint') or '').strip(), (
                f'入口 {entry.get("id")} 依赖选中项，必须声明 disabledHint'
            )


def test_when_config_require_keys_exist_in_schemas(manifest):
    """D7：`when.config.requireKeys` 读的是 `config_schema` 与 `secret_schema` 的键。"""
    declared = set()
    for field in ('config_schema', 'secret_schema'):
        schema = manifest.get(field) or {}
        declared.update((schema.get('properties') or {}).keys())

    for entry in manifest['frontend'].get('entries') or []:
        when = entry.get('when') or {}
        for key in ((when.get('config') or {}).get('requireKeys') or []):
            assert key in declared, f'入口 {entry.get("id")} 引用了未声明的配置键: {key}'


def test_entry_shortcuts_are_wellformed_and_unique(entries):
    """`shortcut` 是「建议快捷键，宿主可覆盖」（设计文档 2.2）。

    格式必须与宿主 `commandPalette.shortcutCombo()` 的折算规则一致，否则
    用户按下去永远匹配不上：`Ctrl+Alt+Shift+Meta` 固定顺序 + 主键大写。
    """
    combo_pattern = re.compile(
        r'^(Ctrl\+)?(Alt\+)?(Shift\+)?(Meta\+)?'
        r'([A-Z0-9]|F\d{1,2}|Arrow(Up|Down|Left|Right)|Enter|Escape|Space|Tab|Backspace|Delete)$'
    )

    seen = {}
    for entry in entries:
        combo = str(entry.get('shortcut') or '').strip()
        if not combo:
            continue
        assert combo_pattern.match(combo), (
            f'入口 {entry.get("id")} 的快捷键格式不符合宿主折算规则: {combo!r}'
        )
        assert combo not in seen, (
            f'快捷键 {combo} 被多个入口声明: {seen[combo]} 与 {entry.get("id")}'
        )
        seen[combo] = entry.get('id')


def test_shortcuts_do_not_collide_with_host_reserved_combos(entries):
    """不要占用宿主已用的组合键（`Ctrl+K` = 命令面板）。"""
    reserved = {'Ctrl+K', 'Ctrl+Shift+K'}
    for entry in entries:
        combo = str(entry.get('shortcut') or '').strip()
        assert combo not in reserved, (
            f'入口 {entry.get("id")} 占用了宿主保留快捷键 {combo}'
        )


def test_surface_ids_are_consistent_across_entries(entries):
    """坑 2：坞按 `<pluginId>:<surface.id ?? entry.id>` 查表，两侧必须一致。

    这里只能验证声明层自洽（多个入口复用同一个 surface id 是允许的，但 id 必须合法）。
    """
    for entry in entries:
        surface = entry.get('surface') or {}
        surface_id = surface.get('id')
        if surface_id is None:
            continue
        assert ENTRY_ID_PATTERN.match(str(surface_id)), surface_id


# --------------------------------------------------------------- 图标名


@pytest.mark.skipif(not os.path.isdir(MAIN_APP_DIR), reason='主程序仓库不可用')
def test_entry_icons_are_existing_host_icons(entries):
    """D4：`entries[].icon` 用宿主图标名，不存在会降级为占位并记警告。

    直接问主程序的图标索引（与 `GET /api/plugins/ui-icons` 同一份数据）。
    """
    sys.path.insert(0, MAIN_APP_DIR)
    try:
        from core.plugins.icons import is_host_icon_name, sprite_for_icon  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover
        pytest.skip(f'无法导入主程序图标索引: {exc}')
    finally:
        sys.path.remove(MAIN_APP_DIR)

    for entry in entries:
        name = str(entry.get('icon') or '').strip()
        if not name or not is_host_icon_name(name):
            continue
        assert sprite_for_icon(name), (
            f'入口 {entry.get("id")} 引用了不存在的宿主图标: {name}'
        )


@pytest.mark.skipif(not os.path.isdir(MAIN_APP_DIR), reason='主程序仓库不可用')
def test_frontend_ui_icons_exist_in_host():
    """`frontend/plugin.js` 与视图里通过 `api.ui.icon()` 引用的名字同样必须存在。"""
    sys.path.insert(0, MAIN_APP_DIR)
    try:
        from core.plugins.icons import sprite_for_icon  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover
        pytest.skip(f'无法导入主程序图标索引: {exc}')
    finally:
        sys.path.remove(MAIN_APP_DIR)

    frontend_dir = os.path.join(ROOT, 'frontend')
    pattern = re.compile(r"""api\.ui\.icon\(\s*['"]([a-z0-9-]+)['"]""")
    used = set()
    for dirpath, _dirnames, filenames in os.walk(frontend_dir):
        for filename in filenames:
            if not filename.endswith('.js'):
                continue
            with open(os.path.join(dirpath, filename), 'r', encoding='utf-8') as handle:
                used.update(pattern.findall(handle.read()))

    missing = sorted(name for name in used if not sprite_for_icon(name))
    assert missing == [], f'引用了不存在的宿主图标: {missing}'


def test_panel_surface_icons_are_host_icons():
    """`panel.js` 的 SURFACES 图标名必须存在（它们是面板标题行的图标）。"""
    if not os.path.isdir(MAIN_APP_DIR):
        pytest.skip('主程序仓库不可用')

    sys.path.insert(0, MAIN_APP_DIR)
    try:
        from core.plugins.icons import sprite_for_icon  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover
        pytest.skip(f'无法导入主程序图标索引: {exc}')
    finally:
        sys.path.remove(MAIN_APP_DIR)

    with open(os.path.join(ROOT, 'frontend', 'panel.js'), 'r', encoding='utf-8') as handle:
        source = handle.read()

    names = re.findall(r"\{ title: '[^']+', icon: '([a-z0-9-]+)'", source)
    assert names, '未能从 panel.js 解析出 surface 图标名'
    missing = sorted(name for name in names if not sprite_for_icon(name))
    assert missing == [], f'surface 引用了不存在的宿主图标: {missing}'


# --------------------------------------------------------------- schema 子集


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


# --------------------------------------------------------------- 模块边界


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


def test_no_backend_module_imports_main_app_modules(manifest):
    """插件后端**零越界**：不再有任何模块导入主程序 `core.*`。

    历史：决策 Q5 曾允许 `summary_bridge.py` 导入 `core.data.ui_store` +
    `core.services.card_service` 来写「概览」，因为主程序当时没有对应 API。
    主程序的域命名空间已提供 `api.cards.read_note` / `write_note`，
    该越界点已消除——本测试从此断言**零容忍**，防止它再被引入。
    """
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
    assert offenders == [], f'插件后端不得导入主程序内部模块: {offenders}'


# --------------------------------------------------------------- 前端契约


def _strip_js_comments(source: str) -> str:
    """剔除 JS 注释，避免「注释里提到某个 API」被误判成「调用了它」。"""
    without_block = re.sub(r'/\*.*?\*/', '', source, flags=re.S)
    return re.sub(r'^\s*//.*$', '', without_block, flags=re.MULTILINE)


def _iter_frontend_js():
    """遍历 `frontend/` 下的全部 JS 文件，产出 `(相对路径, 去注释源码)`。"""
    frontend_dir = os.path.join(ROOT, 'frontend')
    for dirpath, _dirnames, filenames in os.walk(frontend_dir):
        for filename in sorted(filenames):
            if not filename.endswith('.js'):
                continue
            path = os.path.join(dirpath, filename)
            with open(path, 'r', encoding='utf-8') as handle:
                yield os.path.relpath(path, ROOT).replace('\\', '/'), _strip_js_comments(handle.read())


def test_frontend_avoids_removed_host_apis():
    """重写后插件不得再依赖宿主已删除 / 不该用的成员。"""
    forbidden = {
        'setFullscreenScrim': '宿主自己管遮罩合成（pluginSurfaceHost.js）',
        'registerModal': '已废弃，改用 entries + surfaces',
        'registerSettingsSection': '已废弃，改用 entries.register({kind: "settings"})',
        'createRoot': '入口/面板不该再用自绘容器',
    }
    offenders = []
    for relative, source in _iter_frontend_js():
        for name, reason in forbidden.items():
            if re.search(rf'\bapi\.{name}\b', source):
                offenders.append(f'{relative}: api.{name}（{reason}）')
    assert offenders == []


def test_frontend_has_no_global_keydown_or_overlay_teardown():
    """重写目标：插件不再自己承担层级 / 焦点 / ESC / 遮罩。

    `dom.js` 的自定义下拉（`styledSelect`）自己处理方向键与 ESC 是**控件级**
    键盘交互（宿主原语没有等价控件），因此只针对「面板/入口」这类宿主职责文件
    做全局 `document` 监听检查。
    """
    offenders = []
    for relative, source in _iter_frontend_js():
        if relative.endswith(('dom.js', 'panel.js')):
            # panel.js 是 surface 内容渲染器，不该绑全局键盘
            if relative.endswith('panel.js') and re.search(r"document\.addEventListener\(\s*'keydown'", source):
                offenders.append(f'{relative}: document keydown（ESC 归宿主）')
            continue
        if re.search(r"document\.addEventListener\(\s*'keydown'", source):
            offenders.append(f'{relative}: document keydown（ESC 归宿主）')
    assert offenders == []


def test_frontend_avoids_self_drawn_positioning_and_storage():
    """层级 / 定位 / 界面偏好都归宿主：不得出现 fixed、z-index、localStorage。"""
    patterns = {
        r'position\s*:\s*[\'"]?fixed': '层级与定位归宿主容器',
        r'z-index\s*:': 'z-index 归宿主',
        r'localStorage': '界面偏好改用 api.storage',
    }
    offenders = []
    for relative, source in _iter_frontend_js():
        for pattern, reason in patterns.items():
            if re.search(pattern, source):
                offenders.append(f'{relative}: {pattern}（{reason}）')
    assert offenders == []


def test_frontend_uses_noun_namespaced_api():
    """D8：新代码用 `api.host.*` / `api.ui.*`，而不是裸动词别名。"""
    offenders = []
    pattern = re.compile(r'\bapi\.(toast|refresh|switchMode|openDetail|openSettingsSection)\s*\(')
    for relative, source in _iter_frontend_js():
        for number, line in enumerate(source.splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f'{relative}:{number}')
    assert offenders == [], f'仍在使用裸动词别名: {offenders}'


def test_entry_status_writes_are_replayed_after_registry_ready():
    """回归：`activate()` 早于 `EntryRegistry.setPlugins()`，入口写入必须可重放。

    `PluginHost.loadAll()` 先 `loadPlugin()`（= 调用 `activate`），**之后**才
    `EntryRegistry.setPlugins(items)` 建表（`pluginHost.js:183-194`）。
    因此 `activate()` 期间 `api.entries.setStatus()` 会静默失败
    （`entryRegistry.js:566-573` 查不到条目直接 return），自检结论会丢失。

    这里锁定 `plugin.js` 的重放机制：写入必须**只**发生在写入器内部
    （它订阅 `entries:changed` 重放），其余代码走 `reporter.*`。
    """
    with open(os.path.join(ROOT, 'frontend', 'plugin.js'), 'r', encoding='utf-8') as handle:
        raw = handle.read()
    # 注释里会**提到**这些 API 名（说明为什么不能直接调），因此只看代码
    source = _strip_js_comments(raw)

    assert "api.on('entries:changed'" in source, (
        'plugin.js 必须订阅 entries:changed 以重放 activate() 期间的入口写入'
    )
    assert 'createEntryStatusReporter' in source

    # 写入器的定义体之外，不得再直接调 api.entries.setStatus / setBadge
    start = source.index('function createEntryStatusReporter')
    end = source.index('export async function activate')
    reporter_body = source[start:end]
    outside = source[:start] + source[end:]

    assert 'api.entries.setStatus(' in reporter_body, '写入器必须真的写入口状态'
    assert 'api.entries.setStatus(' not in outside, (
        '自检结论必须走 reporter.setStatus（直接写会因注册表未就绪而丢失）'
    )
    assert 'api.entries.setBadge(' not in outside, (
        '角标必须走 reporter.setPendingBadge'
    )
    assert 'reporter.setStatus(' in outside, 'activate() 里应通过 reporter 写状态'


def test_pending_badge_goes_through_injected_writer():
    """待审批角标必须走注入的写入器（带「注册表就绪重放」）。

    宿主先 `activate()` 再建入口表，激活期间直接 `api.entries.setBadge()` 会静默
    失败。因此 `plugin.js` 里的回报器要记住期望值、在 `entries:changed` 时重放。
    """
    with open(os.path.join(ROOT, 'frontend', 'plugin.js'), 'r', encoding='utf-8') as handle:
        source = _strip_js_comments(handle.read())

    assert 'setPendingBadge' in source, 'plugin.js 应暴露待审批角标的写入器'
    assert 'entries:changed' in source, '必须订阅 entries:changed 做重放'
    assert 'api.entries.setBadge(' in source, '最终仍要写到宿主入口角标上'


# --------------------------------------------------------------- 真实校验器


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
    assert parsed.api_version == 2
    assert parsed.manifest_version == 2
    assert parsed.backend_jobs == [
        'ai.analyze', 'ai.wi_rewrite', 'ai.classify', 'ai.propose', 'ai.agent',
    ]
    assert parsed.has_backend and parsed.has_frontend
    assert len(parsed.frontend_entries) == 12
    # 「新契约零警告」是最强断言：图标名、entries 子键、权限、hook 全部干净
    assert parsed.warnings == [], parsed.warnings


@pytest.mark.skipif(not os.path.isdir(MAIN_APP_DIR), reason='主程序仓库不可用')
def test_entries_pass_tier_filter_for_both_tiers():
    """D5：两种 tier 下 4 个入口都必须全部保留。

    - `official`：本插件在 catalog 里的实际分级（坞默认进槽位）。
    - `community`：降级后的兜底。本插件刻意只用 community 子集的 kind，
      所以即使被从 catalog 摘掉，入口也不会被静默忽略。
    """
    sys.path.insert(0, MAIN_APP_DIR)
    try:
        from core.plugins.manifest import PluginManifest  # noqa: PLC0415
        from core.plugins.entries import apply_tier  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover
        pytest.skip(f'无法导入主程序 entries 模块: {exc}')
    finally:
        sys.path.remove(MAIN_APP_DIR)

    parsed = PluginManifest.load(ROOT, check_dir_name=False)
    expected = {
        'workbench',
        'analyze-folder', 'analyze-selected',
        'classify-folder', 'classify-selected',
        'polish-embedded', 'polish-selector', 'polish-folder',
        'open-review', 'propose-library', 'open-profiles',
        'assistant-settings',
    }

    for tier in ('official', 'community'):
        kept, warnings = apply_tier(parsed.plugin_id, parsed.frontend_entries, tier=tier)
        assert warnings == [], f'{tier}: {warnings}'
        assert len(kept) == len(parsed.frontend_entries), tier
        assert {item['id'] for item in kept} == expected, tier
        assert all(item['tier'] == tier for item in kept), tier


@pytest.mark.skipif(not os.path.isdir(MAIN_APP_DIR), reason='主程序仓库不可用')
def test_when_target_survives_the_host_normalizer():
    """`when.target` 必须被宿主的规范化器**保留**下来（不是当作未知子键丢掉）。

    这条是跨仓库契约回归：若主程序侧漏掉 `TARGET_KINDS` 校验分支，
    `when.target` 会被静默丢弃 → 右键菜单里 context 入口对任何目标都可用，
    「右键分类才可用」的约束形同虚设。
    """
    sys.path.insert(0, MAIN_APP_DIR)
    try:
        from core.plugins.entries import normalize_entries  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover
        pytest.skip(f'无法导入主程序 entries 模块: {exc}')
    finally:
        sys.path.remove(MAIN_APP_DIR)

    entries, warnings = normalize_entries('demo-plugin', [
        {'id': 'a', 'kind': 'context', 'label': 'x', 'when': {'target': 'folder'}},
    ])
    assert warnings == []
    assert entries[0]['when'] == {'target': 'folder'}

    # 非法值被拒（忽略 + 警告），而不是原样透传
    bad, bad_warnings = normalize_entries('demo-plugin', [
        {'id': 'b', 'kind': 'context', 'label': 'y', 'when': {'target': 'category'}},
    ])
    assert bad[0]['when'] == {}
    assert any('target' in item for item in bad_warnings)


@pytest.mark.skipif(not os.path.isdir(MAIN_APP_DIR), reason='主程序仓库不可用')
def test_plugin_is_registered_as_official_in_main_app_catalog():
    """本插件必须在主程序包内 catalog 里登记为 official。

    `tier` 只能由**宿主仓库**的清单决定（manifest 无权声明），所以这条断言
    是「官方插件」这一事实在插件仓库侧的回归保护：若主程序侧被误删/误改，
    这里会立刻失败。
    """
    catalog_path = os.path.join(MAIN_APP_DIR, 'static', 'plugins', 'catalog.json')
    if not os.path.isfile(catalog_path):
        pytest.skip('主程序 catalog.json 不存在')

    with open(catalog_path, 'r', encoding='utf-8') as handle:
        payload = json.load(handle)

    entries = {item.get('id'): item for item in (payload.get('plugins') or [])}
    assert 'stm-ai-assistant' in entries, '主程序 catalog 必须登记本插件'
    assert entries['stm-ai-assistant'].get('tier') == 'official'


@pytest.mark.skipif(not os.path.isdir(MAIN_APP_DIR), reason='主程序仓库不可用')
def test_surface_ids_match_frontend_registrations():
    """坑 2：manifest 的 `surface.id` 必须在插件侧真实注册过。

    坞与命令面板都按 `<pluginId>:<surface.id ?? entry.id>` 查表；写错会导致
    「点击无反应且不报错」——所以这里做静态对齐检查。

    重构后 surface 由 `panel.js` 的 `SURFACES` 表批量注册（`plugin.js` 遍历它），
    因此核对对象是那张表 + `prompt-preview` 这条手工注册的次级 surface。
    """
    with open(os.path.join(ROOT, 'frontend', 'plugin.js'), 'r', encoding='utf-8') as handle:
        source = handle.read()
    with open(os.path.join(ROOT, 'frontend', 'panel.js'), 'r', encoding='utf-8') as handle:
        panel_source = handle.read()

    registered = set(re.findall(r"^\s{2}([a-z0-9-]+):\s*\{ title:", panel_source, re.M))
    registered.update(re.findall(r"api\.surfaces\.register\(\s*'([a-z0-9-]+)'", source))
    assert registered, '未能解析出已注册的 surface'
    assert 'surfaceIds' in source, 'plugin.js 应遍历 SURFACES 批量注册'

    with open(MANIFEST_PATH, 'r', encoding='utf-8') as handle:
        manifest = json.load(handle)

    for entry in manifest['frontend'].get('entries') or []:
        surface = entry.get('surface') or {}
        surface_id = surface.get('id') or entry.get('id')
        assert surface_id in registered, (
            f'入口 {entry.get("id")} 的 surface "{surface_id}" 未注册'
        )
