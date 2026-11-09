"""前端清除入口与写失败提示的静态契约。

后端端点有了（`DELETE /jobs/<run_id>`、`/jobs/clear`、`/usage/clear`、
`/pending/clear`），但用户要的是**能点到**的按钮。这类东西只在浏览器里显形，
所以用静态契约钉住：

1. `api.js` 暴露对应方法（否则视图层无从调用）；
2. hub 的「最近作业」有清除入口、待审批有清空入口；
3. 设置页用量区有「清空用量明细」；
4. `/status` 报告 ui_data 写入失败时，界面上要有提示（不要让用户对着不动的
   界面猜）。
"""

import os
import re

from conftest import ROOT

API_PATH = os.path.join(ROOT, 'frontend', 'api.js')
HUB_PATH = os.path.join(ROOT, 'frontend', 'views', 'hub.js')
SETTINGS_PATH = os.path.join(ROOT, 'frontend', 'views', 'settings.js')
SETTINGS_FRONTEND_TEST = os.path.join(ROOT, 'tests_frontend', 'smoke.mjs')


def _read(path: str) -> str:
    with open(path, 'r', encoding='utf-8') as handle:
        return handle.read()


def _strip_comments(source: str) -> str:
    without_block = re.sub(r'/\*.*?\*/', '', source, flags=re.S)
    return re.sub(r'^\s*//.*$', '', without_block, flags=re.MULTILINE)


# ------------------------------------------------------------------ api.js


def test_api_client_exposes_clear_methods():
    source = _strip_comments(_read(API_PATH))

    assert 'forgetJob:' in source, 'api.js 需要 forgetJob(runId)（DELETE /jobs/<id>）'
    assert 'clearJobs:' in source, 'api.js 需要 clearJobs()（POST /jobs/clear）'
    assert 'clearUsage:' in source, 'api.js 需要 clearUsage()（POST /usage/clear）'
    assert 'clearPending:' in source, 'api.js 需要 clearPending()（POST /pending/clear）'

    assert "'/jobs/clear'" in source, 'clearJobs 要打 /jobs/clear'
    assert "'/usage/clear'" in source, 'clearUsage 要打 /usage/clear'
    assert "'/pending/clear'" in source, 'clearPending 要打 /pending/clear'


def test_forget_job_uses_delete_verb_and_run_id():
    source = _strip_comments(_read(API_PATH))
    match = re.search(r'forgetJob:\s*\(([^)]*)\)\s*=>([\s\S]{0,200})', source)
    assert match, '找不到 forgetJob 的定义'
    body = match.group(2)
    assert 'DELETE' in body, 'forgetJob 必须用 DELETE 动词'
    assert 'encodeURIComponent' in body, 'run_id 必须编码后再拼进 URL'


# ------------------------------------------------------------------ hub


def test_hub_has_clear_entry_for_recent_jobs():
    source = _strip_comments(_read(HUB_PATH))
    assert 'clearJobs' in source, (
        '「最近作业」标题旁需要「清除」按钮，调用 client.clearJobs()；'
        '用户原话：希望能手动清除最近作业记录'
    )
    assert 'confirm' in source, '清除是破坏性操作，必须二次确认'


def test_hub_has_clear_entry_for_pending_reviews():
    source = _strip_comments(_read(HUB_PATH))
    assert 'clearPending' in source, (
        '待审批卡需要「清空待审批」入口：宿主清理终态作业后，那批 pending 再也'
        '不会有 apply，只能手动清'
    )


def test_hub_deletes_single_run():
    source = _strip_comments(_read(HUB_PATH))
    assert 'forgetJob' in source, '「最近作业」的每一行应能单条删除'


def test_hub_surfaces_ui_data_write_failure():
    """`/status` 报告写入失败时，hub 要能提示（而不是静默）。"""
    source = _strip_comments(_read(HUB_PATH))
    assert 'ui_data' in source, (
        'hub 读 /status 的 ui_data 字段，写入失败时给出提示：'
        '否则超配额后用户只看到「界面不动了」'
    )


# ------------------------------------------------------------------ settings


def test_settings_usage_section_has_clear_button():
    source = _strip_comments(_read(SETTINGS_PATH))
    assert 'clearUsage' in source, '设置页「用量台账」区需要「清空用量明细」按钮'
    # 两条路径都要有：只清明细（保留累计）与连累计一起归零
    assert 'clearUsage(true)' in source, (
        '需要「连同累计一起归零」的路径（clearUsage(true)），否则用户清不动累计账'
    )
    assert 'clearUsage(false)' in source, (
        '需要「只清明细」的路径（clearUsage(false)），默认不应抹掉累计用量'
    )


def test_settings_shows_ui_data_health():
    source = _strip_comments(_read(SETTINGS_PATH))
    assert 'ui_data' in source, (
        '设置页概览要显示本地记账是否可写：这是用户判断「要不要清理历史」的唯一线索'
    )


# ------------------------------------------------------------------ 前端冒烟


def test_frontend_smoke_covers_clear_entries():
    """冒烟测试要覆盖清除入口（前端的唯一自动防线）。

    冒烟里点的是按钮、断言的是实际发出的请求路径 —— 所以这里查路径，不查方法名。
    """
    source = _read(SETTINGS_FRONTEND_TEST)
    for path in ("'/jobs/clear'", "'/pending/clear'", "'/usage/clear'"):
        assert path in source, f'tests_frontend/smoke.mjs 需要覆盖 {path} 的点击链路'
    assert "'/jobs/abcdef123456'" in source, (
        '冒烟需要覆盖「单条删除」发出的 DELETE /jobs/<run_id>'
    )
