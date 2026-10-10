"""CSS 作用域契约（F11）。

`core/plugins/installer.py:517-591` 是**字符串包含**判定：顶层选择器里必须出现
`.plugin-stm-ai-assistant` 或 `[data-plugin="stm-ai-assistant"]`。
这里复刻该算法做静态自检，并在主程序可用时用真实实现交叉验证。
"""

import os
import re
import sys

import pytest

from conftest import MAIN_APP_DIR, ROOT

PLUGIN_ID = 'stm-ai-assistant'
CSS_PATH = os.path.join(ROOT, 'frontend', 'plugin.css')

SKIP_HEADS = ('@media', '@supports', '@layer', '@container', '@keyframes', '@font-face')


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
        '@media (max-width: 720px) {\n  .plugin-stm-ai-assistant-shell { width: 96vw; }\n}\n',
        encoding='utf-8',
    )
    assert scan_css_scope(str(target), PLUGIN_ID) == []


def test_css_avoids_tailwind_palette_classes():
    """F2：颜色只能用语义 CSS 变量，禁止调色板类。"""
    with open(CSS_PATH, 'r', encoding='utf-8') as handle:
        source = handle.read()
    palette = re.compile(r'\b(?:bg|text|border|ring|from|to|via)-(?:red|blue|green|yellow|purple|pink|gray|slate|zinc|neutral|stone|orange|amber|lime|emerald|teal|cyan|sky|indigo|violet|fuchsia|rose)-\d{2,3}\b')
    assert palette.findall(source) == []


def _strip_comments(source: str) -> str:
    return re.sub(r'/\*.*?\*/', '', source, flags=re.S)


def _rule_body(source: str, selector: str) -> str:
    """取出某个选择器的声明块（用于样式契约断言，注释已剔除）。"""
    match = re.search(re.escape(selector) + r'\s*\{([^}]*)\}', _strip_comments(source))
    assert match, f'未找到规则: {selector}'
    return match.group(1)


def test_overlay_scrim_uses_semantic_token_and_backdrop_filter():
    """回归：全屏 scrim 不能用裸 rgba(0,0,0,...)。

    裸半透明黑在面板内出现高内容滚动容器时（「提示词档案」页签）会被 Chromium
    合成到黑色 backdrop root，整屏塌成纯黑。必须用语义 scrim token +
    backdrop-filter（后者强制建立 backdrop root）。对齐主程序 .modal-overlay。
    """
    with open(CSS_PATH, 'r', encoding='utf-8') as handle:
        source = handle.read()

    body = _rule_body(source, '.plugin-stm-ai-assistant-overlay')
    assert 'var(--surface-scrim' in body, 'scrim 必须使用 --surface-scrim 语义变量'
    assert 'rgba(0, 0, 0,' not in body, 'scrim 不得使用裸 rgba(0,0,0,...)'
    assert 'backdrop-filter: blur(' in body, 'scrim 必须有 backdrop-filter 以建立 backdrop root'


def test_active_tab_matches_detail_page_contract():
    """回归：选中页签必须与角色卡详情页一致——无边框、无位移、2px 内阴影底条。

    旧实现是「向下突出的胶囊」（999px 圆角 + translateY + 负 margin），与详情页不符。
    """
    with open(CSS_PATH, 'r', encoding='utf-8') as handle:
        source = handle.read()

    active = _rule_body(source, '.plugin-stm-ai-assistant-tab.is-active')
    assert 'transform: none' in active, '选中页签不得有位移'
    assert 'border-color: transparent' in active, '选中页签必须无可见边框'
    assert 'inset 0 -2px 0' in active, '选中页签必须有 2px 内阴影底条'

    tab = _rule_body(source, '.plugin-stm-ai-assistant-tab')
    assert 'border-radius: 0.55rem' in tab, '页签圆角必须是详情页的 0.55rem'
    assert '999px' not in tab, '页签不得使用胶囊圆角'


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

    with open(CSS_PATH, 'r', encoding='utf-8') as handle:
        source = handle.read()
    used = set(re.findall(r'var\(\s*(--[a-z0-9-]+)', source))

    # 允许「有 fallback 的变量」缺失：var(--x, fallback) 在未定义时仍能渲染
    with_fallback = set(re.findall(r'var\(\s*(--[a-z0-9-]+)\s*,', source))
    missing = used - defined - with_fallback
    assert missing == set(), f'使用了主程序未定义的 CSS 变量: {sorted(missing)}'


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
