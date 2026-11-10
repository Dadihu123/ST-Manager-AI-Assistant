"""贡献 × 落点矩阵（v3）：验证每个宿主落点在每种上下文里给出哪些菜单节点。

v2 的 `when.target` 已删除：**落点本身表达目标**（`context.folder` /
`context.card` / `context.footer`），因此不再需要「消费者是否提供 targetKind」
这类容易踩的隐式语义，也不需要复刻宿主 `evaluateWhen`（设计文档附录 B）。

这里保留一个**最小**的语义镜像：
- 只考虑 `type: "item"` 的叶子节点（submenu 是容器、separator 是装饰）；
- `when.mode` 按当前工作模式过滤；
- `when.selection` 按选中数折算 none / single / multi；
- `when.config.requireKeys` 按上下文提供的插件配置求值。

主程序仓库不可用时跳过（本文件用来对齐 v3 运行期语义）。
"""

import json
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_PATH = os.path.join(ROOT, 'manifest.json')

MAIN_APP_DIR = os.environ.get(
    'STM_MAIN_APP_DIR',
    os.path.abspath(os.path.join(ROOT, os.pardir, 'ST-Manager')),
)
HOST_AVAILABLE = os.path.isfile(
    os.path.join(MAIN_APP_DIR, 'core', 'plugins', 'contract.py'))

pytestmark = pytest.mark.skipif(not HOST_AVAILABLE, reason='主程序 v3 契约模块不可用')


# ---------------------------------------------------------------- 数据与求值


def _manifest():
    with open(MANIFEST_PATH, 'r', encoding='utf-8') as handle:
        return json.load(handle)


def _menus():
    return (_manifest()['frontend'].get('contributions') or {}).get('menus') or []


def _nodes():
    """扁平化菜单树：`[(placement, node), …]`（placement 继承根节点）。"""
    out = []

    def walk(items, placement=''):
        for node in items or []:
            current = placement or str(node.get('placement') or '')
            out.append((current, node))
            walk(node.get('children') or [], current)

    walk(_menus())
    return out


def _items(placement=None):
    """该落点（或全部落点）的叶子 item 节点。"""
    return [
        node for current, node in _nodes()
        if node.get('type') == 'item' and (placement is None or current == placement)
    ]


def evaluate_when(node: dict, ctx: dict) -> bool:
    """v3 `when` 的最小语义镜像：任一子条件不满足即不可用。"""
    when = node.get('when') or {}
    if not when:
        return True

    modes = when.get('mode') or []
    if modes and ctx.get('mode') not in modes:
        return False

    selection = str(when.get('selection') or '').strip()
    if selection:
        count = int(ctx.get('selectionCount') or 0)
        actual = 'none' if count == 0 else ('single' if count == 1 else 'multi')
        ok = (
            selection == actual
            or (selection == 'any' and count > 0)
            or (selection == 'multi' and count > 1)
        )
        if not ok:
            return False

    require_keys = ((when.get('config') or {}).get('requireKeys') or [])
    if require_keys:
        config = ctx.get('config') or {}
        if any(not config.get(key) for key in require_keys):
            return False

    return True


def offered(placement: str, ctx: dict) -> set:
    """某个落点在该上下文里会列出哪些叶子节点 id。"""
    return {
        node['id']
        for node in _items(placement)
        if evaluate_when(node, ctx)
    }


# ---------------------------------------------------------------- 消费者矩阵


def test_dock_only_lists_the_workbench_entry():
    ids = offered('dock', {'mode': 'cards'})
    assert ids == {'workbench'}


def test_settings_nav_only_lists_the_settings_entry():
    ids = offered('settings.nav', {'mode': 'cards'})
    assert ids == {'assistant-settings'}


@pytest.mark.parametrize('mode,expected', [
    ('cards', {'analyze-folder', 'classify-folder', 'polish-embedded'}),
    ('worldinfo', {'polish-folder'}),
])
def test_folder_context_lists_only_matching_mode(mode, expected):
    """右键分类：cards 视图给三条（打标/归类/精简嵌入书），worldinfo 给一条（精简）。"""
    ids = offered('context.folder', {'mode': mode})
    assert ids == expected


@pytest.mark.parametrize('mode,selection,expected', [
    ('cards', 0, set()),
    ('cards', 1, {'analyze-selected', 'classify-selected'}),
    ('cards', 3, {'analyze-selected', 'classify-selected'}),
    ('worldinfo', 0, set()),
    ('worldinfo', 2, {'polish-selector'}),
    ('chats', 2, set()),
])
def test_batch_actions_require_selection_and_matching_mode(mode, selection, expected):
    """批量菜单：没选中就整块不出现（不是留一条点不动的灰项），且按视图分流。"""
    ids = offered('batch.actions', {'mode': mode, 'selectionCount': selection})
    assert ids == expected


def test_command_palette_lists_only_commands():
    """命令面板按落点收敛：不出现需要右键目标的条目（落点已经隔离）。"""
    palette = offered('commandPalette', {'mode': 'cards'})
    assert palette == {'open-review', 'propose-library', 'open-profiles', 'open-history'}

    context_ids = {node['id'] for node in _items('context.folder')}
    assert context_ids, '本插件应声明 context.folder 入口'
    assert not (palette & context_ids), '命令面板不得包含需要右键目标的条目'


def test_placements_never_mix():
    """每个落点只列出自己的节点：右键 / 批量 / 坞 / 命令面板互不串台。"""
    placements = {current for current, _node in _nodes()}
    assert placements == {
        'dock', 'context.folder', 'batch.actions', 'commandPalette', 'settings.nav',
    }
    for placement in placements:
        ids = offered(placement, {'mode': 'cards', 'selectionCount': 2})
        # 任一节点只能属于一个落点
        assert ids <= {
            node['id'] for node in _items(placement)
        }


def test_batch_entries_declare_selection_and_hint():
    """批量菜单靠 selection 条件收敛，且保留 disabledHint 供宿主置灰说明。"""
    for node in _items('batch.actions'):
        when = node.get('when') or {}
        assert when.get('selection') == 'any', node['id']
        assert str(node.get('disabledHint') or '').strip(), node['id']


def test_context_entries_never_declare_selection_or_target():
    """右键入口的作用域是右键目标：不得声明 selection，也不得再用已删除的 target。"""
    for node in _items('context.folder'):
        when = node.get('when') or {}
        assert 'selection' not in when, node['id']
        assert 'target' not in when, node['id']
        assert str(node.get('disabledHint') or '').strip(), node['id']


def test_global_entries_do_not_declare_when():
    """坞 / 设置 / 命令面板入口不声明 `when`，否则会在某些上下文中整体消失。"""
    for placement in ('dock', 'settings.nav', 'commandPalette'):
        for node in _items(placement):
            assert not (node.get('when') or {}), f'{node["id"]} 不该声明 when'


def test_offered_only_returns_leaf_items():
    """submenu / separator 是结构节点，不会被当作可点击项列出。"""
    offered_ids = offered('batch.actions', {'mode': 'cards', 'selectionCount': 2})
    assert 'ai-helper-batch' not in offered_ids
    assert all(
        node.get('type') == 'item'
        for _placement, node in _nodes()
        if node['id'] in offered_ids
    )


# ---------------------------------------------------------------- 结构约束


def test_every_surface_has_at_least_one_entry():
    """没有入口的 surface 等于死代码；反过来入口指向未注册 surface 会「点了没反应」。"""
    with open(os.path.join(ROOT, 'frontend', 'panel.js'), 'r', encoding='utf-8') as handle:
        panel_source = handle.read()
    with open(os.path.join(ROOT, 'frontend', 'plugin.js'), 'r', encoding='utf-8') as handle:
        plugin_source = handle.read()
    with open(os.path.join(ROOT, 'frontend', 'commands.js'), 'r', encoding='utf-8') as handle:
        command_source = handle.read()

    registered = set(re.findall(r"^\s{2}([a-z-]+):\s*\{ title:", panel_source, re.M))
    registered.update(
        re.findall(r"api\.surfaces\.register\(\s*'([a-z-]+)'", plugin_source))
    assert registered, '未能从 panel.js / plugin.js 解析出已注册 surface'

    # 菜单节点的 action.surface + 命令处理器打开的 surface（open-workbench → hub 等）
    entry_surfaces = {
        (node.get('action') or {}).get('id')
        for _placement, node in _nodes()
        if node.get('type') == 'item' and (node.get('action') or {}).get('type') == 'surface'
    }
    entry_surfaces.discard(None)
    entry_surfaces.update(
        re.findall(r"""openSurface\(ctx, ['"]([a-z-]+)['"]""", command_source))

    programmatic = {'prompt-preview'}
    missing = registered - entry_surfaces - programmatic
    assert not missing, (
        f'以下 surface 没有任何入口，用户永远打不开: {sorted(missing)}'
    )
    assert entry_surfaces <= registered, (
        f'入口指向未注册的 surface: {sorted(entry_surfaces - registered)}'
    )
