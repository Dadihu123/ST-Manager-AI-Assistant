"""测试夹具。

加载方式**刻意模拟主程序**：用 `importlib.util.spec_from_file_location` +
`submodule_search_locations` 构造包（见 `core/plugins/registry.py:262-286`），
不注入 `sys.path`，以验证插件内部的相对导入在真实加载路径下同样成立。
"""

import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_DIR = os.path.join(ROOT, 'backend', 'core')
PACKAGE_NAME = 'stm_ai_core'

MAIN_APP_DIR = os.environ.get('STM_MAIN_APP_DIR', r'D:\Workspace\MyOwn\ST-Manager')


def _load_core_package():
    """把 `backend/core/` 加载为包 `stm_ai_core`。"""
    if PACKAGE_NAME in sys.modules:
        return sys.modules[PACKAGE_NAME]

    init_py = os.path.join(CORE_DIR, '__init__.py')
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME, init_py, submodule_search_locations=[CORE_DIR],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return module


# 模块级执行：测试模块可以直接 `from stm_ai_core import prompt`
_load_core_package()


def load_backend_module(relative_path: str, module_name: str):
    """按路径加载 `backend/` 下的任意模块（用于装配层测试）。"""
    if module_name in sys.modules:
        return sys.modules[module_name]

    target = os.path.join(ROOT, *relative_path.split('/'))
    spec = importlib.util.spec_from_file_location(module_name, target)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_backend_package():
    """把 `backend/` 加载为包（模拟主程序的插件加载）。"""
    name = 'stm_ai_backend'
    if name in sys.modules:
        return sys.modules[name]

    init_py = os.path.join(ROOT, 'backend', '__init__.py')
    spec = importlib.util.spec_from_file_location(
        name, init_py, submodule_search_locations=[os.path.join(ROOT, 'backend')],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def load_backend_submodule(dotted: str):
    """在 `load_backend_package()` 之后按点分名导入子模块（`backend.llm` → `'llm'`）。

    与主程序一致：模块名前缀 `st_plugin_<id>`，子模块通过包路径解析，
    因此插件内部的相对导入（`from .core import ...`）能正常工作。
    """
    load_backend_package()
    import importlib as _importlib  # noqa: PLC0415

    return _importlib.import_module(f'stm_ai_backend.{dotted}')
