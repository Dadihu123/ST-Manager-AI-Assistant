"""简介与本地备注的合成策略（`backend/core/note_merge.py`）。

这层是「AI 简介不再覆盖用户备注」的唯一实现点，因此测试重点是**不丢用户内容**
与**反复打标不叠加**这两条硬约束，而不是字符串长相。
"""

import pytest

from conftest import load_backend_submodule

note_merge = load_backend_submodule('core.note_merge')

USER_NOTE = '自己写的一句话：这是女主的师父。'


# ------------------------------------------------------------------ 归一化


def test_default_mode_is_append():
    """默认必须是「不覆盖」的方向 —— 这正是不再冲掉用户备注的前提。"""
    assert note_merge.DEFAULT_NOTE_MODE == 'append'


@pytest.mark.parametrize('value', ['append', 'prepend', 'replace', 'skip'])
def test_normalize_mode_accepts_known_values(value):
    assert note_merge.normalize_mode(value) == value


@pytest.mark.parametrize('value', ['', None, 'APPEND ', ' Append ', 'garbage', 42, {}])
def test_normalize_mode_falls_back(value):
    assert note_merge.normalize_mode(value) == note_merge.DEFAULT_NOTE_MODE


# ------------------------------------------------------------------ 合成


def test_append_keeps_user_note_first():
    out = note_merge.compose(USER_NOTE, 'AI 简介', 'append')
    assert out['text'].startswith(USER_NOTE)
    assert 'AI 简介' in out['text']
    assert out['kept_chars'] == len(USER_NOTE)
    assert out['changed'] is True


def test_prepend_puts_ai_block_first():
    out = note_merge.compose(USER_NOTE, 'AI 简介', 'prepend')
    assert out['text'].endswith(USER_NOTE)
    assert out['text'].startswith(note_merge.NOTE_BLOCK_START)
    assert out['kept_chars'] == len(USER_NOTE)


def test_replace_drops_user_note_and_reports_zero_kept():
    out = note_merge.compose(USER_NOTE, 'AI 简介', 'replace')
    assert USER_NOTE not in out['text']
    assert out['kept_chars'] == 0, '覆盖模式下没有任何用户内容被保留'


def test_skip_returns_original_text():
    out = note_merge.compose(USER_NOTE, 'AI 简介', 'skip')
    assert out['text'] == USER_NOTE
    assert out['changed'] is False
    assert out['skipped'] == 'mode_skip'


def test_empty_summary_is_skipped():
    out = note_merge.compose(USER_NOTE, '   ', 'append')
    assert out['skipped'] == 'empty'
    assert out['changed'] is False
    assert out['text'] == USER_NOTE


def test_empty_existing_note_writes_only_the_block():
    out = note_merge.compose('', 'AI 简介', 'append')
    assert out['text'] == note_merge.build_block('AI 简介')
    assert out['kept_chars'] == 0


# ------------------------------------------------------------------ 不叠加


def test_recompose_replaces_previous_ai_block():
    """反复打标只替换上一段 AI 内容 —— 这是 append/prepend 可用的前提。"""
    first = note_merge.compose(USER_NOTE, '第一版简介', 'append')['text']
    second = note_merge.compose(first, '第二版简介', 'append')['text']

    assert '第一版简介' not in second
    assert '第二版简介' in second
    assert second.count(USER_NOTE) == 1
    assert second.count(note_merge.NOTE_BLOCK_START) == 1

    third = note_merge.compose(second, '第三版简介', 'prepend')['text']
    assert '第二版简介' not in third, '换策略时也要先剥掉上一段'
    assert third.count(USER_NOTE) == 1
    assert third.count(note_merge.NOTE_BLOCK_START) == 1


def test_same_summary_twice_is_unchanged():
    first = note_merge.compose(USER_NOTE, '同一段简介', 'append')['text']
    again = note_merge.compose(first, '同一段简介', 'append')
    assert again['changed'] is False, '内容没变就不该再写一次盘'


def test_user_edits_around_the_block_survive():
    """用户在 AI 段前后各加了一句，两句都必须保住。"""
    first = note_merge.compose('原备注', '简介', 'append')['text']
    edited = f'顶部补充\n\n{first}\n底部补充'
    out = note_merge.compose(edited, '新简介', 'append')['text']

    assert '顶部补充' in out
    assert '底部补充' in out
    assert '原备注' in out
    assert '新简介' in out


def test_strip_block_preserves_unmarked_details_with_ai_title():
    """无插件标记时无法可靠判断归属，不能删除用户自写的同名块。"""
    messy = (
        f'{USER_NOTE}\n\n'
        f'<details>\n<summary>{note_merge.NOTE_TITLE}</summary>\n\n旧简介\n\n</details>'
    )
    assert note_merge.strip_block(messy) == messy


def test_strip_block_preserves_marker_pair_in_plain_user_text():
    """普通正文里的示例 marker 不是插件折叠块，不能误删。"""
    text = (
        f'{USER_NOTE}\n示例开始：{note_merge.NOTE_BLOCK_START}\n'
        f'这是正文中的格式示例。{note_merge.NOTE_BLOCK_END}\n示例结束。'
    )
    assert note_merge.strip_block(text) == text


def test_summary_with_markers_stays_visible_and_recompose_is_idempotent():
    """简介自身提及 marker 时仍可显示，且不破坏外层区块识别。"""
    quoted_markers = f'引用 {note_merge.NOTE_BLOCK_START} 和 {note_merge.NOTE_BLOCK_END}'
    first = note_merge.compose(USER_NOTE, f'第一版：{quoted_markers}', 'append')['text']

    assert f'&lt;!-- stm-ai-assistant:summary:start --&gt;' in first
    assert f'&lt;!-- stm-ai-assistant:summary:end --&gt;' in first
    assert first.count(note_merge.NOTE_BLOCK_START) == 1
    assert first.count(note_merge.NOTE_BLOCK_END) == 1

    second = note_merge.compose(first, f'第二版：{quoted_markers}', 'append')['text']
    assert '第一版' not in second
    assert '第二版' in second
    assert second.count(USER_NOTE) == 1
    assert second.count(note_merge.NOTE_BLOCK_START) == 1
    assert second.count(note_merge.NOTE_BLOCK_END) == 1
    assert note_merge.strip_block(second) == USER_NOTE


def test_append_preserves_unmarked_details_with_ai_title():
    existing = (
        f'{USER_NOTE}\n\n'
        f'<details>\n<summary>{note_merge.NOTE_TITLE}</summary>\n\n用户写的内容\n\n</details>'
    )
    out = note_merge.compose(existing, '新简介', 'append')['text']

    assert '用户写的内容' in out
    assert out.count(f'<summary>{note_merge.NOTE_TITLE}</summary>') == 2
    assert out.count(note_merge.NOTE_BLOCK_START) == 1
    assert '新简介' in out


def test_strip_block_leaves_plain_note_untouched():
    assert note_merge.strip_block(USER_NOTE) == USER_NOTE
    assert note_merge.strip_block('') == ''
    assert note_merge.strip_block(None) == ''


def test_strip_block_does_not_eat_unrelated_details():
    """别的 `<details>`（用户自己写的折叠块）不能被当成 AI 段剥掉。"""
    text = '<details>\n<summary>我的设定</summary>\n\n别的内容\n\n</details>'
    assert note_merge.strip_block(text) == text


# ------------------------------------------------------------------ 格式


def test_block_is_markdown_and_collapsed():
    """AI 段要能被 Markdown 渲染，且默认折叠（不干扰阅读用户备注）。"""
    block = note_merge.build_block('简介正文')
    assert note_merge.NOTE_BLOCK_START in block
    assert note_merge.NOTE_BLOCK_END in block
    assert '<details>' in block and '</details>' in block
    assert f'<summary>{note_merge.NOTE_TITLE}</summary>' in block
    # 标记是 HTML 注释：渲染后不可见
    assert block.strip().startswith('<!--')
    assert block.strip().endswith('-->')


def test_block_markers_are_stable_constants():
    """标记写进了用户数据，改动它等于让老数据认不出来。"""
    assert note_merge.NOTE_BLOCK_START == '<!-- stm-ai-assistant:summary:start -->'
    assert note_merge.NOTE_BLOCK_END == '<!-- stm-ai-assistant:summary:end -->'
