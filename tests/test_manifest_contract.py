"""manifest 契约（v3）：与主程序 `core/plugins/contract.py` 对齐。

v3 删除了插件仓库里的宿主逻辑复刻（设计文档附录 B：manifest 白名单、
`evaluateWhen`、CSS 扫描器三份复刻）。契约的唯一事实源是主程序
`core/plugins/contract.py`，插件仓库的离线自检入口是
`scripts/stm_plugin_check.py`（由主程序工具链生成、与 contract.py 同源）。

因此本文件只保留三类断言：

1. **插件自己的语义约束**：哪些落点 / 命令 / 图标 / 文案必须有、坞上限、
   快捷键不冲突、任务面板必须可达；
2. **真实校验器交叉验证**：主程序仓库可用时调用
   `contract.validate_manifest(..., strict=True)`，与插件仓库 CI 使用同一套规则；
3. 不依赖契约白名单的模块边界、schema 子集、图标存在性检查。
"""

import json
import os
import re
import sys

import pytest

from conftest import MAIN_APP_DIR, ROOT

MANIFEST_PATH = os.path.join(ROOT, 'manifest.json')
HOST_AVAILABLE = os.path.isdir(MAIN_APP_DIR)

#: v1 落点清单（与主程序 `contract.MENU_PLACEMENTS` 一致；仅用于语义断言）。
MENU_PLACEMENTS = frozenset({
    'context.folder', 'context.card', 'context.footer', 'batch.actions',
    'commandPalette', 'dock', 'settings.nav',
})
#: 命名插槽清单（与主程序 `contract.SLOT_NAMES` 一致）。
SLOT_NAMES = frozenset({
    'settings.section', 'header.actions', 'card.detail.tabs',
    'page.cards.toolbar', 'page.worldinfo.toolbar',
})
#: 菜单节点 / 命令 id 正则（与主程序一致）。
NODE_ID_PATTERN = re.compile(r'^[a-z][a-z0-9-]{0,63}$')
#: 宿主工作模式（`when.mode` 的取值；与前端 `KNOWN_MODES` 一致）。
KNOWN_MODES = frozenset({
    'cards', 'worldinfo', 'chats', 'presets',
    'regex', 'scripts', 'quick_replies', 'beautify',
})
#: `when.selection` 取值（与主程序一致）。
SELECTION_MODES = frozenset({'none', 'any', 'single', 'multi'})
#: v3 已删除的 `when` 子键：落点由 placement 表达，不再需要 target。
REMOVED_WHEN_KEYS = ('target', 'contexts')
#: 快捷键格式（与宿主 `commandPalette.shortcutCombo()` 的折算规则一致）。
SHORTCUT_PATTERN = re.compile(
    r'^(Ctrl\+)?(Alt\+)?(Shift\+)?(Meta\+)?'
    r'([A-Z0-9]|F\d{1,2}|Arrow(Up|Down|Left|Right)|Enter|Escape|Space|Tab|Backspace|Delete)$'
)

SCHEMA_TYPES = frozenset({'string', 'integer', 'number', 'boolean', 'enum'})
SCHEMA_ALLOWED_KEYS = frozenset({
    'type', 'title', 'description', 'default', 'minimum', 'maximum',
    'enum', 'secret', 'required', 'properties',
})

#: 本插件升级到 v3 后的能力披露（v2 `permissions` 的同名迁移）。
EXPECTED_CAPABILITIES = frozenset({
    'cards.read', 'cards.write', 'worldbook.read', 'worldbook.write',
    'tags.read', 'tags.write', 'secrets.read', 'network.outbound', 'jobs.run',
})


# --------------------------------------------------------------- fixtures


@pytest.fixture(scope='module')
def manifest():
    with open(MANIFEST_PATH, 'r', encoding='utf-8') as handle:
        return json.load(handle)


@pytest.fixture(scope='module')
def frontend(manifest):
    return manifest['frontend']


@pytest.fixture(scope='module')
def contributions(frontend):
    return frontend.get('contributions') or {}


@pytest.fixture(scope='module')
def menus(contributions):
    return contributions.get('menus') or []


@pytest.fixture(scope='module')
def slots(contributions):
    return contributions.get('slots') or []


@pytest.fixture(scope='module')
def commands(contributions):
    return contributions.get('commands') or []


@pytest.fixture(scope='module')
def nodes(menus):
    """扁平化菜单树：`[(depth, placement, node), …]`（placement 继承根节点）。"""
    out = []

    def walk(items, depth=0, placement=''):
        for node in items or []:
            current = placement or str(node.get('placement') or '')
            out.append((depth, current, node))
            walk(node.get('children') or [], depth + 1, current)

    walk(menus)
    return out


@pytest.fixture(scope='module')
def nodes_by_id(menus):
    return {node.get('id'): node for _depth, _placement, node in _flatten(menus)}


def _flatten(menus, depth=0, placement=''):
    out = []
    for node in menus or []:
        current = placement or str(node.get('placement') or '')
        out.append((depth, current, node))
        out.extend(_flatten(node.get('children') or [], depth + 1, current))
    return out


@pytest.fixture(scope='module')
def plugin_source():
    with open(os.path.join(ROOT, 'frontend', 'plugin.js'), 'r', encoding='utf-8') as handle:
        return handle.read()


@pytest.fixture(scope='module')
def panel_source():
    with open(os.path.join(ROOT, 'frontend', 'panel.js'), 'r', encoding='utf-8') as handle:
        return handle.read()


@pytest.fixture(scope='module')
def tasks_source():
    with open(os.path.join(ROOT, 'frontend', 'tasks.js'), 'r', encoding='utf-8') as handle:
        return handle.read()


def _load_host_contract():
    """导入主程序 v3 契约模块；主程序未迁移时跳过（而不是复刻一份规则）。"""
    if not HOST_AVAILABLE:
        pytest.skip('主程序仓库不可用')
    sys.path.insert(0, MAIN_APP_DIR)
    try:
        from core.plugins import contract  # noqa: PLC0415

        return contract
    except Exception as exc:  # pragma: no cover
        pytest.skip(f'主程序尚未提供 v3 contract 模块: {exc}')
    finally:
        sys.path.remove(MAIN_APP_DIR)


# --------------------------------------------------------------- 版本与结构


def test_manifest_id_and_version(manifest):
    assert manifest['id'] == 'stm-ai-assistant'
    assert re.match(r'^[a-z][a-z0-9-]{1,63}$', manifest['id'])
    assert re.match(r'^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$',
                    manifest['version'])


def test_version_matches_docs(manifest):
    """`manifest.version` 必须与 README / CHANGELOG / FORK.md 声称的版本一致。

    README 写「本副本是版本 0.4.0」、manifest 却是 0.3.0 时，用户和宿主都无从判断
    装的是哪一版；发布流程也依赖这个数字。
    """
    version = manifest['version']
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    readme = open(os.path.join(root, 'README.md'), encoding='utf-8').read()
    assert f'版本 {version}' in readme, f'README 应声明版本 {version}'

    fork = os.path.join(root, 'FORK.md')
    assert os.path.isfile(fork), 'CHANGELOG 声明新增 FORK.md（Apache-2.0 §4(b) 要求），文件必须存在'
    fork_text = open(fork, encoding='utf-8').read()
    assert version in fork_text, f'FORK.md 应声明本副本版本 {version}'


def test_api_version_and_min_app_version(manifest):
    """v3：`api_version` 与 `manifest_version` 都固定为 3（不保留 v2 兼容）。

    `min_app_version` 钉住**能力依赖**：本插件自 0.4.1 起要求宿主
    `api.worldbook.sources()` 回传 `id`（用来和 `api.host.selection()` 的选中集
    对交集）。宿主 1.1.0 才加上该字段，所以下限必须是 1.1.0——写成 1.0.0 时
    旧主程序会静默装上、「精简选中世界书」报「无法识别选中的世界书」。
    """
    assert manifest['api_version'] == 3
    assert manifest['manifest_version'] == 3
    assert manifest['min_app_version'] == '1.1.0'


def test_legacy_keys_are_gone(manifest, frontend, contributions):
    """v3 删除且不保留别名：`permissions`、`frontend.entries[]`、旧三字段。"""
    assert 'permissions' not in manifest
    assert 'entries' not in frontend
    for legacy in ('panels', 'settings_tab', 'detail_tab'):
        assert legacy not in frontend, legacy
    assert set(frontend) <= {
        'entry', 'styles', 'global_styles', 'render_mode', 'contributions',
    }
    assert set(contributions) <= {'menus', 'slots', 'commands'}


def test_manifest_passes_main_app_contract(manifest):
    """用主程序真实校验器（strict）加载本插件 manifest：零 issue 是最硬断言。"""
    contract = _load_host_contract()
    normalized, issues = contract.validate_manifest(manifest, strict=True)
    assert issues == [], issues
    assert normalized['api_version'] == 3
    assert normalized['manifest_version'] == 3
    assert set(normalized['capabilities']) == EXPECTED_CAPABILITIES
    assert normalized['frontend']['render_mode'] == 'declarative'
    assert normalized['frontend']['contributions']['menus']
    assert len(normalized['frontend']['contributions']['commands']) == 4


def test_backend_entry_declares_package_with_init(manifest):
    entry = manifest['backend']['entry']
    assert re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', entry)
    assert os.path.isfile(os.path.join(ROOT, entry, '__init__.py'))


def test_declared_jobs_are_valid_and_registered(manifest):
    jobs = manifest['backend']['jobs']
    assert jobs, '至少要声明一个作业类型'
    for job_type in jobs:
        assert re.match(r'^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$', job_type), job_type
    assert 'ai.analyze' in jobs


def test_backend_declares_no_hooks(manifest):
    """本插件用不到后端 hook（前端通过 api.on 订阅核心事件）。"""
    assert not (manifest['backend'].get('hooks') or [])


def test_capabilities_are_declared_honestly(manifest):
    """v3 `capabilities` 取代 v2 `permissions`：如实披露，不缩水。"""
    capabilities = manifest.get('capabilities') or []
    assert capabilities, '能力声明不能为空（如实声明是审计的前提）'
    assert set(capabilities) == EXPECTED_CAPABILITIES, sorted(
        set(capabilities) ^ EXPECTED_CAPABILITIES)
    assert 'secrets.read' in capabilities
    assert 'network.outbound' in capabilities


def test_render_mode_is_declarative(manifest, frontend):
    """本插件的界面全部是宿主容器（surface）+ 贡献声明，不自绘、不注入全局样式。"""
    assert frontend.get('render_mode') == 'declarative'
    assert not (frontend.get('global_styles') or [])
    capabilities = set(manifest.get('capabilities') or [])
    assert 'ui.custom' not in capabilities
    assert 'ui.global_styles' not in capabilities


def test_frontend_entry_and_styles_exist(frontend):
    assert os.path.isfile(os.path.join(ROOT, frontend['entry']))
    for style in frontend['styles']:
        assert os.path.isfile(os.path.join(ROOT, style))


# --------------------------------------------------------------- 三原语结构


def test_contributions_declared(menus, slots, commands):
    """菜单树与命令必须声明；本插件没有 settings.section 插槽（设置走 surface）。"""
    assert menus, 'frontend.contributions.menus 不能为空'
    assert commands, 'frontend.contributions.commands 不能为空'
    assert slots == [], '本插件的设置页是独立 surface，不使用 settings.section 插槽'


def test_menu_ids_are_unique_and_match_pattern(menus):
    ids = [node.get('id') for _depth, _placement, node in _flatten(menus)]
    assert len(ids) == len(set(ids)), f'菜单节点 id 重复: {ids}'
    for node_id in ids:
        assert NODE_ID_PATTERN.match(str(node_id or '')), node_id


def test_command_ids_are_unique_and_match_pattern(commands):
    ids = [command.get('id') for command in commands]
    assert len(ids) == len(set(ids)), f'命令 id 重复: {ids}'
    for command_id in ids:
        assert NODE_ID_PATTERN.match(str(command_id or '')), command_id


def test_root_placements_are_known_and_children_omit_them(menus):
    for node in menus:
        assert node.get('placement') in MENU_PLACEMENTS, node.get('placement')
        for child in node.get('children') or []:
            assert 'placement' not in child, (
                f'子节点 {child.get("id")} 不得声明 placement（落点由根节点决定）'
            )


def test_node_types_are_valid_and_actions_resolve(nodes, commands):
    """节点类型合法；item 的 action 必须带 id；command 引用必须已声明。"""
    command_ids = {command.get('id') for command in commands}
    for _depth, _placement, node in nodes:
        node_type = node.get('type')
        assert node_type in {'item', 'submenu', 'separator'}, node.get('id')
        if node_type == 'separator':
            assert not node.get('label') and not node.get('action')
            continue
        if node_type == 'submenu':
            assert node.get('children'), f'submenu {node.get("id")} 缺少 children'
            continue
        action = node.get('action') or {}
        assert action.get('type') in {'surface', 'command', 'url'}, node.get('id')
        if action.get('type') == 'command':
            assert action.get('id') in command_ids, (
                f'菜单节点 {node.get("id")} 引用了未声明的命令: {action.get("id")}'
            )
        if action.get('type') == 'surface':
            assert action.get('id'), node.get('id')
        if action.get('type') == 'url':
            assert str(action.get('url') or '').startswith(('http://', 'https://'))


def test_command_declarations_complete(commands):
    """命令必须声明标题、处理器模块 / 导出名，模块文件真实存在。"""
    for command in commands:
        command_id = command.get('id')
        assert str(command.get('title') or '').strip(), command_id
        module = str(command.get('module') or '')
        assert module, f'命令 {command_id} 缺少 module'
        assert os.path.isfile(os.path.join(ROOT, module)), module
        assert str(command.get('export') or '').strip(), f'命令 {command_id} 缺少 export'


# --------------------------------------------------------------- 落点语义


def test_exactly_one_dock_entry(menus):
    """坞入口只能有一个（全局 8 槽位、单插件上限 2，这里保守占 1）。"""
    docks = [node for node in menus if node.get('placement') == 'dock']
    assert len(docks) == 1, f'应有且仅有一个 dock 入口，实际 {len(docks)} 个'


def test_workbench_dock_routes_to_hub_via_command(menus, nodes_by_id, commands):
    """坞入口 = 命令 open-workbench（快捷键 Ctrl+Shift+A，处理器打开 hub）。

    v3 用命令承载「有处理器 + 有快捷键」的入口；surface 的 type 只来自
    `api.surfaces.register()`，manifest 不再声明 surface type。
    """
    dock = nodes_by_id['workbench']
    assert dock.get('placement') == 'dock'
    assert dock.get('type') == 'item'
    assert dock['action'] == {'type': 'command', 'id': 'open-workbench'}
    assert str(dock.get('label') or '').strip()
    assert str(dock.get('description') or '').strip()

    open_workbench = next(c for c in commands if c['id'] == 'open-workbench')
    assert open_workbench.get('shortcut') == 'Ctrl+Shift+A'
    assert open_workbench.get('export') == 'openWorkbench'

    with open(os.path.join(ROOT, 'frontend', 'commands.js'), 'r', encoding='utf-8') as handle:
        command_source = handle.read()
    assert re.search(r"""openSurface\(ctx,\s*['"]hub['"]""", command_source)


def test_folder_context_group_has_expected_entries(nodes_by_id):
    """右键分类：`context.folder` 下 4 个 item，mode 过滤正确，且不再需要 when.target。"""
    group = nodes_by_id['ai-helper-context']
    assert group.get('placement') == 'context.folder'
    assert group.get('type') == 'submenu'
    children = {child['id']: child for child in group['children']}
    assert set(children) == {
        'analyze-folder', 'classify-folder', 'polish-embedded', 'polish-folder',
    }
    for child in children.values():
        assert child.get('type') == 'item'
        assert child.get('action', {}).get('type') == 'surface'
        assert str(child.get('disabledHint') or '').strip(), child['id']
        when = child.get('when') or {}
        assert 'selection' not in when, '右键分类的作用域是右键目标，与全局选中集无关'
        for key in REMOVED_WHEN_KEYS:
            assert key not in when, f'{child["id"]} 仍在使用已删除的 when.{key}'
    assert children['analyze-folder']['when'] == {'mode': ['cards']}
    assert children['classify-folder']['when'] == {'mode': ['cards']}
    assert children['polish-embedded']['when'] == {'mode': ['cards']}
    assert children['polish-folder']['when'] == {'mode': ['worldinfo']}


def test_batch_group_requires_selection(nodes_by_id):
    """批量菜单：`batch.actions` 下 3 个 item，全部声明 selection: any + disabledHint。"""
    group = nodes_by_id['ai-helper-batch']
    assert group.get('placement') == 'batch.actions'
    assert group.get('type') == 'submenu'
    children = {child['id']: child for child in group['children']}
    assert set(children) == {'analyze-selected', 'classify-selected', 'polish-selector'}
    for child in children.values():
        when = child.get('when') or {}
        assert when.get('selection') == 'any', child['id']
        assert str(child.get('disabledHint') or '').strip(), child['id']
        for key in REMOVED_WHEN_KEYS:
            assert key not in when, f'{child["id"]} 仍在使用已删除的 when.{key}'
    assert children['analyze-selected']['when']['mode'] == ['cards']
    assert children['classify-selected']['when']['mode'] == ['cards']
    assert children['polish-selector']['when']['mode'] == ['worldinfo']


def test_command_palette_entries(menus, commands):
    """命令面板列出 3 条命令入口；它们没有 when（全局可用）。"""
    palette = [node for node in menus if node.get('placement') == 'commandPalette']
    assert {node['id'] for node in palette} == {
        'open-review', 'propose-library', 'open-profiles',
    }
    command_ids = {command.get('id') for command in commands}
    for node in palette:
        assert node.get('action', {}).get('type') == 'command'
        assert node['action']['id'] in command_ids
        assert not (node.get('when') or {}), f'{node["id"]} 是全局入口，不该声明 when'
        assert str(node.get('label') or '').strip()
        assert str(node.get('description') or '').strip()


def test_settings_nav_entry_points_at_settings_surface(nodes_by_id):
    """设置导航项打开独立的 settings surface（不是内联插槽）。"""
    entry = nodes_by_id['assistant-settings']
    assert entry.get('placement') == 'settings.nav'
    assert entry.get('type') == 'item'
    assert entry['action'] == {'type': 'surface', 'id': 'settings'}
    assert str(entry.get('label') or '').strip()


def test_selection_dependent_nodes_have_hints(nodes):
    """依赖选中项的节点必须给出置灰理由（可见但置灰 + 提示，而不是消失）。"""
    for _depth, _placement, node in nodes:
        when = node.get('when') or {}
        if 'selection' in when:
            assert str(node.get('disabledHint') or '').strip(), (
                f'节点 {node.get("id")} 依赖选中项，必须声明 disabledHint'
            )


def test_when_config_require_keys_exist_in_schemas(manifest, nodes, commands):
    """`when.config.requireKeys` 读的是 config_schema / secret_schema 的键。"""
    declared = set()
    for field in ('config_schema', 'secret_schema'):
        schema = manifest.get(field) or {}
        declared.update((schema.get('properties') or {}).keys())

    entries = [node for _d, _p, node in nodes] + list(commands)
    for entry in entries:
        when = entry.get('when') or {}
        for key in ((when.get('config') or {}).get('requireKeys') or []):
            assert key in declared, f'{entry.get("id")} 引用了未声明的配置键: {key}'


def test_when_expressions_use_supported_shape(nodes, commands):
    """`when` 只声明、求值归宿主；v3 已删除 `target` / `contexts`。"""
    for entry in [node for _d, _p, node in nodes] + list(commands):
        when = entry.get('when')
        if when is None:
            continue
        assert isinstance(when, dict), entry.get('id')
        for key in REMOVED_WHEN_KEYS:
            assert key not in when, f'{entry.get("id")} 仍在使用已删除的 when.{key}'
        for mode in when.get('mode') or []:
            assert mode in KNOWN_MODES, mode
        if 'selection' in when:
            assert when['selection'] in SELECTION_MODES, when['selection']


def test_entry_shortcuts_are_wellformed_and_unique(commands):
    """`shortcut` 是「建议快捷键，宿主可覆盖」；格式必须与宿主折算规则一致。"""
    seen = {}
    for command in commands:
        combo = str(command.get('shortcut') or '').strip()
        if not combo:
            continue
        assert SHORTCUT_PATTERN.match(combo), (
            f'命令 {command.get("id")} 的快捷键格式不符合宿主折算规则: {combo!r}'
        )
        assert combo not in seen, (
            f'快捷键 {combo} 被多个命令声明: {seen[combo]} 与 {command.get("id")}'
        )
        seen[combo] = command.get('id')


def test_shortcuts_do_not_collide_with_host_reserved_combos(commands):
    """不要占用宿主已用的组合键（`Ctrl+K` = 命令面板）。"""
    reserved = {'Ctrl+K', 'Ctrl+Shift+K'}
    for command in commands:
        combo = str(command.get('shortcut') or '').strip()
        assert combo not in reserved, (
            f'命令 {command.get("id")} 占用了宿主保留快捷键 {combo}'
        )


def test_every_task_has_at_least_one_menu_node(menus, commands, tasks_source):
    """每个任务都必须至少有一个入口，否则「功能存在但进不去」。

    任务的唯一声明处是 `frontend/tasks.js`；解析它的 `surface:` 取值，
    再做双向核对：任务 → 菜单节点 / 命令处理器、菜单节点 → 已注册 surface。
    """
    task_surfaces = re.findall(r"surface:\s*'([a-z-]+)'", tasks_source)
    assert task_surfaces, '未能从 tasks.js 解析出任务 surface'
    assert len(task_surfaces) == len(set(task_surfaces)), '两个任务指向了同一个 surface'

    action_surfaces = {
        (node.get('action') or {}).get('id')
        for _depth, _placement, node in _flatten(menus)
        if (node.get('action') or {}).get('type') == 'surface'
    }
    with open(os.path.join(ROOT, 'frontend', 'commands.js'), 'r', encoding='utf-8') as handle:
        command_surfaces = set(re.findall(
            r"""openSurface\(ctx, ['"]([a-z-]+)['"]""", handle.read()))
    reachable = action_surfaces | command_surfaces
    for surface in task_surfaces:
        assert surface in reachable, f'任务面板 {surface} 没有任何入口'


def test_all_action_surfaces_are_registered_in_frontend(menus, plugin_source, panel_source):
    """`action.surface.id` 必须在插件侧真实注册过，否则宿主只警告并忽略。"""
    registered = set(re.findall(r"^\s{2}([a-z0-9-]+):\s*\{ title:", panel_source, re.M))
    registered.update(re.findall(r"api\.surfaces\.register\(\s*'([a-z0-9-]+)'", plugin_source))
    assert registered, '未能解析出已注册的 surface'
    assert 'surfaceIds' in plugin_source, 'plugin.js 应遍历 SURFACES 批量注册'

    for _depth, _placement, node in _flatten(menus):
        action = node.get('action') or {}
        if action.get('type') != 'surface':
            continue
        assert action.get('id') in registered, (
            f'菜单节点 {node.get("id")} 指向未注册的 surface: {action.get("id")}'
        )

    # 反向：除程序化打开的 prompt-preview 外，注册的 surface 都应该有入口
    # （菜单节点 action 或命令处理器）。
    action_surfaces = {
        (node.get('action') or {}).get('id')
        for _depth, _placement, node in _flatten(menus)
        if (node.get('action') or {}).get('type') == 'surface'
    }
    with open(os.path.join(ROOT, 'frontend', 'commands.js'), 'r', encoding='utf-8') as handle:
        action_surfaces |= set(re.findall(
            r"""openSurface\(ctx, ['"]([a-z-]+)['"]""", handle.read()))
    programmatic = {'prompt-preview', 'history'}
    missing = registered - action_surfaces - programmatic
    assert not missing, f'以下 surface 没有任何入口，用户永远打不开: {sorted(missing)}'


# --------------------------------------------------------------- 图标


def test_contribution_icons_are_plugin_assets(menus, commands):
    """品牌图标统一用插件自己的 `frontend/assets/icon.svg`（同一插件到处同一图标）。"""
    icon = 'frontend/assets/icon.svg'
    assert os.path.isfile(os.path.join(ROOT, icon)), f'插件图标不存在: {icon}'

    for _depth, _placement, node in _flatten(menus):
        if node.get('type') == 'separator':
            continue
        assert node.get('icon') == icon, (
            f'菜单节点 {node.get("id")} 图标应为 {icon}，实际 {node.get("icon")!r}'
        )
    for command in commands:
        assert command.get('icon') == icon, (
            f'命令 {command.get("id")} 图标应为 {icon}，实际 {command.get("icon")!r}'
        )


@pytest.mark.skipif(not HOST_AVAILABLE, reason='主程序仓库不可用')
def test_frontend_ui_icons_exist_in_host():
    """`api.ui.icon()` 引用的宿主图标名必须存在。"""
    sys.path.insert(0, MAIN_APP_DIR)
    try:
        from core.plugins.icons import sprite_for_icon  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover
        pytest.skip(f'无法导入主程序图标索引: {exc}')
    finally:
        sys.path.remove(MAIN_APP_DIR)

    pattern = re.compile(r"""api\.ui\.icon\(\s*['"]([a-z0-9-]+)['"]""")
    used = set()
    for dirpath, _dirnames, filenames in os.walk(os.path.join(ROOT, 'frontend')):
        for filename in filenames:
            if not filename.endswith('.js'):
                continue
            with open(os.path.join(dirpath, filename), 'r', encoding='utf-8') as handle:
                used.update(pattern.findall(handle.read()))

    missing = sorted(name for name in used if not sprite_for_icon(name))
    assert missing == [], f'引用了不存在的宿主图标: {missing}'


@pytest.mark.skipif(not HOST_AVAILABLE, reason='主程序仓库不可用')
def test_panel_surface_icons_are_host_icons(panel_source):
    """`panel.js` 的 SURFACES 图标名必须存在（它们是面板标题行的图标）。"""
    sys.path.insert(0, MAIN_APP_DIR)
    try:
        from core.plugins.icons import sprite_for_icon  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover
        pytest.skip(f'无法导入主程序图标索引: {exc}')
    finally:
        sys.path.remove(MAIN_APP_DIR)

    names = re.findall(r"\{ title: '[^']+', icon: '([a-z0-9-]+)'", panel_source)
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


def test_tag_pool_categories_are_declared_as_strings(manifest):
    """标签池的分类白/黑名单与每分类规则必须是**声明过的字符串键**。

    宿主的 `api.config.set()` 只接受 `config_schema` 里声明过的键：写内部结构
    `tag_pool`（dict）会被拒绝，表现为「保存并应用后并没有保存」。
    """
    properties = manifest['config_schema']['properties']
    for key in ('tag_pool_categories_include', 'tag_pool_categories_exclude',
                'tag_category_rules'):
        spec = properties[key]
        assert spec['type'] == 'string', f'{key} 必须是 string（宿主表单只支持标量）'
        assert str(spec.get('title') or '').strip(), f'{key} 需要中文标题'
        assert str(spec.get('description') or '').strip(), f'{key} 需要填写说明'


def test_max_tags_is_not_in_host_config_form(manifest):
    """「最大总数量」只在 AI 助手设置里配置，不出现在插件管理页。

    宿主的 `api.config.set()` 只接受 `config_schema` 声明过的键，而声明过的键
    一定会渲染在插件管理页 —— 所以这个值改存插件私有数据
    （`config.PRIVATE_SETTINGS_KEY = 'tag_pool_settings'`），manifest 里不再声明。
    """
    properties = manifest['config_schema']['properties']
    assert 'max_tags' not in properties, '最大总数量不该出现在宿主配置表单里'
    assert properties['tag_pool_budget_tokens']['type'] == 'integer', '预算仍在宿主表单里'


def test_legacy_global_confidence_is_gone(manifest):
    """全局「标签置信度下限」已被「每分类置信度」取代，不应再出现在配置表单里。"""
    properties = manifest['config_schema']['properties']
    assert 'confidence_min' not in properties


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
    """插件后端**零越界**：不得导入主程序 `core.*`（域命名空间 API 已覆盖）。"""
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
                yield os.path.relpath(path, ROOT).replace('\\', '/'), _strip_js_comments(
                    handle.read())


def test_frontend_avoids_removed_host_apis():
    """v3 删除的 API / 旧的入口运行期注册都不得再出现。"""
    forbidden = {
        'setFullscreenScrim': '宿主自己管遮罩合成（pluginSurfaceHost.js）',
        'registerModal': 'v3 已删除，改用 contributions + surfaces',
        'registerSettingsSection': 'v3 已删除，改用 contributions + slots',
        'entries': 'v3 已删除 api.entries，改用 api.contributions',
    }
    offenders = []
    for relative, source in _iter_frontend_js():
        for name, reason in forbidden.items():
            if re.search(rf'\bapi\.{name}\b', source):
                offenders.append(f'{relative}: api.{name}（{reason}）')
    assert offenders == []


def test_frontend_has_no_global_keydown_or_overlay_teardown():
    """插件不再自己承担层级 / 焦点 / ESC / 遮罩。"""
    offenders = []
    for relative, source in _iter_frontend_js():
        if relative.endswith('panel.js'):
            # panel.js 是 surface 内容渲染器，不该绑全局键盘
            if re.search(r"document\.addEventListener\(\s*'keydown'", source):
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


def test_contribution_writes_are_replayed_after_registry_ready(plugin_source):
    """回归：`activate()` 早于 ContributionRegistry 建表，状态写入必须可重放。

    `PluginHost.loadAll()` 先 `loadPlugin()`（= 调用 `activate`），之后才把
    manifest.contributions 装进注册表。因此 activate() 期间
    `api.contributions.update()` 会查不到节点而返回 false，自检结论会丢失。
    """
    source = _strip_js_comments(plugin_source)

    assert "api.on('contributions:changed'" in source, (
        'plugin.js 必须订阅 contributions:changed 以重放 activate() 期间的状态写入'
    )
    assert 'createContributionReporter' in source

    start = source.index('function createContributionReporter')
    end = source.index('export async function activate')
    reporter_body = source[start:end]
    outside = source[:start] + source[end:]

    assert 'api.contributions.update(' in reporter_body, '回报器必须真的写贡献项状态'
    assert 'api.contributions.update(' not in outside, (
        '自检结论必须走 reporter（直接写会因注册表未就绪而丢失）'
    )
    assert 'reporter.setStatus(' in outside, 'activate() 里应通过 reporter 写状态'


def test_settings_surface_refreshes_every_time_it_opens(plugin_source):
    """设置面板每次打开都要重新读配置。

    宿主会**复用已挂载的容器**（`mount` 只跑一次，之后只投递 `onOpen`），而
    「主程序 → 插件管理 → 配置」改的是同一份配置。不在 `onOpen` 里 refresh，
    用户就会看到上一次的旧值（实测：宿主改成 30、插件里还是 20）。
    """
    source = _strip_js_comments(plugin_source)
    assert "surfaceId === 'settings'" in source, '设置面板需要在 onOpen 里单独刷新'
    assert 'view.refresh()' in source, '要调用设置视图的 refresh()'


def test_pending_badge_goes_through_injected_writer(plugin_source):
    """待审批角标必须走注入的写入器（带「注册表就绪重放」）。"""
    source = _strip_js_comments(plugin_source)
    assert 'setPendingBadge' in source, 'plugin.js 应暴露待审批角标的写入器'
    assert 'contributions:changed' in source, '必须订阅 contributions:changed 做重放'
    assert 'api.contributions.update(' in source, '最终仍要写到宿主贡献项角标上'


def test_commands_module_uses_host_api():
    """命令处理器通过 ctx.api 访问宿主 API，不直接 import 宿主模块。"""
    with open(os.path.join(ROOT, 'frontend', 'commands.js'), 'r', encoding='utf-8') as handle:
        source = _strip_js_comments(handle.read())
    assert 'ctx.api' in source or 'ctx && ctx.api' in source
    assert 'api.entries' not in source
    assert source.count('export function') == 4, '应有 4 个命令处理器导出'


# --------------------------------------------------------------- 真实校验器


@pytest.mark.skipif(not os.path.isdir(MAIN_APP_DIR), reason='主程序仓库不可用')
def test_plugin_is_registered_as_official_in_main_app_catalog():
    """本插件必须在主程序包内 catalog 里登记为 official（tier 只能由宿主决定）。"""
    catalog_path = os.path.join(MAIN_APP_DIR, 'static', 'plugins', 'catalog.json')
    if not os.path.isfile(catalog_path):
        pytest.skip('主程序 catalog.json 不存在')

    with open(catalog_path, 'r', encoding='utf-8') as handle:
        payload = json.load(handle)

    entries = {item.get('id'): item for item in (payload.get('plugins') or [])}
    assert 'stm-ai-assistant' in entries, '主程序 catalog 必须登记本插件'
    assert entries['stm-ai-assistant'].get('tier') == 'official'


@pytest.mark.skipif(not HOST_AVAILABLE, reason='主程序仓库不可用')
def test_manifest_normalizes_without_warnings_through_main_app():
    """用主程序 manifest 层加载（若已迁移到 v3）：零警告 + contributions 计数。"""
    sys.path.insert(0, MAIN_APP_DIR)
    try:
        from core.plugins.manifest import PluginManifest  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover
        pytest.skip(f'无法导入主程序 manifest 校验器: {exc}')
    finally:
        sys.path.remove(MAIN_APP_DIR)

    parsed = PluginManifest.load(ROOT, check_dir_name=False)
    if not hasattr(parsed, 'frontend_contributions'):
        pytest.skip('主程序尚未迁移到 v3（PluginManifest 无 frontend_contributions）')

    assert parsed.api_version == 3
    assert parsed.manifest_version == 3
    assert parsed.plugin_id == 'stm-ai-assistant'
    assert parsed.backend_jobs == [
        'ai.analyze', 'ai.wi_rewrite', 'ai.classify', 'ai.propose', 'ai.agent',
    ]
    assert parsed.has_backend and parsed.has_frontend
    assert parsed.frontend_render_mode == 'declarative'
    assert set(parsed.capabilities) == EXPECTED_CAPABILITIES
    assert len(parsed.frontend_contributions['menus']) == 7
    assert len(parsed.frontend_contributions['commands']) == 4
    assert parsed.warnings == [], parsed.warnings
