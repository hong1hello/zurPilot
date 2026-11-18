"""便携包发布清单的签名与校验。

打包时给产物算一份 `release.json`：每个文件的 SHA-256、汇总哈希、以及用私钥签出的
Ed25519 签名。用户拿到包后（或启动器在解压后）可以用内置公钥验一次，确认这包确实是
从我们的构建机出来的、且在传输过程中没被改过。

签名对象不是整个 JSON，而是其中 `release` 字段的**规范序列化字节**——这样 `signature`
字段本身可以自由追加，不影响验签，也不会因为换行/空格/键序导致签名失效。

规范化的三条约束（必须和签名端严格一致，改一条就等于换了协议）：
- `sort_keys=True`：键序固定，不受 dict 插入顺序影响
- `separators=(',', ':')`：不留任何多余空白
- `ensure_ascii=True`：非 ASCII 一律转义，避免不同平台写出不同字节

清单结构::

    {
      "release": {
        "schema": 1,
        "sequence": 1,
        "revision": "<40 位十六进制>",
        "dependencies": "<64 位十六进制>",
        "fileCount": 12345,
        "totalBytes": 755000000,
        "filesSha256": "<64 位十六进制>"
      },
      "signature": "<base64>"
    }
"""
import base64
import hashlib
import json
import os
import re
from pathlib import Path

SCHEMA = 1
MANIFEST_NAME = 'release.json'
SIGNATURE_NAME = 'release.sig'
# 便携包里的运行时和日志不参与签名：runtime 由构建机同一个 Python 版本生成，
# 日志/缓存每次启动都在变，签进去只会让每次校验都失败。
# 只放"确定不含合法文件"的目录名。这里按名字匹配，不区分层级，
# 所以像 dist 这种同名歧义的绝不能进来——frontend/dist 是必须签名的前端产物，
# 而根目录的 dist/ 是 Python 构建残留。歧义交给 is_signable() 的白名单判定。
EXCLUDE_DIRS = frozenset({
    'runtime', 'log', 'cache', 'screenshots', 'node_modules', '.git',
    '__pycache__', '.ruff_cache', '.uv-cache', 'config',
    # 虚拟环境：在仓库根直接签名时会撞上，2 万多个文件，必须排除
    '.venv', 'venv', 'env', '.tox',
    # 以下既不进发布包、也不该参与签名，否则清单会随着开发机的本地状态变化：
    'tests', 'docs', 'dev_tools', '.github', '.pytest_cache',
    # 事务更新的工作目录（缓存裸仓库、ready-* 候选版、journal）
    '.azurpilot',
    # 回滚留下的崩溃现场：它是用户本地的运行痕迹，不同机器内容不同
    'config-after-failed-update',
    # 历史数据备份目录
    'AzurPilot_Data_Backup',
})
EXCLUDE_NAMES = frozenset({
    MANIFEST_NAME, SIGNATURE_NAME, 'probe-summary.json',
    'README.txt', 'AzurPilot.bat', 'PROBE-FAILED.txt',
    # 仓库元文件：不随包分发，也不该进签名
    '.gitignore', '.gitattributes', '.gitmodules', '.dockerignore',
    'AGENTS.md', 'CLAUDE.md', 'Makefile', 'Dockerfile', 'docker-compose.yml',
})

# 参与签名的顶层目录与根文件。
#
# 这份名单必须与 deploy/independent.py 的 APP_DIRS / APP_FILES 完全一致，
# 因为两边分别决定「签名范围」与「可更新范围」。错位会导致：
# - 签名多出来的文件：被篡改也校验不出来（更新不会碰它，也没人比对）
# - 更新多出来的文件：清单里没有它，apply 的哈希比对会把它当"本机修改"而中止
# tests/test_manifest_scope.py 里有断言强制两者相等。
#
# 光靠 EXCLUDE_DIRS 的目录黑名单做不到这件事：frontend/ 下只有 dist/ 该签，
# src/ 与 e2e/ 不该签，黑名单无法表达"某目录下只收一个子目录"。
SIGNABLE_DIRS = ('assets', 'bin', 'campaign', 'deploy', 'module', 'scripts', 'submodule', 'licenses')
SIGNABLE_ROOT_FILES = (
    'alas.py', 'gui.py', 'mcp_server_sse.py', 'tui.py',
    'pyproject.toml', 'uv.lock', 'LICENSE',
)


def is_signable(name):
    """判断一个相对路径是否参与签名。

    与 `deploy.independent.app_path()` 同语义，但不 import 那边的模块——
    签名工具要在打包机上独立可用，不该被 deploy.utils / uv 之类的依赖拖住。

    Args:
        name (str): POSIX 相对路径。

    Returns:
        bool: 参与签名返回 True。
    """
    parts = name.split('/')
    if len(parts) == 1:
        return name in SIGNABLE_ROOT_FILES
    head = parts[0]
    if head == 'frontend':
        # 只要构建产物，源码与 e2e 用例不进包
        return len(parts) > 2 and parts[1] == 'dist'
    if head == 'config':
        # 用户配置不签名；模板由代码生成，也不需要靠清单守护
        return False
    return head in SIGNABLE_DIRS

_HEX40 = re.compile(r'[0-9a-f]{40}')
_HEX64 = re.compile(r'[0-9a-f]{64}')

# cryptography 是否可用。None 表示还没探测过。
# 优先用它，是因为它在打包机上一定有（uv 的依赖图带进来的）；
# 回退到 pycryptodome 是为了便携包侧——它是 pyproject 的直接依赖，必然存在。
_CRYPTOGRAPHY = None


def canonical(value):
    """把对象序列化成签名用的规范字节。

    Args:
        value: 可 JSON 序列化的对象。

    Returns:
        bytes: 键序固定、无空白、纯 ASCII 的 JSON 字节。
    """
    return json.dumps(
        value, sort_keys=True, separators=(',', ':'), ensure_ascii=True
    ).encode('ascii')


def digest(path, chunk=1024 * 1024):
    """按块计算文件的 SHA-256，避免把大文件整个读进内存。

    Args:
        path (str | Path): 目标文件。
        chunk (int): 每次读取的字节数。

    Returns:
        str: 64 位小写十六进制摘要。
    """
    hasher = hashlib.sha256()
    with open(path, 'rb') as file:
        while True:
            block = file.read(chunk)
            if not block:
                break
            hasher.update(block)
    return hasher.hexdigest()


def dependency_id(root):
    """依赖组合指纹：uv.lock + pyproject.toml。

    用这个而不是逐个包算，是因为真正的语义是"运行时装的是不是这一版依赖描述对应的东西"。
    两者任一变化都意味着需要重新 sync，此时不该让旧清单继续通过。

    Args:
        root (str | Path): 项目根目录。

    Returns:
        str: 64 位十六进制摘要。

    Raises:
        FileNotFoundError: 缺少 uv.lock 或 pyproject.toml。
    """
    root = Path(root)
    hasher = hashlib.sha256()
    for name in ('uv.lock', 'pyproject.toml'):
        path = root / name
        if not path.is_file():
            raise FileNotFoundError(f'缺少 {path}')
        hasher.update(path.read_bytes())
        # 用 \0 分隔，防止 (a='x', b='y') 和 (a='xy', b='') 撞同一个摘要。
        hasher.update(b'\0')
    return hasher.hexdigest()


def iter_files(root):
    """遍历需要签名的文件，顺序稳定（按 POSIX 相对路径排序）。

    Args:
        root (str | Path): 待签目录。

    Yields:
        Path: 参与签名的文件绝对路径。
    """
    root = Path(root)
    collected = []
    for current, dirs, names in os.walk(root):
        # 原地过滤，walk 就不会进这些目录，省掉大量 stat。
        # 真正的判定在下面的 is_signable()，这里只是剪枝加速。
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for name in names:
            if name in EXCLUDE_NAMES:
                continue
            path = Path(current) / name
            if path.is_symlink():
                # 符号链接的内容取决于链接目标，同一份包在不同机器上会算出不同哈希。
                continue
            if not is_signable(path.relative_to(root).as_posix()):
                continue
            collected.append(path)
    collected.sort(key=lambda p: p.relative_to(root).as_posix())
    for path in collected:
        yield path


def build(root):
    """扫描目录，产出清单内容（不含签名）。

    Args:
        root (str | Path): 便携包根目录。

    Returns:
        dict: `release` 字段的内容。

    Raises:
        FileNotFoundError: 缺少 uv.lock 或 pyproject.toml。
    """
    root = Path(root)
    hasher = hashlib.sha256()
    count = 0
    total = 0
    for path in iter_files(root):
        relative = path.relative_to(root).as_posix()
        file_digest = digest(path)
        size = path.stat().st_size
        # 汇总哈希的输入也走规范化格式，保证跨平台一致。
        hasher.update(canonical({'path': relative, 'sha256': file_digest, 'size': size}))
        count += 1
        total += size
    return {
        'schema': SCHEMA,
        'sequence': 1,
        'revision': '0' * 40,
        'dependencies': dependency_id(root),
        'fileCount': count,
        'totalBytes': total,
        'filesSha256': hasher.hexdigest(),
    }


def sign(payload, private_key_path, password=None):
    """用 Ed25519 私钥给 `release` 字段签名。

    Args:
        payload (dict): `release` 字段内容。
        private_key_path (str | Path): PEM 格式的 Ed25519 私钥。
        password (bytes | None): 私钥口令，无口令传 None。

    Returns:
        str: base64 编码的签名。

    Raises:
        ImportError: 两个后端都不可用。
    """
    key_data = Path(private_key_path).read_bytes()
    message = canonical(payload)

    if _has_cryptography():
        from cryptography.hazmat.primitives import serialization

        private_key = serialization.load_pem_private_key(key_data, password=password)
        return base64.b64encode(private_key.sign(message)).decode('ascii')

    # 回退到 pycryptodome。它是 pyproject 的直接依赖，便携包里一定有，
    # 而 cryptography 只是间接依赖，用户机器上不保证。
    from Crypto.PublicKey import ECC
    from Crypto.Signature import eddsa

    if password:
        private_key = ECC.import_key(key_data, passphrase=password)
    else:
        private_key = ECC.import_key(key_data)
    signature = eddsa.new(private_key, 'rfc8032').sign(message)
    return base64.b64encode(signature).decode('ascii')


def _has_cryptography():
    """cryptography 是否可用（只探测一次，避免反复 import）。"""
    global _CRYPTOGRAPHY
    if _CRYPTOGRAPHY is None:
        try:
            import cryptography  # noqa: F401
            _CRYPTOGRAPHY = True
        except ImportError:
            _CRYPTOGRAPHY = False
    return _CRYPTOGRAPHY


def verify(payload, signature, public_key_path):
    """用 Ed25519 公钥验证签名。

    Args:
        payload (dict): `release` 字段内容。
        signature (str): base64 编码的签名。
        public_key_path (str | Path): PEM 格式的 Ed25519 公钥。

    Returns:
        bool: 验签通过返回 True；格式不对或验签失败返回 False，不抛异常。
    """
    try:
        key_data = Path(public_key_path).read_bytes()
        raw_signature = base64.b64decode(signature, validate=True)
        message = canonical(payload)
    except (ValueError, OSError, TypeError):
        return False

    if _has_cryptography():
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization

        try:
            public_key = serialization.load_pem_public_key(key_data)
            public_key.verify(raw_signature, message)
            return True
        except (InvalidSignature, ValueError, TypeError):
            return False

    # 回退到 pycryptodome：eddsa.verify 失败抛 ValueError
    from Crypto.PublicKey import ECC
    from Crypto.Signature import eddsa

    try:
        public_key = ECC.import_key(key_data)
        eddsa.new(public_key, 'rfc8032').verify(message, raw_signature)
        return True
    except (ValueError, TypeError):
        return False


def validate(payload):
    """校验清单字段格式，返回缺失或不合法项的描述。

    Args:
        payload (dict): `release` 字段内容。

    Returns:
        list[str]: 问题描述列表；空列表表示格式合规。
    """
    problems = []
    if not isinstance(payload, dict):
        return ['release 不是对象']
    if payload.get('schema') != SCHEMA:
        problems.append(f'schema 应为 {SCHEMA}，实际 {payload.get("schema")!r}')
    if not isinstance(payload.get('sequence'), int):
        problems.append('sequence 必须是整数')
    if not _HEX40.fullmatch(str(payload.get('revision', ''))):
        problems.append('revision 必须是 40 位十六进制')
    if not _HEX64.fullmatch(str(payload.get('filesSha256', ''))):
        problems.append('filesSha256 必须是 64 位十六进制')
    if not _HEX64.fullmatch(str(payload.get('dependencies', ''))):
        problems.append('dependencies 必须是 64 位十六进制')
    return problems


def write_manifest(root, payload, signature):
    """把清单写成 `release.json` + `release.sig`。

    Args:
        root (str | Path): 便携包根目录。
        payload (dict): `release` 字段内容。
        signature (str): base64 签名。
    """
    root = Path(root)
    document = {'release': payload, 'signature': signature}
    target = root / MANIFEST_NAME
    # 先写临时文件再替换，避免中途断电留下半个 JSON。
    temporary = target.with_suffix('.json.tmp')
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
    )
    os.replace(temporary, target)
    (root / SIGNATURE_NAME).write_text(signature + '\n', encoding='utf-8')


def read_manifest(root):
    """读取 `release.json`。

    Args:
        root (str | Path): 便携包根目录。

    Returns:
        dict | None: 解析成功返回文档，失败返回 None。
    """
    path = Path(root) / MANIFEST_NAME
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
