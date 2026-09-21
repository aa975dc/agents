"""Path identity and the unified sensitive-path policy (stdlib only)."""
from pathlib import Path

# 统一敏感清单 = 原 core.sensitive（core.py:117-121，项目快照/可写范围默认排除）
# ∪ 原 archives._validate_path（archives.py:152-157，显式纳管必须拒绝）。取并集后：
# core 侧排除范围是旧清单的超集（更保守），archives 侧拒绝范围与旧清单完全一致
# （core 独有条目均已被 archives 的前缀规则覆盖）。两组语义注释都保留。
SENSITIVE_NAMES = frozenset({
    # core 语义：已知凭据文件名（.env 变体另由前缀覆盖）
    ".env", "id_rsa", "id_ed25519", "credentials.json", ".npmrc", ".pypirc",
    # archives 语义：内部目录与凭据目录/配置，按任意路径段匹配
    ".git", ".dev-companion", ".ssh", ".aws", ".gnupg", ".kube", ".docker",
    ".netrc", ".git-credentials", ".dockercfg", "auth.json",
})
SENSITIVE_PREFIXES = (
    # core 语义：.env 变体（旧 ".env." 前缀被 ".env" 前缀覆盖）
    ".env",
    # archives 语义：私钥/凭据/服务账号族（覆盖 core 的 id_rsa/id_ed25519/credentials.json 精确名）
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials", "secrets", "service-account",
)
SENSITIVE_SUFFIXES = (
    # core 语义：证书/密钥后缀
    ".pem", ".key", ".p12", ".pfx",
    # archives 语义：密钥库/钥匙串
    ".keystore", ".keychain",
)

# core 语义：项目快照遍历时整体跳过的目录（archives 不使用）。
EXCLUDED_DIRS = {".git", ".dev-companion", "node_modules", ".venv", "venv", "__pycache__",
                 ".pytest_cache", ".mypy_cache", "dist", "build", ".next"}


def sensitive(name):
    name = name.lower()
    return (name in SENSITIVE_NAMES or name.startswith(SENSITIVE_PREFIXES) or
            name.endswith(SENSITIVE_SUFFIXES))


def absolute(path):
    return Path(path).absolute()


def realpath(path):
    return Path(path).resolve()


def inside(path, directory):
    path, directory = Path(path), Path(directory)
    return path == directory or directory in path.parents
