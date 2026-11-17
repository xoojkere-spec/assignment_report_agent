"""让测试启动的 Python 子进程在导入应用前安装相同保护。"""

import os
import sys

try:
    from _test_isolation import install

    install()
except Exception:
    # Python 默认忽略 sitecustomize 异常；测试保护失效时必须直接停止。
    sys.stderr.write("测试进程无法安装环境保护\n")
    os._exit(98)
