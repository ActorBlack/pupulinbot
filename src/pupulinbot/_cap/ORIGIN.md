# CAP / HashWX 来源

CAP 协议适配器、原生 ctypes 包装与 Python 对照实现来自用户提供的
`majsoul-analyzer`（下载目录 `code`，2026-11-05 版本），经用户要求移植。
保留算法源码；Python 导入路径调整为 `pupulinbot._cap`，原生库构建缓存
移到 `data/hashwx`（可用 `PUPULIN_HASHWX_CACHE_DIR` 调整），支持从当前
虚拟环境定位 CMake。

原生 HashWX 源码固定为 tevador/hashwx 的
`74b567a31276a4c5d7cb232a5b7639467f49f961`，采用 LGPL-3.0-or-later。
版权和许可证保留在 `vendor/hashwx/LICENSE`、`vendor/hashwx/UPSTREAM.md`
及 `vendor/hashwx-LGPL-3.0.txt`。采用动态库加载，允许通过配置替换兼容动态库。

入口为 `pupulinbot.cap_worker`，运行在有总超时限制的独立进程组。
仅明确的 `429 / x-cap-token-required` 可触发 CAP；普通限流遵循
`Retry-After`，401/403 不尝试求解、切镜像或更换身份。
CAP adapter 保留原项目对 instrumentation 的模拟，不代表站点授予无限访问权限。
长期部署可改用维护者授权的 API Key。
