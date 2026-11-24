"""便携版的事务式更新：准备、安装、确认 / 回滚。

与 `deploy/installer.py` 的区别
    `installer.py` 是 ALAS 那套「git pull + pip install + 重启」——没有事务，
    中途失败会留下半新半旧的目录。这个模块装的是**自包含便携包**：runtime 不动，
    只替换应用文件，且每一步都有回退点。

三段事务
    check()   → 比对远端 revision 与本地 RECEIPT，返回待更新项
    prepare() → 下载、解包、校验、**真起一次候选版**，落到 ready-*/（不动现有文件）
    apply()   → 备份 config 与旧文件，替换，写 journal(installing → trial)
    confirm() → 试运行成功，删 journal 与备份
    recover() → 试运行失败或断电，按 journal 回滚

    任何一步失败都不会让用户停留在「打不开」的状态。apply() 内部异常直接
    recover() 再抛出；进程被杀则下次启动时 `python -m deploy.independent recover`
    从 journal 续上。

安全边界
    - 所有路径过 `app_path()` 白名单：拒绝绝对路径、盘符、`..`、Windows 保留名
    - 拒绝符号链接与 junction，防止解压时把文件写到目录外
    - 逐文件哈希比对：本机改过的文件不会被静默覆盖，直接中止
    - config/ 用 SQLite 一致快照备份，不复制 WAL 边车文件
    - 依赖指纹锁死：uv.lock 或 pyproject.toml 变了就不许用增量更新

为什么不签 runtime
    runtime 由打包机同版本 Python 生成，1GB 且每次构建都一样。签它会让包大一倍、
    校验慢几倍，收益为零。守护它的是 `dependency_id`——依赖描述一变就拒绝增量更新。
"""
import hashlib
import json
import ntpath
import os
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

# 独立模式环境变量。设置后业务侧可以据此跳过联网上报等副作用。
ENV_FLAG = 'AZURPILOT_INDEPENDENT'

# 应用文件白名单。只有这些会被更新替换，其余（runtime/log/cache）一律不碰。
# scripts 也在内：release_manifest_cli.py 是用户端自己验签的入口，
# 它必须能跟代码一起更新，也必须被签名保护——否则被改了也校验不出来。
APP_DIRS = ('assets', 'bin', 'campaign', 'deploy', 'module', 'scripts', 'submodule', 'licenses')
APP_FILES = (
    'alas.py', 'gui.py', 'mcp_server_sse.py', 'tui.py',
    'pyproject.toml', 'uv.lock', 'LICENSE',
)
# 安装状态目录。放应用根下，跟 config 同级，便于用户整体搬走。
RECEIPT = Path('.azurpilot/independent.json')
FILES = Path('.azurpilot/independent-files.json')
WORK = Path('.azurpilot/independent-update')

# 单次下载上限。发布清单是几百字节，前端包 40MB 足够。
MAX_MANIFEST_SIZE = 64 * 1024
MAX_FRONTEND_SIZE = 40 * 1024 * 1024
# 源码压缩包上限（预留 200MB，够放下所有 campaign 图片定义）
MAX_SOURCE_SIZE = 200 * 1024 * 1024

# git 调用超时。fetch 在慢网下可能要几分钟，但必须有上界。
GIT_TIMEOUT = 210
# ls-remote 的超时。不能设太紧：国内直连 GitHub 的 HTTPS 握手就要 20 秒上下，
# 实测从国内一次完整的 ls-remote 需要 21 秒（含 21 秒才报 could not connect）。
# 15 秒会把「网络慢」误判成「分支不存在」，用户看到的是莫名其妙的更新失败。
GIT_SHORT_TIMEOUT = 45

_HEX40 = re.compile(r'[0-9a-f]{40}')
_HEX64 = re.compile(r'[0-9a-f]{64}')
_READY_NAME = re.compile(r'ready-[a-z0-9_]+')

CREATE_FLAGS = 0
if os.name == 'nt':
    CREATE_FLAGS = subprocess.CREATE_NO_WINDOW


def enabled():
    """当前进程是否运行在独立（便携）模式下。

    Returns:
        bool: 环境变量 AZURPILOT_INDEPENDENT 为 '1' 时返回 True。
    """
    return os.environ.get(ENV_FLAG) == '1'


def canonical(value):
    """序列化为签名与哈希共用的规范字节。

    Args:
        value: 可 JSON 序列化的对象。

    Returns:
        bytes: 键序固定、无空白、纯 ASCII 的 JSON 字节。
    """
    return json.dumps(
        value, sort_keys=True, separators=(',', ':'), ensure_ascii=True
    ).encode('ascii')


def digest(path):
    """计算文件的 SHA-256。

    Args:
        path (str | Path): 目标文件。

    Returns:
        str: 64 位小写十六进制摘要。
    """
    with Path(path).open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def dependency_id(root):
    """依赖组合指纹：uv.lock + pyproject.toml。

    这是守护 runtime 的那把锁。不看单个包，只看「依赖描述」——它变了就必须
    换完整安装包，不能拿旧 runtime 硬跑新代码。

    Args:
        root (str | Path): 应用根目录。

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
        # 用 \0 分隔，防止相邻字段拼接后撞同一个摘要
        hasher.update(b'\0')
    return hasher.hexdigest()


def read_json(path):
    """读取 JSON 文件。

    Args:
        path (str | Path): 文件路径。

    Returns:
        Any: 解析结果。
    """
    return json.loads(Path(path).read_text(encoding='utf-8'))


def replace_dir(src, dst, attempts=15, delay=0.2):
    """原子替换目录，遇到占用时退避重试。

    为什么要重试：
    Windows 上 os.replace 对目录会因句柄未释放而抛 PermissionError(WinError 32)。
    常见占用来源都是短暂的——刚被 kill 的探活子进程、杀毒软件实时扫描、
    Windows 索引服务。这类占用通常几百毫秒到几秒就没了，
    直接失败会让用户看到"更新失败"却查不出原因。

    重试间隔递增（0.2s、0.5s、0.8s…），累计约 20 秒，足够覆盖扫描类占用。

    Args:
        src (Path): 源目录。
        dst (Path): 目标路径，必须不存在。
        attempts (int): 最大尝试次数。
        delay (float): 首次重试间隔，后续递增。

    Raises:
        PermissionError: 全部重试都失败时抛出最后一次异常。
    """
    src, dst = Path(src), Path(dst)
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay * (1 + attempt * 0.5))


def write_json(path, value):
    """原子写入 JSON：临时文件 + fsync + os.replace。

    中途断电要么是旧内容、要么是新内容，不会出现半个 JSON。
    这在 journal 与 RECEIPT 上尤其重要——读到半个文件就没法回滚了。

    Args:
        path (str | Path): 目标路径。
        value: 可 JSON 序列化的对象。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(dir=path.parent, delete=False)
    temporary = Path(handle.name)
    try:
        handle.write(canonical(value))
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temporary, path)
    except BaseException:
        handle.close()
        temporary.unlink(missing_ok=True)
        raise


def app_path(name, *, frontend=False, build_frontend=False):
    """校验一个相对路径是否属于可更新的应用文件。

    这是整个模块最关键的防线。解压、备份、恢复三条路径都过它。
    拒绝的东西：绝对路径、盘符、反斜杠、`..`、尾部点或空格、Windows 非法字符、
    Windows 保留名（CON/PRN/AUX/NUL/COM1…）。

    Args:
        name (str): 待校验的相对路径（POSIX 分隔符）。
        frontend (bool): 为 True 时只接受 frontend/dist/ 下的文件。
        build_frontend (bool): 为 True 时只接受 frontend/ 下的文件。

    Returns:
        bool: 合规返回 True。
    """
    posix = PurePosixPath(name)
    win = PureWindowsPath(name)
    if (
        posix.as_posix() != name
        or posix.is_absolute()
        or win.drive
        or '\\' in name
        or not posix.parts
        or '..' in posix.parts
    ):
        return False
    for part in posix.parts:
        if part.endswith(('.', ' ')):
            return False
        if any(char in part for char in '<>:"|?*'):
            return False
        if ntpath.isreserved(part):
            return False
        # 字节码缓存绝不能进更新链路。
        # 签名侧（release_manifest.EXCLUDE_DIRS）早就排除了 __pycache__，
        # 但解包侧曾经放行，于是更新包里一旦混进 .pyc，apply 逐文件比对
        # 就会发现"本地没有这个文件"而判定本机修改，整个更新被挡下来。
        # .pyc 与解释器版本、源码路径强绑定，本来也不该跨机器传输。
        if part == '__pycache__':
            return False
        if part.endswith(('.pyc', '.pyo')):
            return False

    if frontend:
        return len(posix.parts) > 2 and posix.parts[:2] == ('frontend', 'dist')
    if build_frontend:
        return len(posix.parts) > 1 and posix.parts[0] == 'frontend'

    if len(posix.parts) == 1:
        return name in APP_FILES
    if posix.parts[0] == 'config':
        # config 整个目录都不由更新包修改。
        #
        # 曾经放行过 template.json / deploy.template.yaml，想着"模板要跟着版本更新"，
        # 但那和签名范围直接冲突：config 在 release_manifest 的 EXCLUDE_DIRS 里
        # （用户配置不该被校验），于是 files.json 里没有这些模板，
        # apply 逐文件比对时必然判定"本机修改"而中止整个更新——
        # 结果是"可更新但永远更新不了"。
        #
        # 模板本来也不需要走更新包：config_update() 会按 schema 补齐新字段。
        return False
    if posix.parts[0] == 'frontend':
        # 只放行构建产物 frontend/dist/**。
        # 不能整段收下 frontend/：那里有源码和 node_modules，几万个文件，
        # 一旦进了更新包，apply 会逐文件哈希比对，慢到不可用。
        return len(posix.parts) > 2 and posix.parts[1] == 'dist'
    return posix.parts[0] in APP_DIRS


def check_target(root, name):
    """把应用文件路径解析成绝对路径，并确认它不是链接。

    符号链接/junction 会让写入落到目录树之外，必须在替换前拦住。

    Args:
        root (str | Path): 应用根目录。
        name (str): 相对路径。

    Returns:
        Path: 合法的绝对路径。

    Raises:
        ValueError: 路径不合规或经过链接。
    """
    if not (app_path(name) or app_path(name, frontend=True)):
        raise ValueError(f'更新包路径无效 {name}')
    path = Path(root) / name
    if path.resolve() != path or path.is_symlink() or _is_junction(path):
        raise ValueError(f'应用文件不能通过链接更新 {name}')
    return path


def _is_junction(path):
    """判断路径是否为 Windows 目录 junction。

    Args:
        path (str | Path): 目标路径。

    Returns:
        bool: 是 junction 返回 True；非 Windows 或非目录返回 False。
    """
    if os.name != 'nt':
        return False
    path = Path(path)
    if not path.is_dir():
        return False
    try:
        return bool(path.stat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (OSError, AttributeError):
        return False


def unpack(archive, target, *, frontend=False, build_frontend=False):
    """把 zip 解到目标目录，逐文件计算哈希。

    三道防线：路径白名单、大小写重复检测、拒绝符号链接成员。
    大小写重复在 Windows 上会互相覆盖（`Foo.py` 与 `foo.py` 同文件），
    解压时发现就必须报错，不能静默取其一。

    Args:
        archive (str | Path): zip 路径。
        target (str | Path): 解压目标目录。
        frontend (bool): 只解 frontend/dist/ 下的文件。
        build_frontend (bool): 只解 frontend/ 下的文件。

    Returns:
        dict[str, str]: 相对路径 -> SHA-256。

    Raises:
        ValueError: 包内出现重复路径、链接或越界路径。
    """
    target = Path(target)
    hashes = {}
    seen = set()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            if member.is_dir():
                continue
            name = member.filename
            if not app_path(name, frontend=frontend, build_frontend=build_frontend):
                continue
            folded = name.casefold()
            if folded in seen:
                raise ValueError(f'更新包包含重复路径 {name}')
            if stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError(f'更新包包含链接 {name}')
            seen.add(folded)
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            # 'xb' 独占创建：同名文件已存在就失败，避免覆盖上一次的残留
            with bundle.open(member) as source, path.open('xb') as output:
                shutil.copyfileobj(source, output)
            hashes[name] = digest(path)
    return hashes


def fetch(url, limit):
    """带 UA 与大小上限的 HTTP 下载。

    Args:
        url (str): 下载地址。
        limit (int): 允许的最大字节数。

    Returns:
        bytes: 响应正文。

    Raises:
        ValueError: 超过大小上限。
        OSError: 网络错误。
    """
    import urllib.request

    request = urllib.request.Request(
        url, headers={'User-Agent': 'AzurPilot-Independent/1'}
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError('发布文件超过预期大小')
    return body


def update_source(root):
    """读取并校验更新源配置。

    只接受不含账号令牌的 HTTPS 地址，防止凭据被写进 deploy.yaml 后随配置外泄。

    Args:
        root (str | Path): 应用根目录。

    Returns:
        tuple[str, str]: (仓库地址, 分支名)。

    Raises:
        ValueError: 地址或分支不合法。
    """
    from deploy.utils import poor_yaml_read

    root = Path(root)
    values = poor_yaml_read(root / 'config/deploy.yaml')
    repository = str(values.get('Repository') or '').strip().strip('"')
    branch = str(values.get('Branch') or 'master').strip().strip('"')

    if not repository:
        raise ValueError('未配置更新仓库，请填写 Repository（https:// 开头的 Git 地址）')

    url = urlsplit(repository)
    if (
        url.scheme != 'https'
        or not url.hostname
        or url.username is not None
        or any(char in repository for char in ('?', '#', '%'))
        or not url.path.strip('/')
        or re.search(r'[\s\\]', repository)
    ):
        raise ValueError('更新仓库需要使用不带账号或令牌的 HTTPS Git 地址')
    try:
        _ = url.port
    except ValueError as exc:
        raise ValueError('更新仓库的端口无效') from exc

    if (
        not re.fullmatch(r'[A-Za-z0-9_][\w./-]*', branch, flags=re.ASCII)
        or '..' in branch
        or any(
            not part or part.startswith('.') or part.endswith(('.', '.lock'))
            for part in branch.split('/')
        )
    ):
        raise ValueError('更新分支名称无效')
    return repository.rstrip('/'), branch


def _git_executable(root):
    """定位 git.exe。

    Args:
        root (str | Path): 应用根目录。

    Returns:
        str: 可执行文件路径。
    """
    candidate = Path(root) / 'runtime/git/cmd/git.exe'
    if candidate.is_file():
        return str(candidate)
    # 便携包里不带 git 时回退到系统 git；找不到会在 subprocess 层抛错
    return 'git'


def git(root, cwd, *args, stdout=subprocess.DEVNULL, timeout=GIT_TIMEOUT):
    """执行 git 命令，带完整的环境隔离与超时回杀。

    环境隔离是必须的：宿主的 GIT_DIR / GIT_WORK_TREE / 全局 credential.helper
    会让命令跑到别的仓库或弹出凭据框。超时后必须杀进程树——MinGit 拉起的
    HTTPS 与解包子进程会持有管道，只杀父进程会挂住。

    Args:
        root (str | Path): 应用根目录。
        cwd (str | Path): 工作目录。
        *args: git 子命令与参数。
        stdout: stdout 处理方式，默认丢弃；传 PIPE 时收集。
        timeout (int): 超时秒数。

    Returns:
        subprocess.CompletedProcess: 已校验返回码的结果。

    Raises:
        subprocess.CalledProcessError: git 返回非零。
        subprocess.TimeoutExpired: 超时。
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(
        GIT_TERMINAL_PROMPT='0',
        GIT_CONFIG_NOSYSTEM='1',
        GIT_CONFIG_GLOBAL=os.devnull,
    )
    command = [
        _git_executable(root),
        '-c', 'http.sslVerify=true',
        '-c', 'credential.helper=',
        '-c', 'protocol.allow=never',
        '-c', 'protocol.https.allow=always',
        '-c', 'http.lowSpeedLimit=1024',
        '-c', 'http.lowSpeedTime=20',
        *args,
    ]
    capture = stdout == subprocess.PIPE
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        child = subprocess.Popen(
            command, cwd=str(cwd), env=env, creationflags=CREATE_FLAGS,
            stdout=output if capture else stdout, stderr=errors,
        )
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(child)
            raise
        output.seek(0)
        errors.seek(0)
        result = subprocess.CompletedProcess(
            command, child.returncode,
            output.read() if capture else None, errors.read(),
        )
        result.check_returncode()
        return result


def _kill_tree(child):
    """超时后回收整个进程树。

    Args:
        child (subprocess.Popen): 子进程句柄。
    """
    if child.poll() is None and os.name == 'nt':
        taskkill = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/taskkill.exe'
        try:
            subprocess.run(
                [str(taskkill), '/PID', str(child.pid), '/T', '/F'],
                timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=CREATE_FLAGS,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    if child.poll() is None:
        child.kill()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _remote_revision(root, repository, branch):
    """用 ls-remote 取远端分支的提交号。

    Args:
        root (str | Path): 应用根目录。
        repository (str): 仓库地址。
        branch (str): 分支名。

    Returns:
        str: 40 位 commit sha。

    Raises:
        ValueError: 分支不存在或返回格式异常。
    """
    ref = f'refs/heads/{branch}'
    listing = git(
        root, root, 'ls-remote', '--exit-code', '--refs', repository, ref,
        stdout=subprocess.PIPE, timeout=GIT_SHORT_TIMEOUT,
    ).stdout.decode('ascii', 'replace')
    for line in listing.splitlines():
        revision, _, name = line.partition('\t')
        if name == ref and _HEX40.fullmatch(revision):
            return revision
    raise ValueError('没有找到配置的更新分支')


def check(root=None):
    """检查是否有新版本。

    Args:
        root (str | Path, optional): 应用根目录，默认当前目录。

    Returns:
        dict | None: 有新版本时返回 {'revision': sha, 'repository': .., 'branch': ..}，
            否则 None。

    Raises:
        RuntimeError: 读不到远端分支。
    """
    root = Path(root or Path.cwd()).resolve()
    repository, branch = update_source(root)
    try:
        revision = _remote_revision(root, repository, branch)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise RuntimeError(f'无法读取远端分支，当前版本可以继续运行: {error}') from error

    receipt_path = root / RECEIPT
    current = read_json(receipt_path) if receipt_path.is_file() else {'revision': '0' * 40}
    if revision != current.get('revision'):
        return {'revision': revision, 'repository': repository, 'branch': branch}
    return None


def _transaction(root):
    """返回事务日志（journal）路径。

    Args:
        root (str | Path): 应用根目录。

    Returns:
        Path: journal 文件路径。
    """
    work = Path(root) / WORK
    if work.resolve() != work:
        raise ValueError('更新工作目录不能是链接')
    return work / 'transaction.json'


def _source_id(repository):
    """由仓库地址派生稳定的缓存标识。

    Args:
        repository (str): 仓库地址。

    Returns:
        str: 16 位十六进制。
    """
    return hashlib.sha256(repository.encode('utf-8')).hexdigest()[:16]


def _fetch_source(root, directory, offer):
    """拉取指定 revision 的源码，并用本地对象缓存做增量。

    缓存是一个裸仓库 `source-<id>.git`，用固定引用 SOURCE_REF 记住上次的提交。
    这样 git 在 fetch 时能协商已有对象，只传缺失的部分——首次全量，之后增量。

    Args:
        root (str | Path): 应用根目录。
        directory (Path): 工作目录（放缓存）。
        offer (dict): `check()` 的返回值。

    Returns:
        tuple[Path, str]: (缓存仓库路径, 已检出的 revision)。

    Raises:
        RuntimeError: 全部镜像都失败。
    """
    repository = offer['repository']
    revision = offer['revision']
    cache = Path(directory) / f'source-{_source_id(repository)}.git'

    if not cache.is_dir():
        # 首次建裸仓库。用 --bare 而不是普通克隆：不需要工作树，省一半空间。
        git(root, Path(directory), 'init', '--bare', '-q', str(cache))

    # 清理上次中断遗留的锁文件。只删锁，保留已下载对象——这正是增量的前提。
    for name in ('shallow.lock', 'index.lock', 'config.lock'):
        lock = cache / name
        if lock.exists():
            if lock.resolve() != lock:
                raise ValueError('源码缓存锁不能是链接')
            lock.unlink(missing_ok=True)

    # '+refs/heads/...' 让浅克隆的缓存引用可以被强制更新
    spec = f'+{revision}:refs/azurpilot/source'
    git(
        root, cache,
        '-c', 'protocol.version=2',
        'fetch', '--depth=1', '--no-tags', '--no-write-fetch-head',
        '--no-auto-maintenance', repository, spec,
        timeout=GIT_TIMEOUT,
    )
    return cache, revision


def _archive_source(root, cache, revision, archive):
    """从缓存仓库导出 zip。

    用 git archive 而不是复制工作树：只导出该 revision 的受控文件，
    天然排除 .gitignore 里的东西（node_modules、log、本地配置）。

    Args:
        root (str | Path): 应用根目录。
        cache (Path): 缓存裸仓库。
        revision (str): 提交号。
        archive (Path): 输出 zip 路径。

    Raises:
        RuntimeError: 导出失败或体积超限。
    """
    with archive.open('wb') as output:
        child = subprocess.Popen(
            [
                _git_executable(root), '-c', 'protocol.allow=never',
                '-c', 'protocol.https.allow=always',
                'archive', '--format=zip', revision,
            ],
            cwd=str(cache), stdout=output, stderr=subprocess.PIPE,
            creationflags=CREATE_FLAGS,
        )
        try:
            _, errors = child.communicate(timeout=GIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_tree(child)
            raise
    if child.returncode != 0:
        raise RuntimeError(
            f'导出源码失败: {errors.decode("utf-8", "replace")[:300]}'
        )
    size = archive.stat().st_size
    if size > MAX_SOURCE_SIZE:
        raise RuntimeError(f'源码包过大: {size} 字节')
    return archive


def _matching_frontend(root, offer, revision, directory):
    """尝试取与源码 revision 配套的预构建前端包。

    顺序：包内已带的 frontend/dist → 仓库内的 Release 附件 frontend.zip。
    都拿不到就返回 None，让调用方回退到源码构建。

    Args:
        root (str | Path): 应用根目录。
        offer (dict): `check()` 的返回值。
        revision (str): 提交号。
        directory (Path): 工作目录。

    Returns:
        Path | None: 可用的 zip 路径。
    """
    target = Path(directory) / 'frontend.zip'
    repository = offer['repository'].removesuffix('.git')
    # GitHub / GitCode 的 Release 资源命名约定不同，挨个试
    candidates = [
        f'{repository}/releases/download/{revision}/frontend.zip',
        f'{repository}/raw/{revision}/frontend.zip',
    ]
    for url in candidates:
        try:
            body = fetch(url, MAX_FRONTEND_SIZE)
        except (OSError, ValueError):
            continue
        if body[:2] != b'PK':
            continue
        target.write_bytes(body)
        try:
            with zipfile.ZipFile(target) as bundle:
                names = bundle.namelist()
        except zipfile.BadZipFile:
            target.unlink(missing_ok=True)
            continue
        if 'index.html' not in names and 'dist/index.html' not in names:
            target.unlink(missing_ok=True)
            continue
        return target
    return None


def _node_command(root):
    """定位内置 Node 与 npm。

    Args:
        root (str | Path): 应用根目录。

    Returns:
        tuple[str, list[str]] | None: (node 可执行文件, npm 启动参数) 或 None。
    """
    runtime = Path(root) / 'runtime'
    node = runtime / 'nodejs/node.exe'
    npm_cli = runtime / 'nodejs/node_modules/npm/bin/npm-cli.js'
    if node.is_file() and npm_cli.is_file():
        return str(node), [str(npm_cli)]
    return None


def _build_frontend(root, archive, directory, staged):
    """从源码构建前端（最后手段）。

    只在拿不到预构建包时才走这条路。npm ci 用锁文件精确还原，
    镜像优先 npmmirror，失败回退官方源。

    Args:
        root (str | Path): 应用根目录。
        archive (Path): 源码 zip。
        directory (Path): 工作目录。
        staged (Path): 候选应用目录。

    Returns:
        dict[str, str]: 构建产物的相对路径 -> 哈希。

    Raises:
        RuntimeError: 缺少 Node 或构建失败。
    """
    node = _node_command(root)
    if node is None:
        raise RuntimeError('缺少内置 Node，无法构建前端。请用带预构建网页包的发布')
    executable, npm_args = node

    build = Path(directory) / 'frontend-build'
    build.mkdir(parents=True, exist_ok=True)
    # 只解出 frontend/ 下的内容
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            if member.is_dir():
                continue
            name = member.filename
            if not app_path(name, build_frontend=True):
                continue
            path = build / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, path.open('xb') as output:
                shutil.copyfileobj(source, output)

    work = build / 'frontend'
    if not (work / 'package.json').is_file():
        raise RuntimeError('源码里没有 frontend/package.json，无法构建网页')

    env = dict(os.environ)
    env['PATH'] = os.pathsep.join([str(Path(executable).parent), env.get('PATH', '')])
    for registry in ('https://registry.npmmirror.com', 'https://registry.npmjs.org'):
        env['npm_config_registry'] = registry
        try:
            subprocess.run(
                [executable, *npm_args, 'ci', '--no-audit', '--no-fund'],
                cwd=str(work), env=env, check=True, timeout=600,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=CREATE_FLAGS,
            )
            subprocess.run(
                [executable, *npm_args, 'run', 'build'],
                cwd=str(work), env=env, check=True, timeout=600,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=CREATE_FLAGS,
            )
            break
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
    else:
        raise RuntimeError('前端构建失败（两个镜像都试过）')

    dist = work / 'dist'
    if not (dist / 'index.html').is_file():
        raise RuntimeError('前端构建没有产出 dist/index.html')

    hashes = {}
    for path in dist.rglob('*'):
        if not path.is_file():
            continue
        if path.is_symlink() or _is_junction(path):
            raise ValueError(f'前端产物包含链接 {path}')
        name = 'frontend/dist/' + path.relative_to(dist).as_posix()
        if not app_path(name, frontend=True):
            raise ValueError(f'前端产物路径无效 {name}')
        target = staged / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        hashes[name] = digest(target)
    return hashes


def _seed_probe_config(root, staged):
    """给候选目录补一份配置模板，只用于启动探活。

    更新不该碰用户配置，所以 config/ 不在 app_path 白名单里、也不进 files.json。
    但 create_app() 启动时要读 config/template.json，缺了它候选版起不来，
    探活就会把"缺模板"误判成"新版本不可用"。

    只复制模板文件，不碰 ap.json / 数据库——用户的真实配置一个字节都不动。

    Args:
        root (Path): 应用根目录，模板从这里读。
        staged (Path): 候选目录，模板补到这里。
    """
    source = Path(root) / 'config'
    if not source.is_dir():
        return
    target = Path(staged) / 'config'
    target.mkdir(exist_ok=True)
    for name in ('template.json', 'deploy.template.yaml'):
        origin = source / name
        if origin.is_file():
            shutil.copy2(origin, target / name)


def _frontend_for_commit(root, offer, revision, archive, directory, staged):
    """三级前端策略：包内预置 → 匹配发布包 → 源码构建。

    Args:
        root (str | Path): 应用根目录。
        offer (dict): `check()` 的返回值。
        revision (str): 提交号。
        archive (Path): 源码 zip。
        directory (Path): 工作目录。
        staged (Path): 候选应用目录。

    Returns:
        dict[str, str]: 新增文件的相对路径 -> 哈希。
    """
    # 一级：源码包里就带了 dist（打包时提交过）
    if (staged / 'frontend/dist/index.html').is_file():
        return {}

    # 二级：发布里附带的预构建包
    bundle = _matching_frontend(root, offer, revision, directory)
    if bundle is not None:
        with zipfile.ZipFile(bundle) as archive_file:
            names = archive_file.namelist()
        prefix = '' if 'index.html' in names else 'dist/'
        temporary = Path(directory) / 'frontend-unpack'
        temporary.mkdir(parents=True, exist_ok=True)
        hashes = {}
        with zipfile.ZipFile(bundle) as archive_file:
            for member in archive_file.infolist():
                if member.is_dir():
                    continue
                name = member.filename
                if not name.startswith(prefix):
                    continue
                relative = name[len(prefix):]
                target_name = 'frontend/dist/' + relative
                if not app_path(target_name, frontend=True):
                    continue
                path = staged / target_name
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive_file.open(member) as source, path.open('xb') as output:
                    shutil.copyfileobj(source, output)
                hashes[target_name] = digest(path)
        if (staged / 'frontend/dist/index.html').is_file():
            return hashes
        raise RuntimeError('网页包无效，当前版本可以继续运行')

    # 三级：源码构建
    return _build_frontend(root, archive, directory, staged)


def probe(root, staged, timeout=40.0):
    """真起一次候选版，验证它能对外服务。

    这是 prepare 的最后一道闸，也是最值钱的一道。双断言：
    `/healthz` 返回 200（ASGI 起来了）+ `/` 正文含 `<div id="root">`
    （前端真的挂上了）。只验前者发现不了「能启动但白屏」。

    Args:
        root (str | Path): 应用根目录（runtime 在这里）。
        staged (Path): 候选应用目录。
        timeout (float): 总等待秒数。

    Raises:
        RuntimeError: 启动失败或断言不通过。
    """
    import http.client

    root = Path(root)
    python = root / 'runtime/python/python.exe'
    if not python.is_file():
        # 便携包布局可能是 runtime/python.exe（无 python/ 子目录）
        alt = root / 'runtime/python.exe'
        if alt.is_file():
            python = alt
        else:
            raise RuntimeError(f'找不到解释器: {python}')

    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]

    command = [
        str(python), '-B', '-c',
        'import uvicorn; from module.api.app import create_app; '
        f"uvicorn.run(create_app(password='', manage_runtime=False, mount_mcp=False), "
        f"host='127.0.0.1', port={port})",
    ]
    env = dict(os.environ)
    for name in ('AZURPILOT_CONFIG_DIR', 'PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV'):
        env.pop(name, None)
    env.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1')
    env[ENV_FLAG] = '1'

    child = subprocess.Popen(
        command, cwd=str(staged), env=env, creationflags=CREATE_FLAGS,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if child.poll() is not None:
                errors = child.stderr.read().decode('utf-8', 'replace') if child.stderr else ''
                raise RuntimeError(f'候选版启动即退出: {errors[-400:]}')
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
            try:
                connection.request('GET', '/healthz')
                response = connection.getresponse()
                response.read()
                if response.status == 200:
                    connection.request('GET', '/')
                    response = connection.getresponse()
                    if response.status == 200 and b'<div id="root">' in response.read(16 * 1024):
                        return
            except OSError:
                pass
            finally:
                connection.close()
            time.sleep(0.2)
        raise RuntimeError('候选版未通过本机启动检查，继续使用当前版')
    finally:
        if child.poll() is None:
            _kill_tree(child)


# ── 备份与恢复 ──────────────────────────────────────────────────────────────

def backup_file(source, destination):
    """复制单个文件到备份位置。

    Args:
        source (str | Path): 源文件。
        destination (str | Path): 备份目标。
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def restore_file(source, destination):
    """原子地把备份文件还原到目标位置。

    先复制到同目录临时文件再 os.replace：中途断电不会留下半截文件，
    这对被替换的 .py 尤其重要——写了一半的源码没法 import。

    Args:
        source (str | Path): 备份文件。
        destination (str | Path): 还原目标。
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(dir=destination.parent, delete=False)
    temporary = Path(handle.name)
    handle.close()
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def backup_config(config, destination):
    """备份 config 目录，数据库用 SQLite 一致快照。

    直接复制 .db + -wal + -shm 会拿到撕裂状态（WAL 里还有未落盘的事务）。
    正确做法是复制除数据库外的所有文件，再用 sqlite3 的 backup API 取快照。

    Args:
        config (str | Path): config 目录。
        destination (str | Path): 备份目标。
    """
    config = Path(config)
    database = config / 'azurpilot.db'
    sidecars = {'azurpilot.db', 'azurpilot.db-wal', 'azurpilot.db-shm', 'azurpilot.db-journal'}

    def ignore_database(directory, names):
        # 只跳过 config 顶层的那一组数据库文件，子目录里的同名文件照常备份
        if Path(directory) == config and database.is_file():
            return sidecars.intersection(names)
        return ()

    destination = Path(destination)
    shutil.copytree(config, destination, ignore=ignore_database)
    if database.is_file():
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(str(destination / database.name))) as target:
                source.backup(target)


def _previous(root, record):
    """解析并校验 journal 里记录的备份目录。

    备份路径来自磁盘上的 JSON，必须当不可信输入处理：
    名字要匹配 ready-*，目录必须真实存在且不是链接，否则一个被改过的
    journal 就能让 recover 把文件写到任意位置。

    Args:
        root (str | Path): 应用根目录。
        record (dict): journal 内容。

    Returns:
        Path: 备份目录（previous/）。

    Raises:
        ValueError: 名字不合规或目录缺失/是链接。
    """
    name = record.get('ready', '')
    if not _READY_NAME.fullmatch(name):
        raise ValueError(f'更新恢复记录无效: {name!r}')
    backup = Path(root) / WORK / name / 'previous'
    if backup.resolve() != backup or not backup.is_dir():
        raise ValueError('更新恢复副本缺失')
    return backup


# ── 事务三段：prepare / apply / confirm(recover) ────────────────────────────

def prepare(offer, root=None):
    """下载并验证候选版本，落到 ready-*/，全程不碰现有文件。

    Args:
        offer (dict): `check()` 的返回值，含 revision / repository / branch。
        root (str | Path, optional): 应用根目录。

    Returns:
        Path: ready 目录，含 app/、files.json、version.json、source.json。

    Raises:
        RuntimeError: 源变化、依赖不符、候选版启动失败。
        ValueError: 应用包缺少必要文件。
    """
    root = Path(root or Path.cwd()).resolve()
    source = (offer['repository'], offer['branch'])
    if update_source(root) != source:
        raise RuntimeError('更新源已更改，请重新检查更新')

    receipt_path = root / RECEIPT
    current = read_json(receipt_path) if receipt_path.is_file() else {}
    if receipt_path.is_file():
        if dependency_id(root) != current.get('dependencies'):
            raise RuntimeError('本机依赖清单已经改变，请用完整安装包修复')

    directory = _transaction(root).parent
    directory.mkdir(parents=True, exist_ok=True)
    revision = offer['revision']

    with tempfile.TemporaryDirectory(
        prefix='download-', dir=str(directory), ignore_cleanup_errors=True
    ) as temp_name:
        temp = Path(temp_name)
        cache, _ = _fetch_source(root, directory, offer)
        archive = _archive_source(root, cache, revision, temp / 'source.zip')

        staged = temp / 'app'
        staged.mkdir()
        files = unpack(archive, staged)

        required = ('gui.py', 'uv.lock', 'pyproject.toml')
        if not all(name in files for name in required):
            raise RuntimeError('仓库不兼容此独立版，当前版本可以继续运行')

        # 目标版本的依赖必须与本机一致，否则换应用文件会让 runtime 失配
        if receipt_path.is_file() and dependency_id(staged) != current.get('dependencies'):
            raise RuntimeError('所选版本需要不同的 Python 依赖，请使用对应完整安装包')

        # 下载期间用户可能改了更新源，再确认一次
        if update_source(root) != source:
            raise RuntimeError('更新源已更改，请重新检查更新')

        try:
            files.update(
                _frontend_for_commit(root, offer, revision, archive, temp, staged)
            )
        except zipfile.BadZipFile as error:
            raise RuntimeError('网页包无效，当前版本可以继续运行') from error

        final_required = ('gui.py', 'frontend/dist/index.html', 'uv.lock')
        if not all(name in files for name in final_required):
            raise ValueError('应用包缺少必要文件')

        # 最后一道闸：候选版必须真能对外服务
        #
        # 候选目录是从 git archive 出来的，里面没有 config/（app_path 不放行，
        # 更新本来也不该改用户配置）。但 create_app() 启动时要读模板文件，
        # 缺了它会直接 ModuleNotFound 式的 FileNotFoundError 起不来。
        # 这里把模板补进去——只给探活用，files 里不记，apply 不会碰。
        _seed_probe_config(root, staged)
        probe(root, staged)

        ready = Path(tempfile.mkdtemp(prefix='ready-', dir=str(directory)))
        replace_dir(staged, ready / 'app')
        write_json(ready / 'files.json', files)
        write_json(ready / 'version.json', {**current, 'revision': revision})
        write_json(ready / 'source.json', list(source))
        return ready


def apply(ready, root=None):
    """把 ready 目录里的文件装到应用根，写 journal(installing → trial)。

    执行前的三道检查缺一不可：
    1. journal 不存在（否则上一轮还没恢复完）
    2. 每个待改文件的本机哈希与旧清单一致（用户手改过的文件绝不覆盖）
    3. config 与所有目标路径都不是链接

    Args:
        ready (str | Path): `prepare()` 返回的目录。
        root (str | Path, optional): 应用根目录。

    Raises:
        RuntimeError: 源变化、有待恢复事务、或发现本机修改。
        ValueError: 路径经过链接。
    """
    root = Path(root or Path.cwd()).resolve()
    ready = Path(ready)

    source_file = ready / 'source.json'
    if source_file.is_file():
        if read_json(source_file) != list(update_source(root)):
            raise RuntimeError('更新源已更改，请重新检查更新')

    journal = _transaction(root)
    if journal.exists():
        raise RuntimeError('正在恢复上一轮更新，请先完成恢复')

    files_path = root / FILES
    old = read_json(files_path) if files_path.is_file() else {}
    new = read_json(ready / 'files.json')
    changed = sorted(
        name for name in old.keys() | new.keys() if old.get(name) != new.get(name)
    )

    for name in changed:
        target = check_target(root, name)
        if target.exists():
            # 文件存在但哈希对不上 → 用户改过（或上次安装没写完），保留现状
            if name not in old or not target.is_file() or digest(target) != old[name]:
                raise RuntimeError(f'检测到本机修改，保留现有文件 {name}')

    backup = ready / 'previous'
    config = root / 'config'
    if config.is_dir():
        if config.resolve() != config:
            raise ValueError('配置目录不能是链接')
        for directory, names, filenames in os.walk(config, followlinks=False):
            for entry in (*names, *filenames):
                path = Path(directory) / entry
                if path.is_symlink() or _is_junction(path):
                    raise ValueError('配置包含链接，当前安装保留原状')
        backup_config(config, backup / 'config')

    for name in changed:
        target = root / name
        if target.is_file():
            backup_file(target, backup / 'app' / name)

    receipt_path = root / RECEIPT
    if receipt_path.is_file():
        backup_file(receipt_path, backup / RECEIPT)
    if files_path.is_file():
        backup_file(files_path, backup / FILES)

    journal.parent.mkdir(parents=True, exist_ok=True)
    write_json(journal, {'ready': ready.name, 'changed': changed, 'phase': 'installing'})
    try:
        for name in changed:
            target = root / name
            if name in new:
                restore_file(ready / 'app' / name, target)
            else:
                # 新版本删掉的文件，这里同步删除
                target.unlink(missing_ok=True)
        write_json(files_path, new)
        write_json(receipt_path, read_json(ready / 'version.json'))
        # attempts 记的是"启动新版本试了几次"。首次启动不算失败，
        # 只有第二次还看到这条 trial 记录，才说明上一次启动没能跑到确认点。
        write_json(journal, {'ready': ready.name, 'changed': changed,
                             'phase': 'trial', 'attempts': 0})
    except BaseException:
        # 安装中途失败必须原地回滚，不能让用户停在半新半旧的状态
        recover(root)
        raise


def recover(root=None):
    """按 journal 回滚到更新前的状态。

    试运行阶段的回滚会保留失败后的 config（挪到 config-after-failed-update），
    只把更新前的快照换回去。这样用户既回到可用状态，又没丢掉崩溃时的现场。

    Args:
        root (str | Path, optional): 应用根目录。

    Returns:
        bool: 确实执行了恢复返回 True；没有待恢复事务返回 False。

    Raises:
        ValueError: journal 或备份目录被篡改。
    """
    root = Path(root or Path.cwd()).resolve()
    journal = _transaction(root)
    if not journal.is_file():
        return False

    record = read_json(journal)
    backup = _previous(root, record)

    if record.get('phase') == 'trial' and (backup / 'config').is_dir():
        # 现场放在应用根而不是 ready 目录：ready 最后会被整个清掉，
        # 放里面等于白留。名字直白一点，用户一眼能看出这是更新失败的配置。
        failed = root / 'config-after-failed-update'
        prepared = backup.parent / 'config-to-restore'
        config = root / 'config'
        if not failed.exists():
            # 先把快照复制到中间位置，两次 rename 之间断电还能从记录继续
            shutil.copytree(backup / 'config', prepared, dirs_exist_ok=True)
            if config.exists():
                replace_dir(config, failed)
        if not config.exists():
            replace_dir(prepared, config)

    for name in record.get('changed', []):
        target = check_target(root, name)
        original = backup / 'app' / name
        if original.is_file():
            restore_file(original, target)
        else:
            # 这是本轮新加的文件，回滚就是删掉
            target.unlink(missing_ok=True)

    for name in (RECEIPT, FILES):
        original = backup / name
        if original.is_file():
            restore_file(original, root / name)

    journal.unlink(missing_ok=True)
    # 整个 ready 目录都不再有用了：候选版在 ready/app，备份在 ready/previous。
    # 只删其中一个会留下 1GB 级的孤儿文件，所以这里删 backup.parent（即 ready 本身）。
    shutil.rmtree(backup.parent, ignore_errors=True)
    return True


def confirm(root=None):
    """确认试运行成功，清掉 journal 与备份。

    只在 phase == 'trial' 时生效：installing 阶段说明安装还没写完，
    这时调 confirm 会把回滚材料提前删掉。

    Args:
        root (str | Path, optional): 应用根目录。

    Returns:
        bool: 确认成功返回 True；没有待确认事务返回 False。
    """
    root = Path(root or Path.cwd()).resolve()
    journal = _transaction(root)
    if not journal.is_file():
        return False
    record = read_json(journal)
    if record.get('phase') != 'trial':
        return False
    backup = _previous(root, record)
    journal.unlink(missing_ok=True)
    # 同 recover：候选版与备份都在 ready 目录下，一起清掉才算收尾干净。
    shutil.rmtree(backup.parent, ignore_errors=True)
    return True


def trial_pending(root=None):
    """是否存在未确认的试运行事务。

    启动器在每次拉起 WebUI 前查一次。为真只说明「装完了还没确认」，
    **不等于要回滚**——见 bump_trial_attempt()。

    Args:
        root (str | Path, optional): 应用根目录。

    Returns:
        bool: journal 存在返回 True。
    """
    return _transaction(Path(root or Path.cwd()).resolve()).is_file()


def bump_trial_attempt(root=None):
    """记录一次"启动新版本的尝试"，返回累计尝试次数。

    为什么需要它：
    apply() 之后 journal 停在 trial，紧接着就要重启让新代码生效。
    如果启动时看到 trial 就回滚，等于刚装完立刻自己撤掉，永远更新不了。
    但如果启动崩了又不回滚，用户每次开机都撞在坏版本上。

    用计数区分这两种情况：
        第 1 次启动  → 这是刚装完的首次尝试，放它跑
        第 2 次启动  → 上次没能跑到 confirm()，说明新版本起不来，回滚

    Args:
        root (str | Path, optional): 应用根目录。

    Returns:
        int | None: 累计尝试次数；没有待试运行事务时返回 None。

    Raises:
        ValueError: journal 内容损坏。
    """
    root = Path(root or Path.cwd()).resolve()
    journal = _transaction(root)
    if not journal.is_file():
        return None
    record = read_json(journal)
    if record.get('phase') != 'trial':
        # installing 说明上次装到一半就被打断了，交给 recover 处理，不该走试运行
        return None
    attempts = int(record.get('attempts', 0)) + 1
    record['attempts'] = attempts
    write_json(journal, record)
    return attempts


def install(root=None, *, checkout=True):
    """高层封装：检查 → 准备 → 安装，一步到位。

    Args:
        root (str | Path, optional): 应用根目录。
        checkout (bool): 安装后是否丢弃旧版本备份（False 时等 confirm）。

    Returns:
        dict: {'updated': bool, 'revision': str, 'ready': str | None}。

    Raises:
        RuntimeError: 任一阶段失败（此时当前版本仍完整可用）。
    """
    root = Path(root or Path.cwd()).resolve()
    offer = check(root)
    if offer is None:
        return {'updated': False, 'revision': None, 'ready': None}
    ready = prepare(offer, root)
    apply(ready, root)
    if checkout:
        confirm(root)
    return {'updated': True, 'revision': offer['revision'], 'ready': ready.name}


if __name__ == '__main__':
    # 唯一支持的子命令：从 journal 继续未完成的回滚。
    # 打包脚本 / 启动器在应用启动前调用它，确保不会带着半个安装跑起来。
    if sys.argv[1:] != ['recover']:
        raise SystemExit('用法: python -m deploy.independent recover')
    recovered = recover()
    print('已回滚未完成的事务' if recovered else '没有待恢复的事务')


