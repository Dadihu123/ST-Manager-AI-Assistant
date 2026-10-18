"""概览（本地备注）的读写。

**历史上的越界点**：主程序此前没有「本地备注」的插件 API，本模块只能
`from core.data.ui_store import update_ui_data` + `from core.services.card_service
import resolve_ui_key`，靠内部 import 绕过契约面（Provisional 耦合）。

现在主程序已提供 `api.cards.read_note` / `write_note` / `read_notes`
（`core/plugins/domains/cards.py`），本模块退化为**薄封装**，不再直接触碰
主程序内部模块。

保留这层的原因：
1. 调用点语义是「概览」而非「备注」（虽然落点是同一个字段），保留命名可读性；
2. 批量与降级策略集中在一处，作业与路由共用。
"""

import logging

logger = logging.getLogger(__name__)

ORIGIN_CARD_NOTE = 'card_note'
ORIGIN_NAMESPACE = 'namespace'


def resolve_key(api, card_id: str) -> str:
    """解析卡片对应的 `ui_key`。

    主程序内部有 `resolve_ui_key(card_id)`（bundle 卡片的 `ui_key` 与 `card_id`
    不同），但插件拿不到它 —— 因此这里退化为 `card_id` 本身。
    幂等元数据按 `card_id` 记账即可：同一张卡永远得到同一个键。
    """
    return str(card_id or '').strip().replace('\\', '/')


def read_summary(api, card_id: str) -> dict:
    """读取主程序侧概览（用于「当前概览」展示与冲突提示）。"""
    identifier = str(card_id or '').strip()
    if not identifier:
        return {'ok': False, 'summary': '', 'message': '缺少 card_id'}

    try:
        text = api.cards.read_note(identifier)
    except Exception as exc:  # noqa: BLE001 - 能力不可用不应让调用方崩掉
        return {'ok': False, 'summary': '', 'ui_key': identifier, 'message': str(exc)}

    return {
        'ok': True,
        'summary': str(text or ''),
        'ui_key': resolve_key(api, identifier),
        'message': '',
    }


def write_summary(api, store, card_id: str, summary, *, ui_key: str = '') -> dict:
    """写入概览。

    Args:
        api: `PluginAPI`（提供 `cards.write_note`）。
        store: `PluginStore`（降级路径使用）。
        card_id: 卡片 id（相对 `CARDS_FOLDER` 的路径）。
        summary: 概览文本。
        ui_key: 已知的 ui_key（可选，用于插件侧元数据记账）。

    Returns:
        `{'ok', 'origin', 'ui_key', 'message'}`；`origin='card_note'` 表示写进
        主程序的本地备注，`namespace` 表示降级写入插件命名空间。
    """
    text = str(summary or '').strip()
    key = str(ui_key or '').strip() or resolve_key(api, card_id)

    if not text:
        return {'ok': False, 'origin': '', 'ui_key': key, 'message': '概览为空，已跳过写入'}

    try:
        written = bool(api.cards.write_note(card_id, text))
        message = ''
    except Exception as exc:  # noqa: BLE001
        written = False
        message = str(exc)

    if written:
        return {'ok': True, 'origin': ORIGIN_CARD_NOTE, 'ui_key': key, 'message': ''}

    # 降级：写进插件自己的元数据，至少不丢结果
    if store is not None and key:
        try:
            store.update_card_meta(key, {'summary': text, 'summary_origin': ORIGIN_NAMESPACE})
        except Exception:  # noqa: BLE001
            logger.warning('概览降级写入插件命名空间失败', exc_info=True)

    logger.warning('概览未能写入主程序本地备注（%s），已降级到插件命名空间', message or '未知原因')
    return {
        'ok': False,
        'origin': ORIGIN_NAMESPACE,
        'ui_key': key,
        'message': message or '主程序拒绝了本次写入',
    }
