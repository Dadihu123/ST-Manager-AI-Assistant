"""概览写入主程序 `ui_data`（设计文档 Q1 决策：内部 bridge + 降级）。

**背景**：`PluginAPI` 没有写 `ui_data[ui_key].summary` 的能力——`ui_data_update()`
只能写 `_plugins_v1.<id>` 命名空间，而 `write_card_fields` 的字段白名单
（`core/plugins/api.py:62-66`）不含 `summary`。而概览的现有渲染链路读的正是
`ui_data[<resolve_ui_key(card_id)>]['summary']`（`core/data/cache.py:776-788`）。

**边界**（决策 Q5）：本模块是全插件**唯一**允许导入主程序内部模块的地方。
策略是「尽力而为 + 绝不崩」：

1. 优先走 `core.data.ui_store.update_ui_data` + `core.services.card_service.resolve_ui_key`
   在锁内写入；
2. 任一步失败（模块缺失、签名变化、写入被拒）→ 降级写入插件命名空间
   `card_meta[ui_key].summary`，返回 `origin='namespace'`，由 UI 明示。
"""

import logging

logger = logging.getLogger(__name__)

ORIGIN_UI_DATA = 'ui_data'
ORIGIN_NAMESPACE = 'namespace'


def _import_bridge():
    """延迟导入主程序模块；失败返回 `(None, None, 原因)`。"""
    try:
        from core.data.ui_store import update_ui_data  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - 主程序内部模块，缺失即降级
        return None, None, f'无法导入 core.data.ui_store: {exc}'

    try:
        from core.services.card_service import resolve_ui_key  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return None, None, f'无法导入 core.services.card_service: {exc}'

    return update_ui_data, resolve_ui_key, ''


def resolve_key(card_id: str) -> str:
    """解析卡片对应的 `ui_key`；bridge 不可用时退回 `card_id` 本身。"""
    identifier = str(card_id or '').strip().replace('\\', '/')
    if not identifier:
        return ''

    _update, resolve_ui_key, reason = _import_bridge()
    if resolve_ui_key is None:
        logger.debug('ui_key 解析降级（%s）', reason)
        return identifier

    try:
        return str(resolve_ui_key(identifier) or identifier)
    except Exception:  # noqa: BLE001
        logger.debug('resolve_ui_key 调用失败，退回 card_id', exc_info=True)
        return identifier


def read_summary(card_id: str) -> dict:
    """读取主程序侧概览（用于「当前概览」展示与冲突提示）。"""
    identifier = str(card_id or '').strip()
    if not identifier:
        return {'ok': False, 'summary': '', 'message': '缺少 card_id'}

    try:
        from core.data.ui_store import load_ui_data_or_empty  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return {'ok': False, 'summary': '', 'message': f'无法读取主程序 ui_data: {exc}'}

    key = resolve_key(identifier)
    try:
        ui_data = load_ui_data_or_empty()
        entry = ui_data.get(key) if isinstance(ui_data, dict) else None
        summary = entry.get('summary', '') if isinstance(entry, dict) else ''
        return {'ok': True, 'summary': str(summary or ''), 'ui_key': key, 'message': ''}
    except Exception as exc:  # noqa: BLE001
        return {'ok': False, 'summary': '', 'ui_key': key, 'message': str(exc)}


def write_summary(store, card_id: str, summary, *, ui_key: str = '') -> dict:
    """写入概览。

    Args:
        store: `PluginStore`（降级路径使用）。
        card_id: 卡片 id（相对 `CARDS_FOLDER` 的路径）。
        summary: 概览文本。
        ui_key: 已知的 ui_key（可选，避免重复解析）。

    Returns:
        `{'ok', 'origin', 'ui_key', 'message'}`；`origin` 为 `ui_data` 表示写进主程序，
        `namespace` 表示降级到插件命名空间。
    """
    text = str(summary or '').strip()
    key = str(ui_key or '').strip() or resolve_key(card_id)

    if not text:
        return {'ok': False, 'origin': '', 'ui_key': key, 'message': '概览为空，已跳过写入'}

    result = _write_ui_data(card_id, key, text)
    if result['ok']:
        return result

    if store is not None and key:
        try:
            store.update_card_meta(key, {'summary': text, 'summary_origin': ORIGIN_NAMESPACE})
        except Exception:  # noqa: BLE001
            logger.warning('概览降级写入插件命名空间失败', exc_info=True)

    logger.warning('概览未能写入主程序 ui_data（%s），已降级到插件命名空间', result['message'])
    return {
        'ok': False,
        'origin': ORIGIN_NAMESPACE,
        'ui_key': key,
        'message': result['message'],
    }


def _write_ui_data(card_id: str, ui_key: str, text: str) -> dict:
    update_ui_data, resolve_ui_key, reason = _import_bridge()
    if update_ui_data is None:
        return {'ok': False, 'origin': '', 'ui_key': ui_key, 'message': reason}

    key = ui_key
    try:
        key = str(resolve_ui_key(str(card_id or '').strip()) or ui_key)
    except Exception:  # noqa: BLE001
        logger.debug('resolve_ui_key 调用失败，使用传入的 ui_key', exc_info=True)

    if not key:
        return {'ok': False, 'origin': '', 'ui_key': '', 'message': '无法解析 ui_key'}

    def mutator(ui_data):
        if not isinstance(ui_data, dict):
            return
        entry = ui_data.get(key)
        if not isinstance(entry, dict):
            entry = {}
            ui_data[key] = entry
        entry['summary'] = text

    try:
        saved = bool(update_ui_data(mutator))
    except Exception as exc:  # noqa: BLE001 - 主程序内部实现变化不应让作业崩掉
        return {'ok': False, 'origin': '', 'ui_key': key, 'message': f'写入异常: {exc}'}

    if not saved:
        return {'ok': False, 'origin': '', 'ui_key': key, 'message': '主程序拒绝了本次 ui_data 写入'}

    return {'ok': True, 'origin': ORIGIN_UI_DATA, 'ui_key': key, 'message': ''}
