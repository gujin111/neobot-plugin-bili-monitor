"""测试夹具：把插件目录当作包导入（目录名带连字符，不能用普通 import）。"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE = "bili_monitor"


def load_plugin_module():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def pkg():
    return load_plugin_module()


@pytest.fixture(scope="session")
def model(pkg):
    return importlib.import_module(f"{PACKAGE}.bili_model")


@pytest.fixture(scope="session")
def render(pkg):
    return importlib.import_module(f"{PACKAGE}.bili_render")


@pytest.fixture(scope="session")
def store_mod(pkg):
    return importlib.import_module(f"{PACKAGE}.bili_store")


@pytest.fixture(scope="session")
def engine_mod(pkg):
    return importlib.import_module(f"{PACKAGE}.bili_engine")
