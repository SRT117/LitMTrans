"""LitMTrans 应用入口。"""

import os

# 设为 True 可用“新用户”状态启动：不读取或改写本机已有的设置和密钥。
# 调试期间新填的设置与密钥只在当前进程有效，退出后会自动丢弃。
FRESH_USER_DEBUG = False

if FRESH_USER_DEBUG:
    os.environ["LITMTRANS_FRESH_USER_DEBUG"] = "1"

from OT_ui import *


if __name__ == "__main__":
    main()
