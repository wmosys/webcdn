"""共享的 pytest fixture 与导入路径设置。

把仓库的 `scripts/` 目录加入 `sys.path`，使测试可以直接
`import aggregate_rules` / `import notify_serverchan`。
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
