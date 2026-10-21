"""入口 × 消费者矩阵：验证每个宿主落点在每种上下文里给出哪些入口。

重构把入口从 4 条扩到 11 条，`when` 条件因此变复杂（`mode` × `target` × `selection`）。
静态读 JSON 看不出「右键世界书分类时打标入口会不会也出现」这类错误，
所以这里按**宿主消费者**分别求值。

求值逻辑复刻 `ST-Manager/static/js/entryRegistry.js:197-257` 的 `evaluateWhen`，
含一条容易踩的语义：

> `when.target` 只对右键菜单求值——只有消费方提供了 `targetKind` 才检查。
> 坞 / 命令面板 / 批量菜单都不提供，所以 `context` 入口在那里**恒真**。

正因如此，`context` 类入口必须靠**消费者筛选**（只有右键菜单列出 context）
而不是靠 `when.target` 来保证「不出现在命令面板」。这条也一并钉住。

跳过条件：主程序仓库不可用。
"""

import json
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_PATH = os.path.join(ROOT, 'manifest.json')

MAIN_APP_DIR = os.environ.get(
    'STM_MAIN_APP_DIR',
    os.path.abspath(os.path.join(ROOT, os.pardir, 'ST-Manager')),
)
HOST_AVAILABLE = os.path.isfile(os.path.join(MAIN_APP_DIR, 'static', 'js', 'entryRegistry.js'))

pytestmark = pytest.mark.skipif(not HOST_AVAILABLE, reason='主程序仓库不可用')


def _entries():
    with open(MANIFEST_PATH, 'r', encoding='utf-8') as handle:
        return json.load(handle)['frontend']['entries']


def evaluate_when(entry: dict, ctx: dict) -> bool:
    """复刻宿主 `evaluateWhen`：任一子条件不满足即不可用。"""
    when = entry.get('when') or {}
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

    target = str(when.get('target') or '').strip()
    if target:
        actual_target = str(ctx.get('targetKind') or '').strip()
        # 关键语义：没有 targetKind 时跳过（不是「不满足」）
        if actual_target and target != 'any' and target != actual_target:
            return False

    return True


def offered(kinds, ctx) -> set:
    """某个消费者（按 kind 筛选）在该上下文里会列出的入口 id。"""
    return {
        entry['id']
        for entry in _entries()
        if entry.get('kind') in kinds and evaluate_when(entry, ctx)
    }


# ---------------------------------------------------------------- 消费者矩阵

def test_dock_only_lists_dock_entries():
    ids = offered({'dock'}, {'mode': 'cards'})
    assert ids == {'workbench'}


def test_settings_page_only_lists_settings_entries():
    ids = offered({'settings'}, {'mode': 'cards'})
    assert ids == {'assistant-settings'}


@pytest.mark.parametrize('mode,expected', [
    ('cards', {'analyze-folder', 'classify-folder', 'polish-embedded'}),
    ('worldinfo', {'polish-folder'}),
])
def test_context_menu_offers_exactly_the_matching_entries(mode, expected):
    """右键分类：cards 视图给三条（打标/归类/精简嵌入书），worldinfo 给一条（精简）。"""
    ids = offered({'context'}, {'mode': mode, 'targetKind': 'folder'})
    assert ids == expected


@pytest.mark.parametrize('mode,selection,expected', [
    ('cards', 0, set()),
    ('cards', 1, {'analyze-selected', 'classify-selected'}),
    ('cards', 3, {'analyze-selected', 'classify-selected'}),
    ('worldinfo', 0, set()),
    ('worldinfo', 2, {'polish-selector'}),
    ('chats', 2, set()),
])
def test_batch_menu_requires_selection_and_matching_mode(mode, selection, expected):
    """批量菜单：没选中就整块不出现（不是留一条点不动的灰项），且按视图分流。"""
    ids = offered({'batch'}, {'mode': mode, 'selectionCount': selection})
    assert ids == expected


def test_command_palette_excludes_context_entries():
    """命令面板没有「右键目标」，因此**不得**列出 context 类入口。

    `when.target` 在命令面板里恒真（不传 targetKind），所以只能靠消费方筛选。
    这条是 F22 的回归护栏：宿主 `commandPalette.searchable()` 目前**没有**过滤
    context，补丁见 docs/2026-10-19-host-patch.md §5.1。
    """
    entries = _entries()
    context_ids = {entry['id'] for entry in entries if entry.get('kind') == 'context'}
    assert context_ids, '本插件应声明 context 入口'

    # 宿主当前的 searchable() 会把它们放进来 → 说明补丁是必需的
    naive = {entry['id'] for entry in entries if entry.get('kind') != 'status'}
    assert context_ids <= naive, '前提变化：searchable() 已不再包含 context，请更新本文档'

    # 补丁后的期望：命令面板只给 command + dock
    fixed = offered({'command', 'dock'}, {'mode': 'cards'})
    assert fixed == {'workbench', 'open-review', 'propose-library', 'open-profiles'}
    assert not (fixed & context_ids), '命令面板不得包含需要右键目标的入口'


def test_batch_menu_shows_plugin_entries_only_when_applicable():
    """批量菜单按 `filter(enabled)` 收敛，所以「没选中就消失」是预期行为。

    与右键菜单相反：右键项不适用时可见但置灰（带 `disabledHint`）。
    """
    entries = _entries()
    for entry in entries:
        if entry.get('kind') == 'batch':
            assert entry.get('requires_selection') is True, entry['id']
            assert str(entry.get('disabledHint') or '').strip(), entry['id']
        if entry.get('kind') == 'context':
            assert str(entry.get('disabledHint') or '').strip(), entry['id']


# ---------------------------------------------------------------- 结构约束

def test_global_entries_do_not_declare_when():
    """坞 / 设置 / 命令入口不声明 `when`，否则会在某些上下文中整体消失。"""
    for entry in _entries():
        if entry.get('kind') in ('dock', 'settings', 'command'):
            assert not (entry.get('when') or {}), f'{entry["id"]} 不该声明 when'


def test_context_entries_never_declare_selection():
    """右键入口的作用域是右键目标，与全局选中集无关。"""
    for entry in _entries():
        if entry.get('kind') != 'context':
            continue
        when = entry.get('when') or {}
        assert when.get('target') == 'folder', f'{entry["id"]} 缺少 when.target'
        assert 'selection' not in when, f'{entry["id"]} 不得声明 when.selection'


def test_every_surface_has_at_least_one_entry():
    """没有入口的 surface 等于死代码；反过来入口指向未注册 surface 会「点了没反应」。"""
    import re

    entries = _entries()
    entry_surfaces = {(entry.get('surface') or {}).get('id') for entry in entries}

    with open(os.path.join(ROOT, 'frontend', 'panel.js'), 'r', encoding='utf-8') as handle:
        panel_source = handle.read()
    registered = set(re.findall(r"^\s{2}([a-z-]+):\s*\{ title:", panel_source, re.M))
    assert registered, '未能从 panel.js 解析出 SURFACES'

    assert registered <= entry_surfaces, (
        f'以下 surface 没有任何入口，用户永远打不开: {sorted(registered - entry_surfaces)}'
    )
    assert entry_surfaces <= registered, (
        f'以下 surface 未在 panel.js 注册: {sorted(entry_surfaces - registered)}'
    )
