"""简介与本地备注的合成策略（纯函数层）。

**为什么需要这一层**：主程序的「本地备注」（`ui_data[<ui_key>].summary`，UI 上
叫「概览」，见 `templates/components/grid_cards.html` 的 `title="本地备注"`）是
**用户自己写的地方**。插件此前只有一个动作 `write_card_note`：拿到 AI 简介就
整字段覆盖 —— 用户手写的备注会被一次打标直接冲掉，且没有任何提示。

这里把「写哪儿」变成四种显式策略，并负责**只替换上一段 AI 内容**：

| mode      | 结果                                                       |
|-----------|------------------------------------------------------------|
| `append`  | 本地备注在前，AI 段在后（默认）                            |
| `prepend` | AI 段在前，本地备注在后                                    |
| `replace` | 只留 AI 段（旧行为，用户显式选择时才发生）                 |
| `skip`    | 不写备注（AI 简介仍留在插件里可查）                        |

「只替换上一段 AI 内容」的判据是**分隔区标记**：AI 段总是被
`NOTE_BLOCK_START` / `NOTE_BLOCK_END` 包起来，重复打标时先把这个区间剥掉再
拼新的，因此反复打标不会叠加出十份简介。用户在区间外写的任何内容都原样保住。

分隔格式刻意用 HTML 注释 + `<details>`：注释在渲染后不可见（不干扰阅读），
`<details>` 让 AI 段默认折叠。主程序点击本地备注图标后的预览按 Markdown 渲染；
卡片翻面为了性能使用纯文本，那里会显示原始标记。
"""

import re

from .hashing import normalize_text

#: 四种策略（顺序即 UI 下拉框顺序）。
NOTE_MODES = ('append', 'prepend', 'replace', 'skip')

#: 默认策略：**不覆盖用户备注**。
#:
#: 选 `append` 而不是 `replace`：用户反馈的原始问题就是「AI 简介直接覆盖本地
#: 备注」。默认必须是安全的那个方向，否则升级后老用户仍会被静默覆盖一次。
DEFAULT_NOTE_MODE = 'append'

#: AI 段标题（折叠块摘要行）。
NOTE_TITLE = 'AI 简介'

NOTE_BLOCK_START = '<!-- stm-ai-assistant:summary:start -->'
NOTE_BLOCK_END = '<!-- stm-ai-assistant:summary:end -->'

#: 只匹配插件写入的完整折叠块，避免删除用户正文里的 marker pair。
_BLOCK_PATTERN = re.compile(
    re.escape(NOTE_BLOCK_START)
    + r'\n<details>\n<summary>'
    + re.escape(NOTE_TITLE)
    + r'</summary>\n\n.*?\n\n</details>\n'
    + re.escape(NOTE_BLOCK_END),
    re.DOTALL,
)

def normalize_mode(value) -> str:
    """把任意值收敛为合法策略（坏值回落默认）。"""
    text = normalize_text(value).strip().lower()
    return text if text in NOTE_MODES else DEFAULT_NOTE_MODE


def build_block(summary) -> str:
    """把 AI 简介包成一个带标记的折叠块（Markdown + HTML 混排）。"""
    text = str(summary or '').strip()
    if not text:
        return ''
    # marker 是外层 block 的结构边界；以 HTML entity 显示 summary 自带的 marker，
    # 避免它们被误当成外层边界，同时浏览器仍会显示原样文本。
    text = text.replace(NOTE_BLOCK_START, '&lt;!-- stm-ai-assistant:summary:start --&gt;')
    text = text.replace(NOTE_BLOCK_END, '&lt;!-- stm-ai-assistant:summary:end --&gt;')
    return (
        f'{NOTE_BLOCK_START}\n'
        f'<details>\n'
        f'<summary>{NOTE_TITLE}</summary>\n\n'
        f'{text}\n\n'
        f'</details>\n'
        f'{NOTE_BLOCK_END}'
    )


def strip_block(text) -> str:
    """剥掉文本里的 AI 段，只留用户自己写的内容。

    只剥带有插件分隔标记、标题和完整 `<details>` 结构的块。普通正文中只要
    marker 成对出现但不是完整结构，就保留原文。
    """
    source = str(text or '')
    if not source:
        return ''
    cleaned = _BLOCK_PATTERN.sub('', source)
    # 剥掉区间后可能留下连续空行（原区间前后各有一个换行）
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()


def compose(existing, summary, mode) -> dict:
    """按策略合成新备注。

    Args:
        existing: 主程序当前的本地备注全文（可能为空）。
        summary: AI 生成的简介（可能为空）。
        mode: `NOTE_MODES` 之一。

    Returns:
        `{'mode', 'text', 'kept_chars', 'summary_chars', 'changed', 'skipped'}`。

        - `text`: 应该写进 `api.cards.write_note()` 的**全文**。
        - `kept_chars`: 保住的用户内容字符数（UI 用它说明「备注没丢」）。
        - `changed`: 与 `existing` 相比是否真的需要写盘（没变就别写，
          免得白刷宿主缓存、白触发一次 `ui_data` 整文件重写）。
        - `skipped`: 跳过原因（空串表示没有跳过）。
    """
    current = str(existing or '')
    normalized = normalize_mode(mode)
    text = str(summary or '').strip()

    kept = strip_block(current)
    result = {
        'mode': normalized,
        'text': current,
        'kept_chars': len(kept) if normalized != 'replace' else 0,
        'summary_chars': len(text),
        'changed': False,
        'skipped': '',
    }

    if normalized == 'skip':
        result['skipped'] = 'mode_skip'
        result['kept_chars'] = 0
        return result
    if not text:
        result['skipped'] = 'empty'
        return result

    block = build_block(text)

    if normalized == 'replace':
        composed = block
    elif normalized == 'prepend':
        composed = f'{block}\n\n{kept}' if kept else block
    else:  # append
        composed = f'{kept}\n\n{block}' if kept else block

    composed = composed.strip()
    result['text'] = composed
    result['changed'] = composed != current.strip()
    return result
