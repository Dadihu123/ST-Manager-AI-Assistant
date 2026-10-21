"""前端冒烟测试的 pytest 包装。

真正的用例写在 `tests_frontend/smoke.mjs` 里（最小 DOM 桩 + 假插件 API，
真实执行 `activate()`、各面板首屏渲染、作用域解析与提交参数映射）。
这里只负责在 `pytest` 里执行它，让前后端测试有统一入口。

跳过条件：没有 node，或前端目录缺失。
"""

import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'tests_frontend', 'smoke.mjs')


@pytest.mark.skipif(not os.path.isfile(SCRIPT), reason='前端冒烟脚本缺失')
def test_frontend_smoke():
    node = shutil.which('node')
    if not node:
        pytest.skip('环境里没有 node')

    completed = subprocess.run(
        [node, SCRIPT],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=120,
    )
    output = f'{completed.stdout}\n{completed.stderr}'.strip()
    assert completed.returncode == 0, output
    assert '通过' in output, output
