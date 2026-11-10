"""CHANGELOG 的版本锚点契约。

宿主「插件管理 → 更新」在覆盖安装前要展示**本次更新内容**：`check_updates()`
拿到目标版本后，会去读插件仓库的 `CHANGELOG.md`，按 `<!-- release: X.Y.Z -->` …
`<!-- /release -->` 锚点切出「已装版本 → 目标版本」之间的增量段落
（宿主侧实现见 `core/plugins/changelog.py`）。

这份文件曾经**完全不可解析**：`## 0.3.0（2026-10-25）` 下面混着
`### 2026-10-24 …` 这种把日期当二级标题的段落，`## P3 可用性修复（2026-10-11…）`
连版本号都不是；更糟的是 PR #3 合入时把新写的段落整段插到了原文档**前面**，
文件里出现过两个 H1（`# 更新日志` 与 `# CHANGELOG`），而 `manifest.version`
已经是 0.4.1、CHANGELOG 顶部却还写着 `## 未发布`——0.4.0 / 0.4.1 的标题根本不存在。

所以这里锁死四件事，任何一条破了都说明宿主的更新说明又会静默变空：

1. 每个版本都有成对锚点，且**锚点包住该版本的标题行**；
2. 版本号单调、不重复（宿主按版本区间切分，重复会让同一段被讲两遍）；
3. `manifest.version` 对应的版本**必须有锚点段落**（否则「更新到当前版本」没说明）；
4. 非版本文档（P3 段）不加锚点——宿主不该把它当成某个版本的更新说明。
"""

import json
import os
import re

from conftest import ROOT

CHANGELOG_PATH = os.path.join(ROOT, 'CHANGELOG.md')
MANIFEST_PATH = os.path.join(ROOT, 'manifest.json')

#: 与宿主 `core/plugins/changelog.py` 的 `RELEASE_ANCHOR_PATTERN` 同一形状。
_ANCHOR_OPEN = re.compile(r'<!--\s*release:\s*([0-9]+\.[0-9]+\.[0-9]+)\s*-->')
_ANCHOR_CLOSE = re.compile(r'<!--\s*/release\s*-->')
_SEMVER = re.compile(r'^[0-9]+\.[0-9]+\.[0-9]+$')


def _read_changelog():
    with open(CHANGELOG_PATH, encoding='utf-8') as handle:
        return handle.read()


def _sections(text):
    """按锚点切出 `[(version, body)]`；不成对的锚点直接丢掉。"""
    sections = []
    cursor = 0
    while True:
        opened = _ANCHOR_OPEN.search(text, cursor)
        if not opened:
            break
        closed = _ANCHOR_CLOSE.search(text, opened.end())
        if not closed:
            break
        sections.append((opened.group(1), text[opened.end():closed.start()]))
        cursor = closed.end()
    return sections


def _manifest_version():
    with open(MANIFEST_PATH, encoding='utf-8') as handle:
        return json.load(handle)['version']


# ------------------------------------------------------------------ 锚点结构


def test_every_anchor_is_paired():
    """开闭锚点数量必须相等（少一个就意味着某段说明被截断或漏掉）。"""
    text = _read_changelog()
    assert len(_ANCHOR_OPEN.findall(text)) == len(_ANCHOR_CLOSE.findall(text))


def test_anchors_appear_in_descending_version_order():
    """新版本在前：宿主切分后按同一顺序拼接，乱序会让「本次更新」读起来跳版本。"""
    versions = [version for version, _body in _sections(_read_changelog())]

    assert versions, 'CHANGELOG.md 里一个版本锚点都没有'
    assert all(_SEMVER.match(version) for version in versions), versions

    parsed = [tuple(int(part) for part in version.split('.')) for version in versions]
    assert parsed == sorted(parsed, reverse=True), f'版本锚点未按从新到旧排列: {versions}'
    assert len(set(versions)) == len(versions), f'版本锚点重复: {versions}'


def test_each_anchor_contains_its_own_version_heading():
    """锚点必须包住 `## X.Y.Z` 标题行，否则前端展示的正文没有版本小标题。"""
    missing = []
    for version, body in _sections(_read_changelog()):
        if not re.search(rf'^##\s+{re.escape(version)}\b', body, re.MULTILINE):
            missing.append(version)

    assert missing == [], f'这些版本的锚点内缺少 `## <版本>` 标题行: {missing}'
    # 每段都得有正文，空段落等于「有锚点但没说明」
    assert all(body.strip() for _version, body in _sections(_read_changelog()))


def test_manifest_version_has_a_release_section():
    """`manifest.version` 必须能在 CHANGELOG 里找到对应段落。

    回归：manifest 已是 0.4.1，CHANGELOG 顶部却还是「未发布」，`## 0.4.1` 标题
    压根不存在——用户从 0.4.0 更新到 0.4.1 时，宿主按区间切出的是空字符串，
    界面上只剩一句版本号。
    """
    version = _manifest_version()
    versions = [item for item, _body in _sections(_read_changelog())]

    assert version in versions, (
        f'manifest.version={version} 在 CHANGELOG.md 里没有锚点段落；'
        f'现有版本: {versions}'
    )


def test_unreleased_section_is_not_anchored():
    """「未发布」不得加锚点：它没有版本号，混进区间切分会被当成某个已发布版本。"""
    text = _read_changelog()

    unreleased = re.search(r'^##\s*未发布\s*$', text, re.MULTILINE)
    assert unreleased is not None, '应保留「## 未发布」段用于累积下一版条目'

    # 「未发布」段必须落在第一个版本锚点**之前**
    first_anchor = _ANCHOR_OPEN.search(text)
    assert first_anchor is not None
    assert unreleased.start() < first_anchor.start(), (
        '「## 未发布」应位于所有版本锚点之前'
    )


def test_non_version_sections_stay_outside_anchors():
    """`## P3 可用性修复（…）` 这类非版本文档不加锚点。

    它的标题里没有版本号，一旦被锚点包住，宿主会把这段可用性修复当成某个版本的
    更新说明塞给用户，而那个「版本」在 manifest 里根本不存在。
    """
    text = _read_changelog()
    sections = _sections(text)

    p3 = text.find('## P3 可用性修复')
    assert p3 != -1, 'P3 段应作为历史记录保留'

    for version, body in sections:
        assert 'P3 可用性修复' not in body, (
            f'P3 可用性修复被误包进了 {version} 的锚点段落'
        )


def test_anchor_markers_are_not_illustrated_with_literal_markers():
    """说明文字里不得再出现锚点字面量。

    本文件顶部有一大段「版本锚点约定」。若在那里举例写出完整锚点字面量，解析器
    会把它当成真锚点：轻则多出一个空版本段（那个「版本号」是 `<版本号>` 占位符
    或某个真实版本），重则把紧随其后的真锚点**配对错位**，后面所有版本说明整体
    偏移一格。因此那一段刻意用 `⌜!-- release: <版本号> --⌟` 这种变体。

    断言方式：注释开标记的总数必须**恰好**等于「配对成功的版本段数 × 2」。
    多出来的那个开标记就是说明文字里的举例（或一处漏了 `/release` 的笔误）。
    """
    text = _read_changelog()
    paired = len(_sections(text))

    open_count = text.count('<!--')
    close_count = text.count('-->')

    assert open_count == paired * 2, (
        f'注释开标记 {open_count} 个，但只有 {paired} 对版本锚点；'
        '多出来的 `<!--` 会被解析器当成真锚点（检查说明文字里是否写了锚点字面量）'
    )
    assert close_count == paired * 2, f'注释闭标记 {close_count} 个，期望 {paired * 2}'
