"""事务更新的端到端测试：真实 git 仓库，走完 check → prepare → apply → confirm / recover。

与 test_independent.py 的分工：
- 那边是单元测试，路径白名单、哈希比对、状态机逐条钉死
- 这边只验"整条链路能不能真的跑通"：真建裸仓库、真 fetch、真 git archive、
  真解包、真替换文件、真回滚

两处刻意打桩，都写明理由：
- update_source：生产要求 HTTPS 地址，测试用本地路径
- probe：真起 ASGI 应用要完整 runtime，打包流程里已经验过；这里只关心事务
  （打包实测见 logs/p1-git.txt：/healthz 200 且首页含 <div id="root">）
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deploy import independent as ind


def _run(*args, cwd=None):
    """跑一条 git 命令，失败即抛。"""
    result = subprocess.run(
        ['git', *args], cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, encoding='utf-8', errors='replace',
    )
    if result.returncode != 0:
        raise RuntimeError(f'git {" ".join(args)} 失败: {result.stderr[:400]}')
    return result.stdout


def _rev(cwd, ref='HEAD'):
    return _run('rev-parse', ref, cwd=cwd).strip()


@pytest.fixture()
def allow_file_protocol(monkeypatch):
    """放行 file:// 协议，其余与生产一致。

    生产版 git() 是 protocol.allow=never + 只放行 https，用来挡 ext:: 之类
    的传输助手注入。测试要用本地裸仓库当"远端"，只能额外放行 file。
    """
    def patched(root, cwd, *args, stdout=subprocess.DEVNULL, timeout=210):
        command = [
            ind._git_executable(root),
            '-c', 'http.sslVerify=true',
            '-c', 'credential.helper=',
            '-c', 'protocol.allow=never',
            '-c', 'protocol.https.allow=always',
            '-c', 'protocol.file.allow=always',   # 仅测试需要
            *args,
        ]
        result = subprocess.run(
            command, cwd=str(cwd), capture_output=True, timeout=timeout,
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, command, None, result.stderr)
        return subprocess.CompletedProcess(
            command, result.returncode,
            result.stdout if stdout == subprocess.PIPE else None,
            result.stderr,
        )

    monkeypatch.setattr(ind, 'git', patched)


@pytest.fixture()
def no_probe(monkeypatch):
    """跳过候选版启动检查，理由见模块 docstring。"""
    monkeypatch.setattr(ind, 'probe', lambda root, staged, timeout=40.0: None)


APP_FILES_V1 = {
    'gui.py': 'VERSION = 1\n',
    'uv.lock': 'lock-v1\n',
    'pyproject.toml': '[project]\nname = "zurpilot"\n',
    'module/__init__.py': '',
    'module/core.py': 'CORE = 1\n',
    'frontend/dist/index.html': '<div id="root"></div>\n',
    'frontend/dist/assets/app.js': 'console.log(1)\n',
}

APP_FILES_V2 = dict(APP_FILES_V1)
APP_FILES_V2['module/core.py'] = 'CORE = 2\n'
APP_FILES_V2['module/added.py'] = 'NEW = True\n'


def _commit(work, files, message):
    """把 files 写进工作树并提交、推送。"""
    for name, content in files.items():
        path = work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    _run('add', '-A', cwd=work)
    _run('-c', 'user.name=t', '-c', 'user.email=t@example.com',
         'commit', '-m', message, cwd=work)
    _run('push', 'origin', 'HEAD:master', cwd=work)
    return _rev(work)


@pytest.fixture()
def repo(tmp_path):
    """建一个本地裸仓库 + 开发副本，首版已推送。

    Returns:
        tuple[Path, Path, str]: (裸仓库, 开发副本, 首版 revision)
    """
    remote = tmp_path / 'remote.git'
    remote.mkdir()
    _run('init', '--bare', '-q', str(remote))

    dev = tmp_path / 'dev'
    _run('clone', '-q', str(remote), str(dev))
    _run('checkout', '-q', '-b', 'master', cwd=dev)
    revision = _commit(dev, APP_FILES_V1, 'v1')
    return remote, dev, revision


def _make_app(tmp_path, revision, files=APP_FILES_V1):
    """按首版内容建一个"已安装"的应用根。"""
    root = tmp_path / 'app'
    root.mkdir()
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    # config 里放一个用户配置，验证回滚会把它一起还原
    (root / 'config').mkdir()
    (root / 'config' / 'ap.json').write_text('{"user": "v1"}\n', encoding='utf-8')

    hashes = {name: ind.digest(root / name) for name in files}
    ind.write_json(root / ind.FILES, hashes)
    ind.write_json(root / ind.RECEIPT, {
        'schema': 1,
        'revision': revision,
        'dependencies': ind.dependency_id(root),
    })
    return root, hashes


def test_no_update_when_revision_matches(tmp_path, repo, allow_file_protocol, monkeypatch):
    """本地已是远端最新时不返回 offer。"""
    remote, _dev, revision = repo
    root, _ = _make_app(tmp_path, revision)
    monkeypatch.setattr(ind, 'update_source', lambda r: (str(remote), 'master'))

    assert ind.check(root) is None


def test_check_detects_new_revision(tmp_path, repo, allow_file_protocol, monkeypatch):
    """远端有新提交时返回带 revision 的 offer。"""
    remote, dev, revision = repo
    root, _ = _make_app(tmp_path, revision)
    monkeypatch.setattr(ind, 'update_source', lambda r: (str(remote), 'master'))

    second = _commit(dev, APP_FILES_V2, 'v2')

    offer = ind.check(root)
    assert offer is not None
    assert offer['revision'] == second
    assert offer['repository'] == str(remote)
    assert offer['branch'] == 'master'


def test_full_update_cycle(tmp_path, repo, allow_file_protocol, no_probe, monkeypatch):
    """check → prepare → apply → confirm 全链路。"""
    remote, dev, revision = repo
    root, _ = _make_app(tmp_path, revision)
    monkeypatch.setattr(ind, 'update_source', lambda r: (str(remote), 'master'))

    second = _commit(dev, APP_FILES_V2, 'v2')
    offer = ind.check(root)
    assert offer['revision'] == second

    ready = ind.prepare(offer, root)
    assert (ready / 'app' / 'module' / 'core.py').read_text(encoding='utf-8') == 'CORE = 2\n'
    assert (ready / 'files.json').is_file()
    assert json.loads((ready / 'version.json').read_text(encoding='utf-8'))['revision'] == second

    ind.apply(ready, root)

    # 改动生效
    assert (root / 'module' / 'core.py').read_text(encoding='utf-8') == 'CORE = 2\n'
    # 新增文件落地
    assert (root / 'module' / 'added.py').read_text(encoding='utf-8') == 'NEW = True\n'
    # 状态进入 trial
    journal = json.loads((root / ind.WORK / 'transaction.json').read_text(encoding='utf-8'))
    assert journal['phase'] == 'trial'
    assert sorted(journal['changed']) == ['module/added.py', 'module/core.py']
    # 用户配置没被更新碰到
    assert (root / 'config' / 'ap.json').read_text(encoding='utf-8') == '{"user": "v1"}\n'
    # RECEIPT 已指向新版本
    assert json.loads((root / ind.RECEIPT).read_text(encoding='utf-8'))['revision'] == second

    assert ind.confirm(root) is True
    assert not (root / ind.WORK / 'transaction.json').exists()
    # ready 目录整个清掉，不留孤儿
    assert not ready.exists()


def test_rollback_after_failed_trial(tmp_path, repo, allow_file_protocol, no_probe, monkeypatch):
    """apply 之后不确认、直接 recover：文件与配置都要回到更新前。

    模拟的正是"新版本装上了但起不来"——journal 停在 trial，
    下次启动由 gui._recover_pending_update() 接手。
    """
    remote, dev, revision = repo
    root, before_hashes = _make_app(tmp_path, revision)
    monkeypatch.setattr(ind, 'update_source', lambda r: (str(remote), 'master'))

    _commit(dev, APP_FILES_V2, 'v2')
    ready = ind.prepare(ind.check(root), root)
    ind.apply(ready, root)

    # 新版本"跑了一下"，写了配置
    (root / 'config' / 'ap.json').write_text('{"user": "v2-broken"}\n', encoding='utf-8')

    assert ind.trial_pending(root) is True
    assert ind.recover(root) is True

    # 每个文件都回到更新前的字节
    for name, expected in before_hashes.items():
        assert ind.digest(root / name) == expected, f'{name} 未回滚'
    # 更新时新增的文件被删掉
    assert not (root / 'module' / 'added.py').exists()
    # 配置还原成快照
    assert (root / 'config' / 'ap.json').read_text(encoding='utf-8') == '{"user": "v1"}\n'
    # 崩溃现场留档
    failed = root / 'config-after-failed-update'
    assert (failed / 'ap.json').read_text(encoding='utf-8') == '{"user": "v2-broken"}\n'
    # journal 与工作目录清干净
    assert not (root / ind.WORK / 'transaction.json').exists()
    assert not ready.exists()


def test_apply_refuses_local_modification(tmp_path, repo, allow_file_protocol, no_probe, monkeypatch):
    """端到端也要守住"不覆盖用户改动"这条底线。"""
    remote, dev, revision = repo
    root, _ = _make_app(tmp_path, revision)
    monkeypatch.setattr(ind, 'update_source', lambda r: (str(remote), 'master'))

    _commit(dev, APP_FILES_V2, 'v2')
    # 用户自己改了一个待更新的文件
    (root / 'module' / 'core.py').write_text('CORE = "user patch"\n', encoding='utf-8')

    ready = ind.prepare(ind.check(root), root)
    with pytest.raises(RuntimeError, match='检测到本机修改'):
        ind.apply(ready, root)

    assert (root / 'module' / 'core.py').read_text(encoding='utf-8') == 'CORE = "user patch"\n'


def test_prepare_rejects_changed_source(tmp_path, repo, allow_file_protocol, no_probe, monkeypatch):
    """prepare 期间更新源被改，必须重新检查而不是照着旧 offer 装。"""
    remote, dev, revision = repo
    root, _ = _make_app(tmp_path, revision)

    state = {'calls': 0}

    def flaky_source(r):
        state['calls'] += 1
        # 第一次返回真地址，之后返回别的地址，模拟用户中途改配置
        return (str(remote) if state['calls'] == 1 else 'https://github.com/other/repo', 'master')

    _commit(dev, APP_FILES_V2, 'v2')
    monkeypatch.setattr(ind, 'update_source', lambda r: (str(remote), 'master'))
    offer = ind.check(root)

    monkeypatch.setattr(ind, 'update_source', flaky_source)
    with pytest.raises(RuntimeError, match='更新源已更改'):
        ind.prepare(offer, root)


def test_second_update_after_confirm(tmp_path, repo, allow_file_protocol, no_probe, monkeypatch):
    """确认后再来一轮，验证 FILES 基线被正确更新（不是只认第一次）。"""
    remote, dev, revision = repo
    root, _ = _make_app(tmp_path, revision)
    monkeypatch.setattr(ind, 'update_source', lambda r: (str(remote), 'master'))

    _commit(dev, APP_FILES_V2, 'v2')
    ind.apply(ind.prepare(ind.check(root), root), root)
    ind.confirm(root)

    # v3 只改 gui.py
    v3 = dict(APP_FILES_V2)
    v3['gui.py'] = 'VERSION = 3\n'
    third = _commit(dev, v3, 'v3')

    offer = ind.check(root)
    assert offer['revision'] == third
    ind.apply(ind.prepare(offer, root), root)
    ind.confirm(root)

    assert (root / 'gui.py').read_text(encoding='utf-8') == 'VERSION = 3\n'
    assert (root / 'module' / 'core.py').read_text(encoding='utf-8') == 'CORE = 2\n'
