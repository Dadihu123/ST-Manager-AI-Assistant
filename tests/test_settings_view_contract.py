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
REVIEW_PATH = os.path.join(ROOT, 'frontend', 'views', 'review.js')


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


def test_tag_pool_section_has_help_button_and_category_pickers():
    """标签池：两个输入框 + 标题旁 `?` 说明 + 「点一下就填」的分类按钮。

    用户原话：「在插件配置中做一个输入框，并在输入框外弄一个 ?，告诉我怎么填写，
    我不懂程序」。所以这几样都是**用户可见的契约**，缺一个就等于没交付。
    """
    source = _strip_comments(_read(SETTINGS_PATH))

    assert "'标签池'" in source, '设置页必须有「标签池」区块'
    assert '只取这些分类' in source and '排除这些分类' in source
    assert 'categories_include' in source and 'categories_exclude' in source

    # `?` 帮助：按钮 + 内联说明面板（不能用绝对定位浮层，面板滚动区会裁掉）
    assert 'plugin-stm-ai-assistant-help-btn' in source
    assert 'function helpNote(' in source
    assert "'?'" in source
    # 分类按钮：把名字填进输入框，且两边互斥
    assert 'function moveCategory(' in source
    assert 'saveTagPool(' in source

    assert 'client.tagPool()' in source, '进入设置页要读取当前标签池与分类清单'

    # 「现有标签分类」旁必须提醒：数量含角色卡「隔离区」里的标签，可能与管理器显示不一致
    assert '隔离区' in source, '分类清单旁必须提示「数量包含隔离区标签」'
    assert 'plugin-stm-ai-assistant-tagpool-picker-note' in source


def test_tag_pool_has_per_category_confidence_and_quota():
    """新置信度系统：每个分类可展开设置「置信度 + 设置数量」，外加最大总数量。

    用户要求：全局置信度不要了；每个分类一个阈值、一个数量；
    分类数量之和不能超过最大总数量，保存超限要弹窗拦下。
    """
    source = _strip_comments(_read(SETTINGS_PATH))

    assert '最大总数量' in source, '设置页要有「最大总数量」输入'
    assert '标签池 token 预算' in source, '设置页要有「标签池 token 预算」'
    assert 'budget_tokens: budgetTokens' in source
    assert '标签置信度' in source and '设置数量' in source
    assert 'function formatRuleSummary(' in source, '折叠态要显示规则摘要'
    assert 'tagpool-rule-panel' in source, '分类要能展开规则面板'
    # 保存时把规则与总数量一起发出去
    assert 'max_tags: maxTags' in source
    assert 'rules,' in source
    # 超限：**提醒但仍允许保存**（用户选定），实际执行按总上限截断
    assert 'quotaTotal > maxTags' in source
    assert '仍要保存吗' in source
    assert '按总上限' in source and '只作参考' in source
    # 旧的全局置信度控件不应再出现
    assert '置信度下限' not in source


def test_tag_pool_client_hits_backend_endpoints():
    source = _strip_comments(_read(API_PATH))

    assert re.search(r"tagPool:\s*\(\)\s*=>\s*call\('/tag-pool'\)", source), (
        'tagPool 必须读 GET /tag-pool'
    )
    assert re.search(r"saveTagPool:\s*\(payload\)\s*=>[\s\S]{0,80}call\('/tag-pool'", source), (
        'saveTagPool 必须写 POST /tag-pool'
    )


def test_tag_pool_prunes_orphaned_category_rules():
    """分类被改名/删除后，它的规则必须能被清掉。

    渲染遍历 `categories`、保存遍历 `Object.keys(rules)`：孤儿规则看不见、
    删不掉，却仍会重新 POST 回去并被算进「已分配 N」——超限弹窗永远对不上。
    """
    source = _strip_comments(_read(SETTINGS_PATH))

    assert 'orphanRules' in source, '渲染前必须识别出入已不存在的分类的规则'
    assert re.search(r'knownNames\.has', source), '要有「规则名是否还在分类清单里」的判断'
    assert re.search(r'delete\s+rules\[name\]', source), '孤儿规则要从工作副本里删掉'

    # 守卫：分类清单没加载到时不能把所有规则当孤儿清空
    assert 'categoriesLoaded' in source, (
        '必须区分「分类清单为空」与「分类全被删了」：前者是没加载完，不能清规则'
    )
    assert re.search(r'state\.tagPoolCategories\.length\s*>\s*0', source)

    # 用户要被告知，而不是设置莫名消失
    assert '已失效' in source


def test_merge_result_surfaces_frozen_skips():
    """合并结果必须把「被冻结分类挡下」的标签显示出来。

    合并 = `remove(from)`，等于删标签。跳过冻结项后如果界面仍只说「成功」，
    用户会以为合并做了、实际那几条压根没动。
    """
    source = _strip_comments(_read(REVIEW_PATH))

    assert 'merges_executed' in source, '审批页要读合并执行结果'
    assert 'skipped_frozen' in source, '要把被冻结挡下的标签列出来'
    assert '冻结' in source, '提示要说清原因，不能只报个数'


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
