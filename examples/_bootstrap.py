"""让 examples/ 下的脚本无需安装即可直接 `python examples/xx.py` 运行。

正式使用请 `pip install riskguard`,就不需要这段引导了。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
