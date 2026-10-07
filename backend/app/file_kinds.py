"""文件类型判定(二进制识别的单一事实来源)

动机:sandbox 模式读文件走 shell 文本通道(`wc -l` + `awk`),二进制文件的原始
字节会被当成文本一路返回到前端渲染成乱码,同时污染智能体的 read_file 上下文;
而 local 模式与上传回退路径各有一套"能不能显示"的判定口径,三处不一致。
本模块把判定收敛成一套,供 sandbox_tools 与 routers/workspace 共用。

口径(刻意**不做** UTF-8 有效性判定):
- 后缀命中 BINARY_SUFFIXES → 二进制(零 IO,最先判)
- 文件头部含 NUL 字节 → 二进制
- 其余按文本处理

不判 UTF-8 有效性的原因:GBK 文本(中文 Windows 上很常见)会被误判成二进制而
无法预览;代价是这类文件在查看器里以替换字符呈现,而非彻底不可读。
"""
from pathlib import PurePosixPath

# 二进制后缀表(小写含点)。前端 frontend/src/utils/fileKind.ts 有同表镜像,
# 改这里必须同步改那里 —— 前端据它决定"要不要发起内容请求"。
BINARY_SUFFIXES: frozenset[str] = frozenset({
    # 办公文档(docx/xlsx 是 zip 容器,pdf 是二进制流,均无在线预览能力)
    ".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".ppt", ".pptx", ".pdf",
    # 压缩包 / 归档
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
    # 图片
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tiff",
    # 音视频
    ".mp3", ".mp4", ".avi", ".mov", ".wav", ".flac", ".ogg",
    # 可执行 / 编译产物
    ".exe", ".dll", ".so", ".dylib", ".jar", ".war", ".class", ".pyc",
    ".o", ".a", ".obj", ".lib", ".bin",
    # 字体 / 数据库 / git 对象
    ".ttf", ".otf", ".woff", ".woff2", ".sqlite", ".db", ".pack", ".idx",
    # 镜像
    ".iso", ".dmg",
})

# 下载拒绝清单:路径任一段命中 DENY_DIR_NAMES,或文件名/后缀命中下面两组。
# .git 必须拒绝 —— 带 token 的 clone URL 会落进 .git/config(`https://x-access-token:{token}@host`),
# "下载工作区任意文件"若不做限制就等于把用户 OAuth 凭证一键发出去。
DENY_DIR_NAMES: frozenset[str] = frozenset({".git"})
DENY_FILE_NAMES: frozenset[str] = frozenset({"id_rsa", "id_dsa", ".netrc", ".git-credentials"})
DENY_FILE_SUFFIXES: frozenset[str] = frozenset({".pem", ".key", ".p12", ".pfx"})

# 二进制文件的占位正文(三处读取路径共用,前端按 binary 标记决定不渲染它)
BINARY_PLACEHOLDER = "(二进制文件,无法显示)"

# NUL 探测读取窗口(字节)。与 sandbox 模式 shell 侧 `head -c` 保持同一口径。
NUL_PROBE_BYTES = 8192


def file_suffix(path: str) -> str:
    """取小写后缀(含点);无后缀返回空串。用 PurePosixPath 兼容两种模式的路径写法。"""
    suffix = PurePosixPath(str(path).replace("\\", "/")).suffix
    return suffix.lower() if suffix else ""


def is_likely_binary(path: str) -> bool:
    """仅按后缀判定二进制(零 IO)。命中即可直接跳过内容读取。"""
    return file_suffix(path) in BINARY_SUFFIXES


def has_nul_bytes(data: bytes, limit: int = NUL_PROBE_BYTES) -> bool:
    """头部窗口内含 NUL 字节即判二进制。空文件判文本(可预览的"空")。"""
    return b"\x00" in data[:limit]


def is_denied_download_path(path: str) -> bool:
    """下载拒绝清单:凭证/密钥类路径段命中返回 True。

    两点口径:
    - 按路径段逐段比对(不查子串),避免 `my.git-hooks/x.txt` 这类名字被误伤
    - **大小写不敏感**:local 模式跑在 Windows/macOS 的默认文件系统上(NTFS 不区分
      大小写、HFS+ 默认也不区分),`.GIT/config` 会解析到真 `.git/config`。
      只在 Linux 沙箱里按字面比才安全,而下载端点两种模式都要过
    """
    parts = PurePosixPath(str(path).replace("\\", "/")).parts
    lowered = [p.lower() for p in parts]
    for part in lowered[:-1]:
        if part in DENY_DIR_NAMES:
            return True
    name = lowered[-1] if lowered else ""
    if name in DENY_FILE_NAMES:
        return True
    return file_suffix(name) in DENY_FILE_SUFFIXES
