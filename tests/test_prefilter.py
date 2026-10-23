"""前置过滤（`core/prefilter.py`）：数据/代码类条目不送 LLM（§7）。

**口径的来历**（2,543 条真实条目的离线校准，见
`tmp/e2e-2026-10-22/g6-prefilter.json`）：

| 口径 | 保护条数 |
|---|---|
| 任意 `{{...}}` 都算信号 | 1,306（51.4%） |
| 只算**结构性宏**（现行） | 611（24.0%） |

差的 695 条是「散文里只有一个 `{{user}}`」的普通条目——它们既不是数据也不是代码。
而 §12.3 实测被改坏的 7 条靠名字/结构命中，与宏无关。纯文本宏由 `wicheck`
的「宏保真」硬闸兜底（一个宏丢了就 block），所以这层不必再挡。

注意：下面多行正文一律用三引号写真实换行，避免 `\n` 转义在源码里失真。
"""

import pytest

from stm_ai_core import prefilter


def entry(name, content):
    return {'comment': name, 'content': content}


#: 三键 + 嵌套（MVU 变量树最常见的形状）→ 结构化
NESTED_TREE = """好感度:
  初始: 0
  上限: 100
体力:
  初始: 100
信任度:
  初始: 0"""

#: 三个扁平键 → 不算结构化（§7 的阈值是「大或嵌套」）
FLAT_THREE = """甲: 1
乙: 2
丙: 3"""

#: 六个扁平键 → 算（≥6）
FLAT_SIX = """甲: 1
乙: 2
丙: 3
丁: 4
戊: 5
己: 6"""

#: 三段 INI / 状态栏模板
INI_THREE = """[状态]
体力 100
[道具]
伤药 x3
[结局]
未触发"""

#: 两段：不够
INI_TWO = """[状态]
体力 100
[道具]
伤药 x3"""


# ------------------------------------------------------------------ 不挡（正常散文）


@pytest.mark.parametrize('content', [
    '小遥是丰缘地区的劲敌，喜欢吃东西。',
    '若{{user}}在丰缘早期相遇，她只会骑自行车。',          # 纯文本替换宏
    '{{char}}与{{user}}的关系是竞争。',
    '这是一段很长的设定：' + '细节。' * 60,
    '<小遥>\n版本: 丰缘动画版\n</小遥>',                    # 自定义 XML 标签，不是布局 HTML
    '{{random::甲::乙}} 出现时，天气变化。',                # 随机宏不改程序状态
    '{{// 这条注释可以删}} 正文继续。',                     # ST 注释，删掉是好事
])
def test_ordinary_prose_is_not_protected(content):
    assert prefilter.skip_reason(entry('普通条目', content)) == ''


def test_custom_xml_tag_is_not_layout_html():
    """`<小遥>`/`<analysis>` 这类自定义标签不算布局 HTML（不能整条不送）。"""
    assert prefilter.skip_reason(entry('角色', '<analysis>主线</analysis>')) == ''


# ------------------------------------------------------------------ 挡（数据 / 代码）


@pytest.mark.parametrize('name,content,expected', [
    ('[MVU] InitVar', '变量: 0', 'protected:name'),
    ('[核心] 中央统合系统', '设定。', 'protected:name'),
    ('[initvar]', '某某树', 'protected:name'),
    ('随便', NESTED_TREE, 'protected:structured'),
    ('随便', INI_THREE, 'protected:ini'),
    ('随便', '好感: {{getvar::好感度}}', 'protected:var_func'),
    ('随便', '{{setglobalvar::旗标::1}}', 'protected:var_func'),
    ('随便', '{{format_message_variable::stat_data}}', 'protected:var_func'),
    ('随便', '<% if (x) { %>正文<% } %>', 'protected:ejs'),
    ('随便', '<script>window.x = 1</script>', 'protected:script'),
    ('随便', '<details><summary>标题</summary>正文</details>', 'protected:html'),
    ('随便', '/setvar 好感 1', 'protected:stscript'),
    ('随便', 'def foo():\n    return 1', 'protected:code_py'),
    ('随便', 'const x = 1;\nconsole.log(x)', 'protected:code_js'),
])
def test_data_or_code_is_protected(name, content, expected):
    assert prefilter.skip_reason(entry(name, content)) == expected


def test_ini_needs_three_sections():
    """两段不算：状态栏模板通常 3 段以上，2 段更可能是正文里的小标题。"""
    assert prefilter.skip_reason(entry('x', INI_TWO)) == ''


def test_flat_mapping_is_not_structured():
    assert prefilter.is_structured(FLAT_THREE) is False
    assert prefilter.is_structured(NESTED_TREE) is True


def test_six_flat_keys_are_structured():
    assert prefilter.is_structured(FLAT_SIX) is True


def test_plain_text_macro_is_not_a_structural_macro():
    """`{{user}}`/`{{char}}` 只是文本替换：不进本层，交给检查器的宏闸。"""
    assert 'protected' not in prefilter.skip_reason(
        entry('角色', '{{char}}说：{{user}}你好。'),
    )


def test_empty_and_odd_entries_are_safe():
    assert prefilter.skip_reason(None) == ''
    assert prefilter.skip_reason({}) == ''
    assert prefilter.skip_reason({'content': ''}) == ''
    assert prefilter.skip_reason({'name': 'x', 'content': None}) == ''


def test_describe_gives_human_label():
    assert '名字' in prefilter.describe('protected:name')
    assert '结构化' in prefilter.describe('protected:structured')
    # 未知原因原样返回，不抛错
    assert prefilter.describe('protected:whatever') == 'protected:whatever'
