"""概览（本地备注）的读写。

**历史上的越界点**：主程序此前没有「本地备注」的插件 API，本模块只能
`from core.data.ui_store import update_ui_data` + `from core.services.card_service
import resolve_ui_key`，靠内部 import 绕过契约面（Provisional 耦合）。

现在主程序已提供 `api.cards.read_note` / `write_note` / `read_notes`
（`core/plugins/domains/cards.py`），本模块退化为**薄封装**，不再直接触碰
主程序内部模块。

保留这层的原因：
1. 调用点语义是「概览」而非「备注」（虽然落点是同一个字段），保留命名可读性；
2. 批量与降级策略集中在一处，作业与路由共用；
3. **合成策略**（覆盖 / 接在头部 / 接在尾部 / 不写）集中在这里，见
   `core/note_merge.py`。写入口只有这一处，所以策略不可能被某条路径绕过。
"""

import logging

from .core import note_merge

logger = logging.getLogger(__name__)

ORIGIN_CARD_NOTE = 'card_note'
ORIGIN_NAMESPACE = 'namespace'
MAX_NOTE_WRITE_ATTEMPTS = 3


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


def write_summary(api, store, card_id: str, summary, *, ui_key: str = '',
                  mode: str = '') -> dict:
    """写入概览（按 `mode` 合成，见 `core/note_merge.py`）。

    Args:
        api: `PluginAPI`（提供 `cards.read_note` / `cards.write_note`）。
        store: `PluginStore`（降级路径与「上次写了什么」使用）。
        card_id: 卡片 id（相对 `CARDS_FOLDER` 的路径）。
        summary: AI 生成的概览文本。
        ui_key: 已知的 ui_key（可选，用于插件侧元数据记账）。
        mode: 合成策略；省略时用 `note_merge.DEFAULT_NOTE_MODE`。

    Returns:
        `{'ok', 'origin', 'ui_key', 'message', 'mode', 'kept_chars',
           'summary_chars', 'changed', 'skipped'}`；`origin='card_note'` 表示写进
        主程序的本地备注，`namespace` 表示降级写入插件命名空间。
    """
    text = str(summary or '').strip()
    key = str(ui_key or '').strip() or resolve_key(api, card_id)
    resolved_mode = note_merge.normalize_mode(mode)

    base = {
        'ok': False,
        'origin': '',
        'ui_key': key,
        'message': '',
        'mode': resolved_mode,
        'kept_chars': 0,
        'summary_chars': len(text),
        'changed': False,
        'skipped': '',
    }

    if resolved_mode == 'skip':
        # 用户显式选择「不写简介」：不算失败，也不要降级写进命名空间
        # （否则「不写」变成了「写到别处」，用户以为生效了其实只是搬了个地方）。
        base['ok'] = True
        base['skipped'] = 'mode_skip'
        return base

    if not text:
        base['message'] = '概览为空，已跳过写入'
        base['skipped'] = 'empty'
        return base

    # 读当前备注：合成策略要求「保住用户写的那部分」，因此必须先读。
    # 读失败时**不能**当作「备注是空的」继续写 —— 那正好会覆盖掉用户的备注，
    # 也就是这个功能要修的 bug。宁可这次不写卡片备注、只存进插件，
    # 也不拿用户的数据赌一次「大概读不到就是空吧」。
    read_result = read_summary(api, card_id)
    if not read_result.get('ok'):
        message = read_result.get('message') or '读取本地备注失败'
        logger.warning('读取本地备注失败，本次不写卡片备注（避免覆盖用户内容）: %s %s',
                       card_id, message)
        if store is not None and key:
            try:
                store.update_card_meta(key, {'summary': text,
                                             'summary_origin': ORIGIN_NAMESPACE})
            except Exception:  # noqa: BLE001
                logger.warning('概览降级写入插件命名空间失败', exc_info=True)
        base.update(
            origin=ORIGIN_NAMESPACE,
            message=f'读不到当前本地备注，为免覆盖已存的内容，本次只把概览存进插件（{message}）',
        )
        return base

    existing = str(read_result.get('summary') or '')
    message = ''
    payload = text

    for attempt in range(MAX_NOTE_WRITE_ATTEMPTS):
        composed = note_merge.compose(existing, text, resolved_mode)
        base['kept_chars'] = int(composed.get('kept_chars') or 0)
        base['changed'] = bool(composed.get('changed'))

        if not composed.get('changed'):
            # 已经是这个内容（同一份简介重复打标）：不写盘。
            # 写一次没变化的内容等于白刷宿主缓存 —— 而宿主每次
            # `update_ui_data` 都是「整文件 + 4 代备份 + fsync」。
            base.update(ok=True, origin=ORIGIN_CARD_NOTE, skipped='unchanged')
            return base

        payload = str(composed.get('text') or '')

        try:
            written = bool(api.cards.write_note(
                card_id, payload, expected_text=existing,
            ))
        except TypeError as exc:
            # 旧宿主的 write_note 没有条件参数。参数绑定失败发生在宿主写入前，
            # 因此只能安全降级，不能重试无条件写入。
            message = f'宿主不支持条件备注写入：{exc}'
            break
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            break

        if written:
            base.update(ok=True, origin=ORIGIN_CARD_NOTE)
            return base

        # False 可能是并发冲突，也可能是卡片/磁盘写入失败。只有读回值确实变化时
        # 才重新合成并重试；读回仍相同就按普通写入失败处理。
        latest = read_summary(api, card_id)
        if not latest.get('ok'):
            message = latest.get('message') or '条件写入失败后无法重新读取本地备注'
            break

        latest_text = str(latest.get('summary') or '')
        if latest_text == existing:
            message = '主程序拒绝了条件备注写入'
            break

        existing = latest_text
        if attempt == MAX_NOTE_WRITE_ATTEMPTS - 1:
            composed = note_merge.compose(existing, text, resolved_mode)
            base['kept_chars'] = int(composed.get('kept_chars') or 0)
            base['changed'] = bool(composed.get('changed'))
            if not composed.get('changed'):
                base.update(ok=True, origin=ORIGIN_CARD_NOTE, skipped='unchanged')
                return base
            payload = str(composed.get('text') or '')
            message = f'本地备注连续发生并发修改，已达到 {MAX_NOTE_WRITE_ATTEMPTS} 次写入上限'

    # CAS 冲突时 payload 已按最新读到的备注重新合成；旧宿主没有无条件写入尝试。
    if store is not None and key:
        try:
            store.update_card_meta(key, {'summary': payload,
                                         'summary_origin': ORIGIN_NAMESPACE})
        except Exception:  # noqa: BLE001
            logger.warning('概览降级写入插件命名空间失败', exc_info=True)

    logger.warning('概览未能写入主程序本地备注（%s），已降级到插件命名空间', message or '未知原因')
    base.update(origin=ORIGIN_NAMESPACE, message=message or '主程序拒绝了本次写入')
    return base
