"""让 pytest 无需安装即可导入 src 布局下的 riskguard 包。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
