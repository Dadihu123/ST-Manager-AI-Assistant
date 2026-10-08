"""token 估算：公式一致性、边界与成本换算。"""

import math
import os
import sys

import pytest

from conftest import MAIN_APP_DIR
from stm_ai_core import tokens


@pytest.mark.parametrize('text,expected', [
    ('', 0),
    (None, 0),
    ('你好世界', 4),
    ('abcd', 2),
    ('a' * 7, 2),
    ('你好abc', 3),
])
def test_estimate_basic(text, expected):
    assert tokens.estimate(text) == expected


def test_estimate_uses_cjk_plus_ratio_formula():
    sample = '角色卡说明：Alice is a knight. 战斗 12 次。'
    cjk = len([char for char in sample if '\u4e00' <= char <= '\u9fa5'])
    assert tokens.estimate(sample) == math.ceil(cjk + (len(sample) - cjk) / 3.5)


def test_estimate_messages_summarizes_by_role():
    messages = [
        {'role': 'system', 'content': '你是助手'},
        {'role': 'user', 'content': '你好世界'},
        {'role': 'assistant', 'content': 'ok'},
    ]
    result = tokens.estimate_messages(messages)

    assert result['by_role']['system'] == 4
    assert result['by_role']['user'] == 4
    assert result['by_role']['assistant'] == 1
    assert result['by_index'] == [4, 4, 1]
    assert result['total'] == 9


def test_estimate_messages_tolerates_non_dict_items():
    result = tokens.estimate_messages(['noise', None, {'content': 'abcd'}])
    assert result['by_index'] == [0, 0, 2]
    assert result['total'] == 2


def test_estimate_cost_marks_unpriced_when_no_price():
    result = tokens.estimate_cost(1000, 1000)
    assert result['priced'] is False
    assert result['total'] == 0


def test_estimate_cost_computes_amount_when_priced():
    result = tokens.estimate_cost(2000, 1000, price_per_1k_prompt=1.5,
                                 price_per_1k_completion=3.0, currency='CNY')
    assert result['priced'] is True
    assert result['prompt'] == pytest.approx(3.0)
    assert result['completion'] == pytest.approx(3.0)
    assert result['total'] == pytest.approx(6.0)
    assert result['currency'] == 'CNY'


@pytest.mark.skipif(not os.path.isdir(MAIN_APP_DIR), reason='主程序仓库不可用')
def test_matches_main_app_formula():
    """与主程序 `core/utils/text.py:41-44` 的公式逐样本对齐（契约 C1）。"""
    sys.path.insert(0, MAIN_APP_DIR)
    try:
        from core.utils.text import calculate_token_count  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - 环境缺失时跳过
        pytest.skip(f'无法导入主程序 token 工具: {exc}')
    finally:
        sys.path.remove(MAIN_APP_DIR)

    for sample in ['你好世界', 'Hello world', '混合 mixed 文本 123', '', 'a' * 7]:
        assert tokens.estimate(sample) == calculate_token_count({'name': sample}), sample
