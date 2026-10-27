"""测试夹具：让 tests/ 能以顶层模块方式导入插件，并把临时目录放到仓库内。"""

import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))


def pytest_configure(config) -> None:
    """本机 %TEMP% 下的 pytest 根目录无读权限，tmp_path 默认改用仓库内的 .pytest_tmp/。"""
    if not config.option.basetemp:
        config.option.basetemp = str(PLUGIN_DIR / ".pytest_tmp")
