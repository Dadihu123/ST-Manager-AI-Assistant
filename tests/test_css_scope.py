"""CSS 契约（F11 作用域 + F13/F15 声明层）。

`core/plugins/installer.py:595-640` 是**字符串包含**判定：顶层选择器里必须出现
`.plugin-stm-ai-assistant` 或 `[data-plugin="stm-ai-assistant"]`。
这里复刻该算法做静态自检，并在主程序可用时用真实实现交叉验证。

声明层（F13/F15）的判据来自 `installer.scan_css_declarations`：
`position_fixed` / `unknown_tokens` / `important` / `hardcoded_sprite`。
"""

import json
import os
import re
import sys
import tempfile

import pytest

from conftest import MAIN_APP_DIR, ROOT

PLUGIN_ID = 'stm-ai-assistant'
CSS_PATH = os.path.join(ROOT, 'frontend', 'plugin.css')

SKIP_HEADS = ('@media', '@supports', '@layer', '@container', '@keyframes', '@font-face')
GROUPING_AT_RULES = ('@media', '@supports', '@layer', '@container')
OPAQUE_AT_RULES = ('@keyframes', '@font-face')

#: 宿主 sprite 引用（F15）：插件不得硬编码 `url('/static/icons/ui.svg#icon-x')`
SPRITE_REFERENCE_PATTERN = re.compile(
    r'/static/icons/|(?<![\w/-])icons/(?:ui|detail|sidebar|preset)\.svg'
)


def _read_css() -> str:
    with open(CSS_PATH, 'r', encoding='utf-8') as handle:
        return handle.read()


def _strip_comments(source: str) -> str:
    """剔除 CSS 注释（注释里会说明旧模式已删除，不该被断言命中）。"""
    return re.sub(r'/\*.*?\*/', '', source, flags=re.S)


def scan_css_scope(css_path: str, plugin_id: str) -> list:
    """复刻 `installer.scan_css_scope`（仅取判定所需逻辑）。"""
    with open(css_path, 'r', encoding='utf-8') as handle:
        source = handle.read()

    scope_tokens = (
        f'.plugin-{plugin_id}', f'[data-plugin="{plugin_id}"]', f"[data-plugin='{plugin_id}']",
    )

    violations = []
    stack = []
    buffer = []
    skip_blocks = 0
    index = 0
    length = len(source)

    while index < length:
        char = source[index]

        if char == '/' and index + 1 < length and source[index + 1] == '*':
            end = source.find('*/', index + 2)
            index = length if end == -1 else end + 2
            continue

        if char == '{':
            selector = ''.join(buffer).strip()
            buffer = []
            head = selector.split(None, 1)[0].lower() if selector else ''

            if head.startswith('@'):
                if head in SKIP_HEADS:
                    skip_blocks += 1
                stack.append(head)
            else:
                if skip_blocks == 0 and selector and not any(token in selector for token in scope_tokens):
                    first = selector.split(',', 1)[0].strip().lower()
                    is_keyframe = first in ('from', 'to') or bool(re.match(r'^\d+(\.\d+)?%$', first))
                    if not is_keyframe:
                        violations.append(selector)
                stack.append('')
            index += 1
            continue

        if char == '}':
            if stack:
                head = stack.pop()
                if head.startswith('@') and head in SKIP_HEADS:
                    skip_blocks = max(0, skip_blocks - 1)
            buffer = []
            index += 1
            continue

        buffer.append(char)
        index += 1

    return violations


# --------------------------------------------------------------- 声明层复刻


def _parse_css_blocks(source: str) -> dict:
    """复刻 `installer._parse_css_blocks`（注释已剔除）。"""
    root = {'prelude': '', 'at': '', 'children': [], 'decls': []}
    stack = [root]
    buffer = []
    index = 0
    length = len(source)

    while index < length:
        char = source[index]

        if char == '/' and index + 1 < length and source[index + 1] == '*':
            end = source.find('*/', index + 2)
            index = length if end == -1 else end + 2
            continue

        if char == '{':
            prelude = ''.join(buffer).strip()
            buffer = []
            head = prelude.split(None, 1)[0].lower() if prelude else ''
            node = {
                'prelude': prelude,
                'at': head if head.startswith('@') else '',
                'children': [],
                'decls': [],
            }
            stack[-1]['children'].append(node)
            stack.append(node)
            index += 1
            continue

        if char in '};':
            text = ''.join(buffer).strip()
            if text:
                stack[-1]['decls'].append(text)
            buffer = []
            if char == '}' and len(stack) > 1:
                stack.pop()
            index += 1
            continue

        buffer.append(char)
        index += 1

    tail = ''.join(buffer).strip()
    if tail:
        stack[-1]['decls'].append(tail)
    return root


def _iter_var_references(value: str):
    """产出 `var(--x)` 引用：`(名字, 是否有回退值)`。复刻 `installer._iter_var_references`。"""
    text = str(value or '')
    index = 0

    while True:
        start = text.find('var(', index)
        if start == -1:
            return

        position = start + 4
        depth = 0
        has_fallback = False
        name_chars = []
        while position < len(text):
            char = text[position]
            if char == '(':
                depth += 1
            elif char == ')':
                if depth == 0:
                    break
                depth -= 1
            elif char == ',' and depth == 0:
                has_fallback = True
            elif depth == 0 and not has_fallback:
                name_chars.append(char)
            position += 1

        name = ''.join(name_chars).strip()
        if name.startswith('--'):
            yield name, has_fallback
        index = max(position + 1, start + 4)


def _resolve_known_tokens() -> frozenset:
    """从主程序 `variables.css` 解析宿主令牌清单。"""
    path = os.path.join(MAIN_APP_DIR, 'static', 'css', 'modules', 'variables.css')
    tokens = set()

    def walk(node):
        for decl in node['decls']:
            name = decl.split(':', 1)[0].strip()
            if name.startswith('--'):
                tokens.add(name)
        for child in node['children']:
            walk(child)

    with open(path, 'r', encoding='utf-8') as handle:
        walk(_parse_css_blocks(handle.read()))
    return frozenset(tokens)


def scan_css_declarations(css_path: str, plugin_id: str, *, known_tokens=None) -> dict:
    """复刻 `installer.scan_css_declarations`（F13 / F15）。"""
    with open(css_path, 'r', encoding='utf-8') as handle:
        source = handle.read()

    if known_tokens is None:
        known_tokens = frozenset()

    findings = {key: [] for key in (
        'position_fixed', 'unknown_tokens', 'important', 'hardcoded_sprite',
    )}
    root = _parse_css_blocks(source)

    def is_scoped(selector: str) -> bool:
        tokens = (
            f'.plugin-{plugin_id}', f'[data-plugin="{plugin_id}"]',
            f"[data-plugin='{plugin_id}']",
        )
        return any(token in selector for token in tokens)

    def walk(node, selector: str):
        for decl in node['decls']:
            prop, _, raw_value = decl.partition(':')
            prop = prop.strip().lower()
            value = raw_value.strip()
            if not prop or not value:
                continue

            if prop == 'position' and value.split(None, 1)[0].lower() == 'fixed':
                findings['position_fixed'].append(f'{selector or "顶层"} 使用 position: fixed')

            if '!important' in value.lower() and not is_scoped(selector):
                findings['important'].append(f'{selector or "顶层"} 在非作用域选择器上用 !important')

            if SPRITE_REFERENCE_PATTERN.search(value):
                findings['hardcoded_sprite'].append(f'{selector or "顶层"} 硬编码宿主 sprite: {value}')

            for name, has_fallback in _iter_var_references(value):
                if has_fallback or name in known_tokens:
                    continue
                findings['unknown_tokens'].append(f'{selector or "顶层"} 引用未知令牌 {name} 且无回退值')

        for child in node['children']:
            head = child['at']
            if head in OPAQUE_AT_RULES:
                continue
            if head:
                walk(child, selector)
            else:
                walk(child, child['prelude'])

    for child in root['children']:
        if child['at'] in OPAQUE_AT_RULES:
            continue
        if child['at']:
            walk(child, '')
        else:
            walk(child, child['prelude'])

    return {category: items for category, items in findings.items() if items}


# --------------------------------------------------------------- 作用域


def test_plugin_css_is_fully_scoped():
    assert os.path.isfile(CSS_PATH)
    assert scan_css_scope(CSS_PATH, PLUGIN_ID) == []


def test_scan_detects_unscoped_selector(tmp_path):
    """变异测试：注入裸选择器必须能被检出，否则本测试没有意义。"""
    target = tmp_path / 'bad.css'
    target.write_text('.card { border: 1px solid red; }\n', encoding='utf-8')
    assert scan_css_scope(str(target), PLUGIN_ID) == ['.card']


def test_scan_ignores_selectors_inside_grouping_rules(tmp_path):
    target = tmp_path / 'grouped.css'
    target.write_text(
        '@media (max-width: 720px) {\n  .plugin-stm-ai-assistant-tabs { width: 96vw; }\n}\n',
        encoding='utf-8',
    )
    assert scan_css_scope(str(target), PLUGIN_ID) == []


def test_css_avoids_tailwind_palette_classes():
    """F2：颜色只能用语义 CSS 变量，禁止调色板类。"""
    source = _read_css()
    palette = re.compile(r'\b(?:bg|text|border|ring|from|to|via)-(?:red|blue|green|yellow|purple|pink|gray|slate|zinc|neutral|stone|orange|amber|lime|emerald|teal|cyan|sky|indigo|violet|fuchsia|rose)-\d{2,3}\b')
    assert palette.findall(source) == []


def test_css_has_no_hex_color_literals():
    """颜色只用语义变量（现有 CSS 零十六进制字面量，保持）。"""
    source = re.sub(r'/\*.*?\*/', '', _read_css(), flags=re.S)
    assert re.findall(r'#[0-9a-fA-F]{3,8}\b', source) == []


# --------------------------------------------------------------- 声明层


def test_css_has_no_position_fixed():
    """重写核心：插件不再自绘 fixed 浮层（层级/定位归宿主容器）。"""
    findings = scan_css_declarations(CSS_PATH, PLUGIN_ID)
    assert findings.get('position_fixed') is None, findings.get('position_fixed')


def test_css_has_no_self_drawn_overlay_or_scrim():
    """自绘遮罩、模糊、`body:has(...)` 兜底规则都必须消失。"""
    # 注释里会**说明**这些旧模式已删除，因此断言只看声明（去注释后）
    source = _strip_comments(_read_css())
    assert 'plugin-stm-ai-assistant-overlay' not in source
    assert 'plugin-stm-ai-assistant-launcher' not in source
    assert 'plugin-stm-ai-assistant-shell' not in source
    assert 'backdrop-filter' not in source, 'backdrop-filter 归宿主（宿主 surface 刻意不提供模糊）'
    assert 'body:has(' not in source, '`:has()` 兜底规则已随宿主修复删除'
    assert 'z-index: 2140' not in source and 'z-index: 2150' not in source


def test_css_has_no_hardcoded_host_sprite():
    """F15：不得硬编码宿主 sprite 路径（插件自带资源走 assets/ 是允许的）。"""
    findings = scan_css_declarations(CSS_PATH, PLUGIN_ID)
    assert findings.get('hardcoded_sprite') is None, findings.get('hardcoded_sprite')


def test_plugin_brand_icon_is_a_plugin_owned_asset():
    """品牌图标走**插件自己的**资源，且全插件**只有一个**图标（问题 4）。

    用户原话：「既然是同一个插件，应该保持相同的图标更有辨识度，包括右键菜单，
    包括批量处理，入口总览哪里直接调成相同的插件图标即可。」

    实现方式：`entries[].icon` 与 `surfaces.register().icon` 都写
    `frontend/assets/icon.svg`（插件内相对路径）。宿主 `utils/icons.js:resolveIcon`
    三态解析：含 `/` 或 `.` 的值 → `/api/plugins/<id>/assets/<path>`，
    `uiPrimitives.js:icon()` 对资源路径渲染 `<span>` + CSS mask。
    `core/plugins/assets.py` 的 `ALLOWED_EXTENSIONS` 含 `.svg`，该路由可用。

    ⚠️ 这取代了更早的写法：当时 `resolveIconUrl` 只支持宿主图标名，插件只好在
    自己的 CSS 里用 mask 引资源 URL 画品牌图标，而入口图标只能退而用宿主图标名
    （`wand` / `tags` / `folder` / `settings-gear`）——同一个插件在不同落点显示
    不同图标，辨识度差。现在两侧统一走插件资源。
    """
    icon_path = os.path.join(ROOT, 'frontend', 'assets', 'icon.svg')
    assert os.path.isfile(icon_path), '插件品牌图标 frontend/assets/icon.svg 缺失'

    with open(icon_path, 'r', encoding='utf-8') as handle:
        svg = handle.read()
    assert 'currentColor' in svg, '图标必须用 currentColor 才能跟随主题前景色'
    assert '<path' in svg

    # ① manifest 的每个入口都用同一个插件资源路径
    with open(os.path.join(ROOT, 'manifest.json'), 'r', encoding='utf-8') as handle:
        manifest = json.load(handle)
    icons = [entry.get('icon') for entry in manifest['frontend']['entries']]
    assert set(icons) == {'frontend/assets/icon.svg'}, (
        f'所有入口必须用同一个插件图标（含右键/批量/总览）：{icons}'
    )

    # ② surface 注册也用同一个图标（弹层标题行）
    with open(os.path.join(ROOT, 'frontend', 'plugin.js'), 'r', encoding='utf-8') as handle:
        plugin_source = handle.read()
    assert "icon: 'frontend/assets/icon.svg'" in plugin_source

    # ③ CSS 不再需要自己画品牌图标（图标归宿主标题行）
    source = _strip_comments(_read_css())
    assert '/static/icons/' not in source, '插件 CSS 不得引用宿主 sprite'
    assert 'plugin-stm-ai-assistant-glyph' not in source, (
        '品牌图标已归宿主标题行（`surface.icon`），插件 CSS 不该再自绘 glyph'
    )

    findings = scan_css_declarations(CSS_PATH, PLUGIN_ID)
    assert findings.get('hardcoded_sprite') is None, (
        f'插件自有资源 URL 不应被 F15 判为硬编码宿主 sprite: {findings.get("hardcoded_sprite")}'
    )


def test_status_pills_live_in_the_host_header_row():
    """状态胶囊挂在**宿主标题行**，插件不再自绘第二条状态栏（问题 3）。

    用户原话：「当前的第二行应该合并到第一行去，也就是图标应该放到左上角，
    胶囊应该挪到顶部。」

    旧实现：宿主标题行（标题 + ×）+ 插件自绘状态行（品牌图标 + 胶囊）→ 顶部两行。
    新实现：图标归宿主标题行左侧，胶囊走 `spec.headerExtra` 注入的附加槽，
    与标题、关闭按钮**同一行**；插件只保留页签 + 主体。
    """
    with open(os.path.join(ROOT, 'frontend', 'panel.js'), 'r', encoding='utf-8') as handle:
        source = handle.read()

    # 胶囊槽仍存在，且 `refreshStatus` 只清它
    assert 'plugin-stm-ai-assistant-status-badges' in source, '必须有状态胶囊槽'
    assert 'clear(badgeSlot)' in source

    # 自绘状态行已删除
    assert 'plugin-stm-ai-assistant-status-row' not in source, (
        '状态行已合并进宿主标题行，不该再自绘'
    )
    assert 'plugin-stm-ai-assistant-glyph' not in source, (
        '品牌图标归宿主标题行（`surface.icon`），插件不该再自绘'
    )

    # 胶囊通过 mountHeaderExtra 注入宿主标题行的附加槽
    assert 'mountHeaderExtra' in source
    assert 'container.append(badgeSlot)' in source

    with open(os.path.join(ROOT, 'frontend', 'plugin.js'), 'r', encoding='utf-8') as handle:
        plugin_source = handle.read()
    assert 'headerExtra(container) {' in plugin_source
    assert 'panel.mountHeaderExtra(container)' in plugin_source


def test_panel_root_only_has_tabs_and_body():
    """插件根节点只含「页签 + 主体」——标题行与状态胶囊都归宿主容器。"""
    with open(os.path.join(ROOT, 'frontend', 'panel.js'), 'r', encoding='utf-8') as handle:
        source = handle.read()

    assert 'root.append(tabBar, body)' in source, (
        '根节点只应 append 页签与主体'
    )
    assert 'root.append(statusRow' not in source


def test_css_important_only_on_scoped_selectors():
    """F13：`!important` 只允许出现在带作用域的选择器上。"""
    findings = scan_css_declarations(CSS_PATH, PLUGIN_ID)
    assert findings.get('important') is None, findings.get('important')


@pytest.mark.skipif(not os.path.isdir(MAIN_APP_DIR), reason='主程序仓库不可用')
def test_css_variables_are_known_or_have_fallback():
    """F13：`var(--x)` 无回退值时必须在宿主令牌清单内。"""
    known = _resolve_known_tokens()
    findings = scan_css_declarations(CSS_PATH, PLUGIN_ID, known_tokens=known)
    assert findings.get('unknown_tokens') is None, findings.get('unknown_tokens')


def test_css_uses_only_defined_variables():
    """变量必须能在主程序里找到定义（否则样式静默失效，参见附录 K）。"""
    if not os.path.isdir(MAIN_APP_DIR):
        pytest.skip('主程序仓库不可用')

    css_root = os.path.join(MAIN_APP_DIR, 'static', 'css')
    defined = set()
    for dirpath, _dirnames, filenames in os.walk(css_root):
        for filename in filenames:
            if not filename.endswith('.css'):
                continue
            with open(os.path.join(dirpath, filename), 'r', encoding='utf-8', errors='ignore') as handle:
                defined.update(re.findall(r'(--[a-z0-9-]+)\s*:', handle.read()))

    source = _read_css()
    used = set(re.findall(r'var\(\s*(--[a-z0-9-]+)', source))

    # 允许「有 fallback 的变量」缺失：var(--x, fallback) 在未定义时仍能渲染
    with_fallback = set(re.findall(r'var\(\s*(--[a-z0-9-]+)\s*,', source))
    missing = used - defined - with_fallback
    assert missing == set(), f'使用了主程序未定义的 CSS 变量: {sorted(missing)}'


# --------------------------------------------------------------- 视觉契约


def _rule_body(source: str, selector: str) -> str:
    """取出某个选择器的声明块（用于样式契约断言，注释已剔除）。"""
    match = re.search(re.escape(selector) + r'\s*\{([^}]*)\}', _strip_comments(source))
    assert match, f'未找到规则: {selector}'
    return match.group(1)


def test_active_tab_matches_detail_page_contract():
    """回归：选中页签必须与角色卡详情页一致——无边框、无位移、2px 内阴影底条。

    旧实现是「向下突出的胶囊」（999px 圆角 + translateY + 负 margin），与详情页不符。
    """
    source = _read_css()

    active = _rule_body(source, '.plugin-stm-ai-assistant-tab.is-active')
    assert 'transform: none' in active, '选中页签不得有位移'
    assert 'border-color: transparent' in active, '选中页签必须无可见边框'
    assert 'inset 0 -2px 0' in active, '选中页签必须有 2px 内阴影底条'

    tab = _rule_body(source, '.plugin-stm-ai-assistant-tab')
    assert 'border-radius: 0.55rem' in tab, '页签圆角必须是详情页的 0.55rem'
    assert '999px' not in tab, '页签不得使用胶囊圆角'


def test_panel_and_pills_are_in_flow_not_floating():
    """面板与胶囊槽都是容器内的普通流式区块（不再 `position: fixed` / 右对齐浮标）。"""
    source = _read_css()

    panel = _rule_body(source, '.plugin-stm-ai-assistant-panel')
    assert 'position' not in panel, '面板不得自行定位'
    assert 'flex' in panel

    pills = _rule_body(source, '.plugin-stm-ai-assistant-status-badges')
    assert 'position' not in pills, '胶囊槽不得自行定位'
    assert 'inline-flex' in pills


def test_plugin_does_not_style_host_surface_dom():
    """坑 5：`.plugin-surface__*` 是宿主 DOM，插件 CSS 不得试图隐藏/覆盖它。

    注释里会解释「为什么不隐藏宿主的关闭按钮」，所以只检查**选择器**。
    """
    source = _strip_comments(_read_css())
    assert '.plugin-surface' not in source, (
        '宿主 surface 的 DOM 不在插件作用域内，插件 CSS 选不中它，也不该尝试'
    )


# --------------------------------------------------------------- 交叉验证


@pytest.mark.skipif(not os.path.isdir(MAIN_APP_DIR), reason='主程序仓库不可用')
def test_matches_main_app_scanner():
    """与主程序真实实现交叉验证（同一文件、同一结论）。"""
    sys.path.insert(0, MAIN_APP_DIR)
    try:
        from core.plugins.installer import scan_css_scope as real_scan  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover
        pytest.skip(f'无法导入主程序扫描器: {exc}')
    finally:
        sys.path.remove(MAIN_APP_DIR)

    assert real_scan(CSS_PATH, PLUGIN_ID) == []


@pytest.mark.skipif(not os.path.isdir(MAIN_APP_DIR), reason='主程序仓库不可用')
def test_matches_main_app_declaration_scanner():
    """声明层也要与主程序真实实现同结论（空 = 零警告）。"""
    sys.path.insert(0, MAIN_APP_DIR)
    try:
        from core.plugins.installer import (  # noqa: PLC0415
            scan_css_declarations as real_scan,
        )
    except Exception as exc:  # pragma: no cover
        pytest.skip(f'无法导入主程序声明层扫描器: {exc}')
    finally:
        sys.path.remove(MAIN_APP_DIR)

    assert real_scan(CSS_PATH, PLUGIN_ID) == {}


@pytest.mark.skipif(not os.path.isdir(MAIN_APP_DIR), reason='主程序仓库不可用')
def test_replica_matches_main_app_on_mutated_input(tmp_path):
    """变异测试：复刻实现与真实实现对「坏 CSS」必须给出同样结论。"""
    sys.path.insert(0, MAIN_APP_DIR)
    try:
        from core.plugins.installer import (  # noqa: PLC0415
            scan_css_declarations as real_scan,
        )
    except Exception as exc:  # pragma: no cover
        pytest.skip(f'无法导入主程序声明层扫描器: {exc}')
    finally:
        sys.path.remove(MAIN_APP_DIR)

    target = tmp_path / 'bad.css'
    target.write_text(
        '.plugin-stm-ai-assistant-x { position: fixed; z-index: 10; }\n'
        '.plugin-stm-ai-assistant-y { color: var(--nope); }\n'
        '.plugin-stm-ai-assistant-z { background: url(\'/static/icons/ui.svg#icon-x\'); }\n',
        encoding='utf-8',
    )
    replica = scan_css_declarations(str(target), PLUGIN_ID)
    real = real_scan(str(target), PLUGIN_ID)

    assert 'position_fixed' in replica and 'position_fixed' in real
    assert 'unknown_tokens' in replica and 'unknown_tokens' in real
    assert 'hardcoded_sprite' in replica and 'hardcoded_sprite' in real
