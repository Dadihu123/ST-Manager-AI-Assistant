"""应用动作执行器：把「模型答出来的东西」按声明落到卡片上。

设计文档 §8.4。task spec 的 `apply_actions` 是一串声明：

```jsonc
[
  {"from": "summary", "action": "write_card_note"},
  {"from": "tags",    "action": "apply_tags", "params": {"replace": false}},
  {"from": "suggested_new_tags", "action": "queue_suggestions"}
]
```

`from` 指向 `output_schema.properties` 里的字段名，`action` 是本模块登记的动作。
**新增功能不再需要改 jobs**：加一个 task spec（配置）即可。

设计要点：

- 每个动作返回统一结构 `{'ok', 'applied', 'skipped', 'message'}`，
  单个动作失败**不中断**其余动作（一个字段写坏了不该让标签也丢掉）。
- 所有写操作都走 `PluginAPI` 的域方法，不碰主程序内部模块。
- `apply_tags` 复用 `jobs.split_tags` 的已知标签过滤，避免写入不存在的标签。
"""

import logging

from .core.errors import AiAssistantError

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ 动作实现


def _action_write_card_note(api, store, card_id, value, params, context) -> dict:
    """把字段值写进主程序的本地备注（概览）。"""
    from . import summary_bridge  # 局部导入：避免与 jobs 成环

    text = str(value or '').strip()
    if not text:
        return {'ok': True, 'applied': False, 'skipped': 'empty', 'message': ''}

    result = summary_bridge.write_summary(
        api, store, card_id, text, ui_key=context.get('ui_key', ''),
    )
    return {
        'ok': bool(result.get('ok')),
        'applied': bool(result.get('ok')),
        'skipped': '',
        'message': str(result.get('message') or ''),
        'origin': str(result.get('origin') or ''),
    }


def _action_apply_tags(api, store, card_id, value, params, context) -> dict:
    """把标签合并进卡片（黑名单与未知标签由主程序/插件过滤）。

    取值优先级：`context['accepted_tags']` > 字段值。前者是**已按标签库筛过**
    的清单（作业在解析阶段就算好了），后者可能含未知标签。
    """
    accepted = context.get('accepted_tags')
    if not isinstance(accepted, list):
        accepted = value if isinstance(value, list) else []

    names = []
    for item in accepted:
        # accepted_tags 可能是字符串列表（已筛过已知标签），也可能是 {name, confidence}
        if isinstance(item, dict):
            name = str(item.get('name') or '').strip()
        else:
            name = str(item or '').strip()
        if name and name not in names:
            names.append(name)

    if not names:
        return {'ok': True, 'applied': False, 'skipped': 'empty', 'message': ''}

    replace = bool(params.get('replace', False))
    result = api.cards.apply_tags([card_id], add=names, replace=replace)

    messages = []
    if result.get('skipped_blacklist'):
        messages.append('被黑名单拦截: ' + '、'.join(result['skipped_blacklist']))
    if result.get('skipped_unknown'):
        messages.append('因标签库锁定被跳过: ' + '、'.join(result['skipped_unknown']))

    failed = result.get('failed') or []
    return {
        'ok': not failed,
        'applied': bool(result.get('updated')),
        'skipped': '',
        'message': '；'.join(messages),
        'added': list(result.get('added') or []),
    }


def _action_write_card_fields(api, store, card_id, value, params, context) -> dict:
    """改写卡片正文字段（写前自动快照，便于回滚）。"""
    if not isinstance(value, dict) or not value:
        return {'ok': True, 'applied': False, 'skipped': 'empty', 'message': ''}

    # 只允许改这些字段名，防止模型凭空写出奇怪键名污染卡片
    allowed = params.get('fields')
    patch = {}
    for key, item in value.items():
        name = str(key or '').strip()
        if not name:
            continue
        if isinstance(allowed, (list, tuple)) and name not in allowed:
            continue
        patch[name] = '' if item is None else str(item)

    if not patch:
        return {'ok': True, 'applied': False, 'skipped': 'no_allowed_field', 'message': ''}

    if params.get('snapshot', True):
        try:
            api.backups.snapshot_card(card_id, remark='task_spec 改写前快照')
        except Exception:  # noqa: BLE001 - 快照失败不该阻断写入
            logger.warning('改写前快照失败: %s', card_id, exc_info=True)

    written = bool(api.cards.write_fields(card_id, patch))
    return {
        'ok': written,
        'applied': written,
        'skipped': '',
        'message': '' if written else '主程序拒绝写入',
        'fields': sorted(patch),
    }


def _action_write_worldbook(api, store, card_id, value, params, context) -> dict:
    """改写世界书条目。

    `value` 形如 `{'new_content': '...', 'entry_uid': '...'}`；`entry_uid`
    缺失时从 `params` 取（task spec 可以在动作里写死要改哪条）。
    """
    if not isinstance(value, dict):
        return {'ok': True, 'applied': False, 'skipped': 'empty', 'message': ''}

    new_content = str(value.get('new_content') or value.get('content') or '').strip()
    if not new_content:
        return {'ok': True, 'applied': False, 'skipped': 'empty', 'message': ''}

    source = params.get('source') or context.get('worldbook_source')
    entry_uid = str(value.get('entry_uid') or params.get('entry_uid') or '').strip()
    if not source or not entry_uid:
        return {
            'ok': False, 'applied': False, 'skipped': 'missing_target',
            'message': '缺少世界书来源或条目 uid，未写入',
        }

    # 先读再写：`write` 需要完整数据 + 修订号（乐观并发）
    current = api.worldbook.read(source)
    if not isinstance(current, dict):
        return {
            'ok': False, 'applied': False, 'skipped': 'unreadable',
            'message': '无法读取世界书',
        }

    entries = current.get('entries')
    if not isinstance(entries, list):
        return {
            'ok': False, 'applied': False, 'skipped': 'unreadable',
            'message': '世界书结构异常（无 entries）',
        }

    found = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get('uid') or entry.get('id') or '') == entry_uid:
            entry['content'] = new_content
            found = True
            break

    if not found:
        return {
            'ok': False, 'applied': False, 'skipped': 'entry_not_found',
            'message': f'世界书里找不到条目 {entry_uid}',
        }

    try:
        api.backups.snapshot_card(card_id, remark='世界书改写前快照')
    except Exception:  # noqa: BLE001
        logger.warning('世界书改写前快照失败', exc_info=True)

    result = api.worldbook.write(
        source, current, expected_revision=str(current.get('source_revision') or ''),
    )
    ok = bool(result.get('success'))
    return {
        'ok': ok,
        'applied': ok,
        'skipped': '',
        'message': '' if ok else str(result.get('message') or '主程序拒绝写入'),
        'entry_uid': entry_uid,
    }


def _action_queue_suggestions(api, store, card_id, value, params, context) -> dict:
    """把新标签建议放进队列（**不自动创建标签**）。"""
    items = value if isinstance(value, list) else []
    cleaned = []
    for item in items:
        if isinstance(item, dict):
            name = str(item.get('name') or '').strip()
            reason = str(item.get('reason') or '').strip()
        else:
            name = str(item or '').strip()
            reason = ''
        if name:
            cleaned.append({'name': name, 'reason': reason})

    if not cleaned:
        return {'ok': True, 'applied': False, 'skipped': 'empty', 'message': ''}

    try:
        store.record_tag_suggestions(cleaned, card_key=context.get('ui_key', ''))
    except Exception as exc:  # noqa: BLE001
        logger.warning('记录新标签建议失败', exc_info=True)
        return {'ok': False, 'applied': False, 'skipped': '', 'message': str(exc)}

    return {
        'ok': True, 'applied': True, 'skipped': '', 'message': '',
        'count': len(cleaned),
    }


def _action_move_cards(api, store, card_id, value, params, context) -> dict:
    """把卡片移动到分类目录。

    `value` 可以是分类名字符串，也可以是 `{'category': '...'}`。
    """
    category = ''
    if isinstance(value, dict):
        category = str(value.get('category') or '').strip()
    else:
        category = str(value or '').strip()

    category = category or str(params.get('category') or '').strip()
    if not category:
        return {'ok': True, 'applied': False, 'skipped': 'empty', 'message': ''}

    if params.get('create_category', True):
        try:
            api.cards.create_category(category)
        except Exception:  # noqa: BLE001 - 已存在会返回 False，不算错误
            logger.debug('创建分类失败（可能已存在）: %s', category, exc_info=True)

    result = api.cards.move([card_id], category)
    ok = not (result.get('failed') or [])
    return {
        'ok': ok,
        'applied': bool(result.get('count')),
        'skipped': '',
        'message': '' if ok else '主程序拒绝移动',
        'category': category,
    }


#: `action` 名 → 实现
ACTION_HANDLERS = {
    'write_card_note': _action_write_card_note,
    'apply_tags': _action_apply_tags,
    'write_card_fields': _action_write_card_fields,
    'write_worldbook': _action_write_worldbook,
    'queue_suggestions': _action_queue_suggestions,
    'move_cards': _action_move_cards,
}


# ------------------------------------------------------------------ 入口


def run_actions(actions, result, *, api, store, card_id: str, context=None) -> dict:
    """按声明顺序执行应用动作。

    Args:
        actions: task spec 的 `apply_actions`。
        result: 解析后的字段值（`parsing.validate_result()['result']`）。
        api: `PluginAPI`。
        store: `PluginStore`。
        card_id: 目标卡片。
        context: `{'ui_key', 'accepted_tags', 'worldbook_source', ...}` 之类的上下文。

    Returns:
        `{'applied': bool, 'actions': [{'action','from','ok','applied','skipped','message'}],
           'warnings': [str]}`
    """
    spec_list = actions if isinstance(actions, (list, tuple)) else []
    values = result if isinstance(result, dict) else {}
    ctx_data = dict(context or {})

    records = []
    warnings = []
    any_applied = False

    for entry in spec_list:
        if not isinstance(entry, dict):
            continue

        action = str(entry.get('action') or '').strip()
        source = str(entry.get('from') or '').strip()
        params = entry.get('params') if isinstance(entry.get('params'), dict) else {}

        handler = ACTION_HANDLERS.get(action)
        if handler is None:
            warnings.append(f'未知的应用动作，已跳过: {action}')
            records.append({
                'action': action, 'from': source, 'ok': False,
                'applied': False, 'skipped': 'unknown_action', 'message': '',
            })
            continue

        value = values.get(source) if source else None

        try:
            outcome = handler(api, store, card_id, value, params, ctx_data)
        except AiAssistantError as exc:
            warnings.append(f'动作 {action} 失败: {exc.message}')
            outcome = {'ok': False, 'applied': False, 'skipped': '',
                       'message': exc.message}
        except Exception as exc:  # noqa: BLE001 - 单动作失败不该中断整批
            logger.warning('应用动作 %s 异常', action, exc_info=True)
            warnings.append(f'动作 {action} 异常: {exc}')
            outcome = {'ok': False, 'applied': False, 'skipped': '',
                       'message': str(exc)}

        record = {'action': action, 'from': source}
        record.update(outcome)
        records.append(record)

        if outcome.get('applied'):
            any_applied = True
        if outcome.get('message'):
            warnings.append(f'动作 {action}: {outcome["message"]}')

    return {'applied': any_applied, 'actions': records, 'warnings': warnings}
