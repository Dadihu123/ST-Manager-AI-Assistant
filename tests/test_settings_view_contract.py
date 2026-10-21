"""设置页前端契约：模型候选下拉与「未保存也能拉取模型」的数据通路。

这两条都是用户直接报过的问题，而且都**只在浏览器里**才会显形（单元测试不渲染 DOM），
因此用静态契约把它们钉住：

1. **模型候选必须和其他下拉框是同一个控件**。此前它写成
   `<select class="plugin-stm-ai-assistant-select">`——这个类名其实是自定义下拉
   `div` wrapper 的类名，原生 select 套上它只拿到「透明底 + 无边框」，
   拿不到 trigger 那套外观，和同一张表单里的「接口协议」并排时明显不是一个控件。
   正确做法是 `createUi().select()`（`dom.js:styledSelect`）。

2. **拉取模型列表不允许先保存**。用户原话：「我输入了 API 地址和 API key 后就应该允许
   去拉取模型列表，但是当前不行，必须得先保存而后再重新打开这个 provider」。
   因此前端必须把表单里的 `api_key`（明文、仅本次请求）和 `provider_id` 一起发给
   草稿型端点 `/models`，而不是只发 `/providers/<id>/models` 让后端去读已落盘密钥。
"""

import os
import re

from conftest import ROOT

SETTINGS_PATH = os.path.join(ROOT, 'frontend', 'views', 'settings.js')
API_PATH = os.path.join(ROOT, 'frontend', 'api.js')


def _read(path: str) -> str:
    with open(path, 'r', encoding='utf-8') as handle:
        return handle.read()


def _strip_comments(source: str) -> str:
    without_block = re.sub(r'/\*.*?\*/', '', source, flags=re.S)
    return re.sub(r'^\s*//.*$', '', without_block, flags=re.MULTILINE)


def test_model_candidates_use_the_shared_styled_select():
    """模型候选不得再用原生 `<select>` 冒充自定义下拉。"""
    source = _strip_comments(_read(SETTINGS_PATH))

    assert "el('select'" not in source, (
        "settings.js 里出现了原生 el('select' …)：模型候选必须用 createUi().select()"
        "（styledSelect），否则外观与「接口协议」等下拉框不一致"
    )
    assert 'plugin-stm-ai-assistant-select-source' not in source, (
        '模型候选不该手搓 styledSelect 的隐藏 source；用 createUi().select()'
    )
    # 模型候选由 createUi 的 select 构造，并且能接收动态候选（setOptions）
    assert 'const modelSelect = select(' in source, '模型候选必须用 styledSelect'
    assert 'modelSelect.setOptions(' in source, '拉取完成后要用 setOptions 填充候选'


def test_model_row_field_spans_full_grid_row():
    """「模型候选」是「控件 + 按钮」并排的长字段，必须跨满栅格行。"""
    source = _strip_comments(_read(SETTINGS_PATH))

    assert 'plugin-stm-ai-assistant-field--wide' in source, (
        '模型候选字段需要 --wide 类跨满整行，否则下拉被压成半列、按钮换行'
    )


def test_fetch_models_posts_draft_provider_and_inline_api_key():
    """拉模型必须带「未保存的草稿」：base_url + 表单里的 api_key + provider_id。

    这是「必须保存后才能拉取」的根因修复点：不要退回只发 base_url 的旧写法。
    """
    source = _strip_comments(_read(SETTINGS_PATH))

    match = re.search(r'async function fetchModels\([^)]*\)\s*\{([\s\S]*?)\n  \}', source)
    assert match, '未找到 fetchModels 函数'
    body = match.group(1)

    assert 'client.listModels({' in body, 'fetchModels 必须调草稿型 listModels(payload)'
    for key in ('base_url', 'api_style', 'api_key', 'provider_id'):
        assert re.search(rf'\b{key}:', body), f'拉取模型的请求体缺少 {key}'
    assert 'fields.api_key.value' in body, '必须把表单里刚输入的 Key 一起发出去'

    # 不再用「先要 provider id」的旧闸门（用户填了地址+Key 就该能拉）
    assert '先填 provider id' not in body, (
        '不要再以 provider id 为拉取前提：未保存的 provider 也允许拉取'
    )
    assert '/providers/' not in body, '新路径是 POST /models（草稿型），不是 /providers/<id>/models'


def test_list_models_client_hits_the_draft_endpoint():
    """`api.js` 的 listModels 指向草稿型 `/models`。"""
    source = _strip_comments(_read(API_PATH))

    assert re.search(r"listModels:\s*\(payload\)\s*=>\s*call\('/models'", source), (
        "listModels 必须是草稿型 POST /models，接收完整 payload"
    )


def test_styled_select_fits_inside_the_scroll_container():
    """下拉菜单必须按滚动区可用空间翻向/钳高，否则会被面板滚动区裁掉。

    弹层里插件自建了 `overflow-y: auto` 的主体滚动区，而 `.plugin-stm-ai-assistant-select-menu`
    是 `position: absolute`：不处理的话，设置页下半部分的下拉一打开就只剩两三行可见
    （实测菜单 16rem 高、滚动区只剩 165px 可见）。
    """
    dom_source = _strip_comments(_read(os.path.join(ROOT, 'frontend', 'dom.js')))
    css_source = _strip_comments(_read(os.path.join(ROOT, 'frontend', 'plugin.css')))

    assert 'function fitMenu(' in dom_source, 'styledSelect 打开时必须按可用空间布局菜单'
    assert "classList.add('is-dropup')" in dom_source, '空间不足时应朝上展开'
    assert 'menu.style.maxHeight' in dom_source, '菜单高度必须钳到可用空间内'
    assert 'function scrollParent(' in dom_source, '需要找到最近的滚动祖先来算可用空间'

    assert '.plugin-stm-ai-assistant-select.is-dropup' in css_source, (
        '朝上展开的样式必须落在插件作用域内'
    )
    assert 'bottom: calc(100% + 0.35rem)' in css_source, '朝上展开时贴住触发器上沿'
    # 定位仍归 absolute（宿主管 fixed/层级）
    assert 'position: fixed' not in dom_source
