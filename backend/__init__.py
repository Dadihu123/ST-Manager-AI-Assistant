"""stm-ai-assistant 插件入口。

`register(api)` 由 manifest 的 `backend.register` 指定（见 `core/plugins/registry.py:289-303`）：
主程序在 `create_app()` 阶段构造 `PluginAPI` 并调用它；`api.route()` 收集的路由规则
会在 `register()` 返回后由 `api.finalize()` 统一注册。

装配顺序：store → routes → jobs → cli → 首次运行初始化。
"""

import logging

from .store import PluginStore

logger = logging.getLogger(__name__)

__all__ = ['register']


def register(api):
    """插件注册入口。"""
    from . import cli as cli_module
    from . import jobs as jobs_module
    from . import routes as routes_module

    store = PluginStore(api)
    api.log('info', 'stm-ai-assistant 开始装配')

    routes_module.register_routes(api, store)
    jobs_module.register_jobs(api, store)
    cli_module.register_cli(api, store)

    # 首次运行初始化：写档案索引、按 secret_schema 生成默认 provider。
    # 失败不阻断插件加载（降级为「设置页为空」，用户可手动创建）。
    try:
        store.sync_profile_index()
        store.providers_config()
    except Exception:  # noqa: BLE001
        logger.warning('stm-ai-assistant 初始化失败', exc_info=True)

    api.log('info', f'stm-ai-assistant 就绪：{len(store.list_profiles())} 个提示词档案')
