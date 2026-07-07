"""支持 ``python -m riskguard`` 调用命令行工具。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
