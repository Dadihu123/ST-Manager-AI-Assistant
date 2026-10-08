"""CLI 子命令（`python app.py plugin stm-ai-assistant <name> [args...]`）。

契约见主程序设计文档附录 I：`handler(namespace)` 接收 `argparse.Namespace`，
`namespace.args` 是位置参数列表，`namespace.options` 是 `--key value` / `--flag`
的解析结果；返回 `None`/`True` → 0、`False` → 1、`int` → 原样作为退出码。

注意：CLI **不启动服务器、不跑 `init_services`**（附录 I 第 5 条），作业 worker
不会运行——因此 `analyze` 走**同步执行**，不经过作业系统。
"""

import json
import logging

from . import config as config_module
from . import preview as preview_module
from .core import prompt as prompt_core
from .core.errors import AiAssistantError

logger = logging.getLogger(__name__)

ANALYZE_USAGE = (
    '用法: analyze <card_id> [card_id...] '
    '[--profile=id] [--provider=id] [--model=名称] [--mode=both|summary|tags] [--apply]'
)


class _CliContext:
    """CLI 用的最小作业上下文（满足 `_analyze_one` 的依赖）。"""

    def __init__(self, run_id: str = 'cli'):
        self.run_id = str(run_id)

    def should_cancel(self) -> bool:
        return False

    def log(self, level: str, message: str) -> None:
        print(f'[{level}] {message}')


def register_cli(api, store) -> None:
    """注册 CLI 子命令。"""
    api.register_cli(
        'status', lambda namespace: cmd_status(api, store, namespace),
        description='打印配置、provider、提示词档案与用量台账',
    )
    api.register_cli(
        'profiles', lambda namespace: cmd_profiles(api, store, namespace),
        description='档案管理：list / show <id> / export [--out=路径]',
    )
    api.register_cli(
        'preview', lambda namespace: cmd_preview(api, store, namespace),
        description='渲染提示词预览：preview <card_id> [--profile=] [--provider=] [--model=]',
    )
    api.register_cli(
        'analyze', lambda namespace: cmd_analyze(api, store, namespace),
        description='同步分析卡片（不写盘，除非加 --apply）：' + ANALYZE_USAGE,
    )


def cmd_status(api, store, namespace) -> int:
    config = config_module.load_config(api)
    print(f'插件: {api.plugin_id} v{api.manifest.get("version", "")}（主程序 {api.app_version}）')
    print(f'写回方式: {config["apply_mode"]} · 批次: {config["batch_size"]} · 跳过未变化: {config["skip_unchanged"]}')

    profiles = store.list_profiles()
    active = store.active_profile_id() or '(按模型自动匹配)'
    print(f'提示词档案: {len(profiles)} 个 · 当前: {active}')

    providers = store.describe_providers()
    print(f'Provider: {len(providers)} 个')
    for item in providers:
        key_state = '已设置' if (item.get('has_api_key') or item.get('has_fallback_key')) else '未设置'
        flags = []
        if item.get('is_default'):
            flags.append('默认')
        if item.get('allow_local'):
            flags.append('允许本地')
        suffix = f' [{" · ".join(flags)}]' if flags else ''
        print(f'  - {item["id"]}{suffix}: {item["base_url"]} · 模型={item.get("model") or "(未设)"} · 密钥={key_state}')

    ledger = store.usage_ledger()
    print(
        '用量: prompt={prompt} · completion={completion} · 作业={jobs}'.format(
            prompt=ledger['total_prompt_tokens'],
            completion=ledger['total_completion_tokens'],
            jobs=ledger['jobs'],
        )
    )

    suggestions = store.list_tag_suggestions()
    if suggestions:
        print(f'待审批新标签: {len(suggestions)} 个（{"、".join(list(suggestions)[:10])}）')
    return 0


def cmd_profiles(api, store, namespace) -> int:
    args = [str(item) for item in (namespace.args or [])]
    options = namespace.options or {}
    action = args[0] if args else 'list'

    if action == 'list':
        index = store.profile_index()
        if not index:
            print('（没有档案）')
            return 0
        for item in index:
            origin = '内置' if item['builtin'] else '自定义'
            print(f'{item["id"]:<28} {item["model_match"]:<14} v{item["version"]:<3} {origin:<4} {item["name"]}')
        return 0

    if action == 'show':
        if len(args) < 2:
            print('用法: profiles show <id>')
            return 2
        profile = store.get_profile(args[1])
        if profile is None:
            print(f'[error] 档案不存在: {args[1]}')
            return 1
        print(json.dumps(profile, ensure_ascii=False, indent=2))
        return 0

    if action == 'export':
        payload = store.export_profiles(str(options.get('id') or ''))
        out = str(options.get('out') or '')
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if out:
            try:
                with open(out, 'w', encoding='utf-8') as handle:
                    handle.write(text)
            except OSError as exc:
                print(f'[error] 写入失败: {exc}')
                return 1
            print(f'已导出 {len(payload.get("profiles") or [])} 个档案到 {out}')
        else:
            print(text)
        return 0

    print(f'[error] 未知子命令: {action}（可用: list / show / export）')
    return 2


def cmd_preview(api, store, namespace) -> int:
    args = [str(item) for item in (namespace.args or [])]
    options = namespace.options or {}

    if not args:
        print('用法: preview <card_id> [--profile=] [--provider=] [--model=]')
        return 2

    config = config_module.load_config(api)
    try:
        result = preview_module.build_preview(
            api=api, store=store, config=config, card_id=args[0],
            profile_id=str(options.get('profile') or ''),
            provider_id=str(options.get('provider') or ''),
            model=str(options.get('model') or ''),
            budget_tokens=int(options.get('budget') or 0),
        )
    except AiAssistantError as exc:
        print(f'[error] {exc.code}: {exc.message}')
        return 1

    print(f'档案: {result["profile"]["id"]}（{result["profile"]["name"]}）v{result["profile"]["version"]}')
    print(f'Provider: {result["provider"]["id"]} · 模型: {result["provider"]["model"] or "(未设)"}')
    print(
        'Token 估算: system={system} · user={user} · 合计={total} · 最大输出={max_completion}'.format(
            system=result['tokens']['system'], user=result['tokens']['user'],
            total=result['tokens']['total'], max_completion=result['tokens']['max_completion'],
        )
    )
    worldbook = result['worldbook']
    print(
        f'世界书: {worldbook["count"]} 条 · 总 {worldbook["total_tokens"]} token · '
        f'节选 {worldbook["excerpt_tokens"]} token · 裁剪={worldbook["truncated"]}'
    )
    if result['missing']['required']:
        print(f'[warning] 缺失必需变量: {"、".join(result["missing"]["required"])}')
    if result['missing']['optional']:
        print(f'[info] 缺失可选变量: {"、".join(result["missing"]["optional"])}')

    for message in result['messages']:
        print('-' * 60)
        print(f'[{message["role"]}]')
        print(message['content'])
    print('-' * 60)
    return 0


def cmd_analyze(api, store, namespace) -> int:
    from . import summary_bridge  # noqa: PLC0415 - 延迟导入，避免 CLI 与作业模块的循环引用
    from .core import hashing  # noqa: PLC0415
    from .jobs import (  # noqa: PLC0415
        MODE_BOTH,
        VALID_MODES,
        analyze_one,
        known_tag_set,
        read_tag_library,
        tag_usage,
    )

    args = [str(item) for item in (namespace.args or []) if str(item).strip()]
    options = namespace.options or {}

    if not args:
        print(ANALYZE_USAGE)
        return 2

    config = config_module.load_config(api)
    try:
        provider = store.resolve_provider(str(options.get('provider') or ''))
    except AiAssistantError as exc:
        print(f'[error] {exc.code}: {exc.message}')
        return 1

    api_key = store.resolve_provider_key(provider['id'])
    if not api_key:
        print(f'[error] provider「{provider["id"]}」未配置 API Key')
        return 1

    mode = str(options.get('mode') or MODE_BOTH).strip().lower()
    if mode not in VALID_MODES:
        print(f'[error] mode 必须是 {list(VALID_MODES)} 之一')
        return 2

    model_name = str(options.get('model') or provider.get('model') or '')
    try:
        profile = store.resolve_profile(str(options.get('profile') or ''), model_name)
    except AiAssistantError as exc:
        print(f'[error] {exc.code}: {exc.message}')
        return 1

    apply_to_disk = bool(options.get('apply'))
    limits = prompt_core.build_limits(config, profile)
    tag_library = read_tag_library(api)
    known_tags = known_tag_set(tag_library)
    usage_map = tag_usage(api)
    keywords = config_module.wi_keywords(config)
    wi_budget = preview_module.worldbook_budget(config)
    extra_headers = store.resolve_provider_headers(provider['id'])
    ctx = _CliContext()

    print(
        f'档案={profile["id"]}(v{profile.get("version")}) · provider={provider["id"]} · '
        f'模型={model_name or "(未设)"} · {"写入磁盘" if apply_to_disk else "只预览（不写盘）"}'
    )

    exit_code = 0
    for card_id in args:
        card = api.read_card(card_id)
        content_hash = hashing.card_content_hash(card) if isinstance(card, dict) else ''
        ui_key = summary_bridge.resolve_key(card_id)

        try:
            outcome = analyze_one(
                api=api, store=store, config=config, ctx=ctx,
                card_id=card_id, ui_key=ui_key, content_hash=content_hash, mode=mode,
                profile=profile, limits=limits, model_name=model_name,
                provider=provider, api_key=api_key, extra_headers=extra_headers,
                tag_library=tag_library, known_tags=known_tags, usage_map=usage_map,
                wi_budget=wi_budget, keywords=keywords,
                apply_mode='auto' if apply_to_disk else 'review',
                dry_run=not apply_to_disk, force=True, strict=config['validation_strict'],
                apply_tags_enabled=config['apply_tags'],
            )
        except AiAssistantError as exc:
            print(f'[error] {card_id}: {exc.code}: {exc.message}')
            exit_code = 1
            continue

        print('=' * 60)
        print(f'{card_id} → {outcome["status"]}')
        print(json.dumps(outcome['result'], ensure_ascii=False, indent=2))

    return exit_code
