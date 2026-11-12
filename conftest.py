"""测试引导：无论插件目录叫什么名字，都先按包把插件加载进 ``sys.modules``。

NeoBot 的插件目录通常是 ``plugins/<插件名>``（合法标识符），但本仓库的目录名
可能带连字符（``neobot-plugin-bili-monitor``）。pytest 会把工程根目录当成包并
按目录名导入，带连字符时必然失败；这里先注册好模块，让各种导入路径都能复用
同一个插件实例。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PACKAGE = "bili_monitor"


def _bootstrap() -> object:
    module = sys.modules.get(PACKAGE)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(
        PACKAGE,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    if spec is None or spec.loader is None:  # pragma: no cover - 入口缺失
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


_MODULE = _bootstrap()

# pytest 的 Package.setup() 会用目录名（或回落 ``__init__``）导入根目录
if _MODULE is not None:
    sys.modules.setdefault(ROOT.name, _MODULE)
    sys.modules.setdefault("__init__", _MODULE)
