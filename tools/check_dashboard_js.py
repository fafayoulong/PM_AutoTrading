# -*- coding: utf-8 -*-
"""把 src/dashboard.py 里内嵌的 <script> 段抽出来交给 node --check 做语法校验。

面板的 JS 是写在 Python 字符串里的，`py_compile` 查不出语法错误 —— 改面板必须
跑这个（项目约定）。用法：

    python tools/check_dashboard_js.py
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "src" / "dashboard.py"

BLOCK = re.compile(r"<script(?:\s[^>]*)?>(.*?)</script>", re.DOTALL)


def main() -> int:
    text = DASHBOARD.read_text(encoding="utf-8")
    blocks = BLOCK.findall(text)
    if not blocks:
        print("没找到任何 <script> 段 —— 面板结构变了？")
        return 1
    failures = 0
    with tempfile.TemporaryDirectory(prefix="pm_dash_js_") as tmp:
        for index, block in enumerate(blocks, start=1):
            # 去掉带 src= 的外链段（没有内容）
            if not block.strip():
                continue
            path = Path(tmp) / f"block{index}.js"
            path.write_text(block, encoding="utf-8")
            result = subprocess.run(
                ["node", "--check", str(path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            status = "OK" if result.returncode == 0 else "FAIL"
            print(f"  <script> #{index}（{len(block)} 字符）: {status}")
            if result.returncode != 0:
                failures += 1
                print(result.stderr.strip()[:2000])
    print(f"共 {len(blocks)} 段，失败 {failures} 段")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
