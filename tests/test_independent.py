"""便携版事务更新的回归测试。

重点不是「能不能装成功」，而是四条安全边界——每一条失效都会让用户
拿到一个打不开的目录：

1. 路径白名单：`..`、盘符、Windows 保留名、符号链接必须全部拒绝
2. 本机修改保护：用户手改过的文件绝不被静默覆盖
3. journal 状态机：installing 阶段不能 confirm，trial 才能
4. 回滚完整性：recover 之后文件哈希必须与更新前逐一相同

第 4 条是核心。用真实文件哈希比对，而不是只检查「没抛异常」。
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deploy import independent as ind


# ── 路径白名单 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize('name', [
    'module/api/app.py',
    'gui.py',
    'deploy/independent.py',
    'assets/cn/ui/shop.png',
    'config/template.json',
    'config/deploy.template.yaml',
])
def test_app_path_accepts_legitimate(name):
    assert ind.app_path(name) is True


@pytest.mark.parametrize('name', [
    '../outside.py',              # 父目录逃逸
    'module/../../etc/passwd',    # 中段逃逸
    '/abs/path.py',               # 绝对路径
    'C:/Windows/system32/x.py',   # 盘符
    'module\\api\\app.py',        # 反斜杠
    'module/api/app.py.',         # 尾部点
    'module/api/app.py ',         # 尾部空格
    'module/CON.py',              # Windows 保留名
    'module/a<b.py',              # 非法字符
    '',                           # 空
    '.',                          # 单点
])
def test_app_path_rejects_dangerous(name):
    assert ind.app_path(name) is False


def test_app_path_restricts_config_to_templates():
    """config 下只放行模板，用户配置不能被更新覆盖。"""
    assert ind.app_path('config/template.json') is True
    assert ind.app_path('config/ap.json') is False
    assert ind.app_path('config/deploy.yaml') is False


def test_app_path_frontend_scope():
    assert ind.app_path('frontend/dist/index.html', frontend=True) is True
    assert ind.app_path('frontend/dist/assets/a.js', frontend=True) is True
    # 不能借 frontend 标志写进 frontend 根或外层
    assert ind.app_path('frontend/package.json', frontend=True) is False
    assert ind.app_path('module/a.py', frontend=True) is False


# ── 解包安全 ────────────────────────────────────────────────────────────────

def _make_zip(path, entries):
    """造一个 zip。entries 是 {名字: 内容}。"""
    import zipfile
    with zipfile.ZipFile(path, 'w') as bundle:
        for name, content in entries.items():
            bundle.writestr(name, content)
    return path


def test_unpack_skips_paths_outside_whitelist(tmp_path):
    """白名单外的成员被跳过，不进目标目录。"""
    archive = _make_zip(tmp_path / 'a.zip', {
        'gui.py': 'print(1)\n',
        'module/api/app.py': 'X = 1\n',
        'secrets/key.pem': 'private\n',      # 不在白名单
        '../escape.py': 'evil\n',            # 越界
    })
    target = tmp_path / 'out'
    target.mkdir()
    hashes = ind.unpack(archive, target)

    assert 'gui.py' in hashes
    assert 'module/api/app.py' in hashes
    assert not (target / 'secrets').exists()
    assert not (tmp_path / 'escape.py').exists()


def test_unpack_rejects_case_insensitive_duplicates(tmp_path):
    """大小写不同的同名文件在 Windows 上会互相覆盖，必须报错。"""
    archive = _make_zip(tmp_path / 'dup.zip', {
        'module/A.py': 'one\n',
        'module/a.py': 'two\n',
    })
    target = tmp_path / 'out'
    target.mkdir()
    with pytest.raises(ValueError, match='重复路径'):
        ind.unpack(archive, target)


def test_unpack_hashes_every_file(tmp_path):
    archive = _make_zip(tmp_path / 'h.zip', {'gui.py': 'content\n'})
    target = tmp_path / 'out'
    target.mkdir()
    hashes = ind.unpack(archive, target)
    assert hashes['gui.py'] == ind.digest(target / 'gui.py')


# ── 依赖指纹 ────────────────────────────────────────────────────────────────

def test_dependency_id_changes_with_lockfile(tmp_path):
    (tmp_path / 'uv.lock').write_text('v1\n', encoding='utf-8')
    (tmp_path / 'pyproject.toml').write_text('[project]\n', encoding='utf-8')
    before = ind.dependency_id(tmp_path)
    (tmp_path / 'uv.lock').write_text('v2\n', encoding='utf-8')
    assert ind.dependency_id(tmp_path) != before


def test_dependency_id_requires_both(tmp_path):
    with pytest.raises(FileNotFoundError):
        ind.dependency_id(tmp_path)


def test_dependency_id_separator_prevents_collision(tmp_path):
    """'ab'+'c' 和 'a'+'bc' 不能算出同一个摘要。"""
    left = tmp_path / 'left'
    right = tmp_path / 'right'
    left.mkdir()
    right.mkdir()
    (left / 'uv.lock').write_text('ab', encoding='utf-8')
    (left / 'pyproject.toml').write_text('c', encoding='utf-8')
    (right / 'uv.lock').write_text('a', encoding='utf-8')
    (right / 'pyproject.toml').write_text('bc', encoding='utf-8')
    assert ind.dependency_id(left) != ind.dependency_id(right)


# ── 原子写 ──────────────────────────────────────────────────────────────────

def test_write_json_is_atomic_and_readable(tmp_path):
    path = tmp_path / 'nested' / 'state.json'
    ind.write_json(path, {'sequence': 1, 'revision': 'a' * 40})
    assert json.loads(path.read_text(encoding='utf-8'))['sequence'] == 1
    # 不应留下临时文件
    assert not list(path.parent.glob('*.tmp'))


def test_write_json_overwrites_existing(tmp_path):
    path = tmp_path / 'state.json'
    ind.write_json(path, {'v': 1})
    ind.write_json(path, {'v': 2})
    assert json.loads(path.read_text(encoding='utf-8'))['v'] == 2


# ── 事务状态机与回滚 ────────────────────────────────────────────────────────

def _make_app(tmp_path):
    """造一个最小的应用根：静态文件 + config + 空数据库。"""
    import sqlite3
    from contextlib import closing

    root = tmp_path / 'app'
    root.mkdir()
    (root / 'gui.py').write_text('VERSION = 1\n', encoding='utf-8')
    (root / 'uv.lock').write_text('lock-v1\n', encoding='utf-8')
    (root / 'pyproject.toml').write_text('[project]\nname="x"\n', encoding='utf-8')
    (root / 'module').mkdir()
    (root / 'module' / 'core.py').write_text('CORE = 1\n', encoding='utf-8')
    (root / 'frontend' / 'dist').mkdir(parents=True)
    (root / 'frontend' / 'dist' / 'index.html').write_text('<div id="root"></div>\n', encoding='utf-8')
    config = root / 'config'
    config.mkdir()
    (config / 'ap.json').write_text('{"user": "data"}\n', encoding='utf-8')
    # 用 closing 而不是 with：`with sqlite3.connect()` 只管理事务，不关闭连接。
    # 连接不关会让 azurpilot.db 一直被占用，Windows 上整个 config 目录都没法 rename。
    with closing(sqlite3.connect(str(config / 'azurpilot.db'))) as connection:
        connection.execute('CREATE TABLE t (v INTEGER)')
        connection.execute('INSERT INTO t VALUES (42)')
        connection.commit()
    return root


def _make_ready(root, changes, *, phase='trial'):
    """造一个 ready 目录，模拟 prepare() 的产物。"""
    work = root / ind.WORK
    work.mkdir(parents=True, exist_ok=True)
    ready = work / 'ready-test1'
    (ready / 'app' / 'module').mkdir(parents=True)
    (ready / 'app' / 'frontend' / 'dist').mkdir(parents=True)

    for name, content in changes.items():
        path = ready / 'app' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')

    # files.json 描述安装后的完整状态
    old = json.loads((root / ind.FILES).read_text(encoding='utf-8')) \
        if (root / ind.FILES).is_file() else {}
    new = dict(old)
    for name in changes:
        if changes[name] is None:
            new.pop(name, None)
        else:
            new[name] = ind.digest(ready / 'app' / name)
    ind.write_json(ready / 'files.json', new)
    ind.write_json(ready / 'version.json', {'revision': 'b' * 40, 'dependencies': 'c' * 64})
    ind.write_json(ready / 'source.json', ['https://example.com/repo', 'master'])
    return ready, new


def _install_baseline(root):
    """给应用根建立初始 FILES/RECEIPT，模拟已安装状态。"""
    files = {
        name: ind.digest(root / name)
        for name in ('gui.py', 'uv.lock', 'pyproject.toml', 'module/core.py',
                     'frontend/dist/index.html')
    }
    ind.write_json(root / ind.FILES, files)
    ind.write_json(root / ind.RECEIPT, {'revision': 'a' * 40, 'dependencies': 'c' * 64})
    return files


def test_apply_backs_up_and_journal_reaches_trial(tmp_path, monkeypatch):
    """正常安装：文件被替换，journal 停在 trial，备份齐全。"""
    root = _make_app(tmp_path)
    _install_baseline(root)
    ready, new = _make_ready(root, {'module/core.py': 'CORE = 2\n'})

    # 跳过源校验（测试没有真实仓库）
    monkeypatch.setattr(ind, 'update_source', lambda r: ('https://example.com/repo', 'master'))
    ind.apply(ready, root)

    assert (root / 'module' / 'core.py').read_text(encoding='utf-8') == 'CORE = 2\n'
    record = json.loads((root / ind.WORK / 'transaction.json').read_text(encoding='utf-8'))
    assert record['phase'] == 'trial'
    assert record['changed'] == ['module/core.py']
    # 旧文件被备份
    backup = ready / 'previous' / 'app' / 'module' / 'core.py'
    assert backup.is_file() and backup.read_text(encoding='utf-8') == 'CORE = 1\n'


def test_apply_refuses_to_overwrite_local_modification(tmp_path, monkeypatch):
    """用户手改过的文件不能被静默覆盖。"""
    root = _make_app(tmp_path)
    _install_baseline(root)
    # 用户在更新前改了文件
    (root / 'module' / 'core.py').write_text('USER EDIT\n', encoding='utf-8')

    ready, _ = _make_ready(root, {'module/core.py': 'CORE = 2\n'})
    monkeypatch.setattr(ind, 'update_source', lambda r: ('https://example.com/repo', 'master'))

    with pytest.raises(RuntimeError, match='检测到本机修改'):
        ind.apply(ready, root)

    # 用户改动仍在，且没有留下 journal
    assert (root / 'module' / 'core.py').read_text(encoding='utf-8') == 'USER EDIT\n'
    assert not (root / ind.WORK / 'transaction.json').exists()


def test_apply_refuses_when_journal_exists(tmp_path, monkeypatch):
    """上一轮没恢复完就再装，会破坏回滚材料。"""
    root = _make_app(tmp_path)
    _install_baseline(root)
    ready, _ = _make_ready(root, {'module/core.py': 'CORE = 2\n'})
    monkeypatch.setattr(ind, 'update_source', lambda r: ('https://example.com/repo', 'master'))

    journal = root / ind.WORK / 'transaction.json'
    journal.parent.mkdir(parents=True, exist_ok=True)
    ind.write_json(journal, {'ready': 'ready-test1', 'changed': [], 'phase': 'trial'})

    with pytest.raises(RuntimeError, match='正在恢复'):
        ind.apply(ready, root)


def test_recover_restores_every_changed_file(tmp_path, monkeypatch):
    """回滚后所有文件必须与更新前逐字节一致。"""
    root = _make_app(tmp_path)
    before = _install_baseline(root)
    before_hashes = {name: ind.digest(root / name) for name in before}

    ready, _ = _make_ready(root, {
        'module/core.py': 'CORE = 2\n',
        'gui.py': 'VERSION = 2\n',
    })
    monkeypatch.setattr(ind, 'update_source', lambda r: ('https://example.com/repo', 'master'))
    ind.apply(ready, root)

    assert (root / 'module' / 'core.py').read_text(encoding='utf-8') == 'CORE = 2\n'
    assert ind.recover(root) is True

    for name, expected in before_hashes.items():
        assert ind.digest(root / name) == expected, f'{name} 未正确回滚'
    assert not (root / ind.WORK / 'transaction.json').exists()


def test_recover_preserves_failed_config(tmp_path, monkeypatch):
    """回滚要保留崩溃后的 config 现场，而不是直接丢掉。"""
    root = _make_app(tmp_path)
    _install_baseline(root)
    ready, _ = _make_ready(root, {'module/core.py': 'CORE = 2\n'})
    monkeypatch.setattr(ind, 'update_source', lambda r: ('https://example.com/repo', 'master'))
    ind.apply(ready, root)

    # 模拟新版本运行失败后又写了配置
    (root / 'config' / 'ap.json').write_text('{"user": "broken"}\n', encoding='utf-8')
    ind.recover(root)

    # 配置还原成更新前的快照
    assert (root / 'config' / 'ap.json').read_text(encoding='utf-8') == '{"user": "data"}\n'
    # 崩溃现场留在应用根，不在 ready 里（ready 会被清掉）
    failed = root / 'config-after-failed-update'
    assert failed.is_dir(), '失败的 config 必须留档'
    assert (failed / 'ap.json').read_text(encoding='utf-8') == '{"user": "broken"}\n'


def test_recover_removes_files_added_by_update(tmp_path, monkeypatch):
    """更新新增的文件，回滚时要删掉。"""
    root = _make_app(tmp_path)
    _install_baseline(root)
    ready, _ = _make_ready(root, {'module/extra.py': 'NEW = 1\n'})
    monkeypatch.setattr(ind, 'update_source', lambda r: ('https://example.com/repo', 'master'))
    ind.apply(ready, root)
    assert (root / 'module' / 'extra.py').is_file()

    ind.recover(root)
    assert not (root / 'module' / 'extra.py').exists()


def test_recover_without_journal_returns_false(tmp_path):
    root = _make_app(tmp_path)
    assert ind.recover(root) is False


def test_confirm_only_in_trial_phase(tmp_path, monkeypatch):
    """installing 阶段不能 confirm，否则回滚材料被提前删掉。"""
    root = _make_app(tmp_path)
    _install_baseline(root)
    ready, _ = _make_ready(root, {'module/core.py': 'CORE = 2\n'})
    monkeypatch.setattr(ind, 'update_source', lambda r: ('https://example.com/repo', 'master'))

    journal = root / ind.WORK / 'transaction.json'
    journal.parent.mkdir(parents=True, exist_ok=True)
    (ready / 'previous' / 'app').mkdir(parents=True, exist_ok=True)
    ind.write_json(journal, {'ready': ready.name, 'changed': [], 'phase': 'installing'})

    assert ind.confirm(root) is False
    assert journal.exists()


def test_confirm_clears_journal_in_trial(tmp_path, monkeypatch):
    root = _make_app(tmp_path)
    _install_baseline(root)
    ready, _ = _make_ready(root, {'module/core.py': 'CORE = 2\n'})
    monkeypatch.setattr(ind, 'update_source', lambda r: ('https://example.com/repo', 'master'))
    ind.apply(ready, root)

    assert ind.confirm(root) is True
    assert not (root / ind.WORK / 'transaction.json').exists()
    assert not (ready / 'previous' / 'app').exists()


def test_trial_pending_reflects_journal(tmp_path, monkeypatch):
    root = _make_app(tmp_path)
    _install_baseline(root)
    assert ind.trial_pending(root) is False

    ready, _ = _make_ready(root, {'module/core.py': 'CORE = 2\n'})
    monkeypatch.setattr(ind, 'update_source', lambda r: ('https://example.com/repo', 'master'))
    ind.apply(ready, root)
    assert ind.trial_pending(root) is True

    ind.confirm(root)
    assert ind.trial_pending(root) is False


def test_recover_rejects_tampered_journal(tmp_path):
    """journal 里的 ready 名字不可信，必须校验。"""
    root = _make_app(tmp_path)
    journal = root / ind.WORK / 'transaction.json'
    journal.parent.mkdir(parents=True, exist_ok=True)
    ind.write_json(journal, {'ready': '../../../etc', 'changed': [], 'phase': 'trial'})

    with pytest.raises(ValueError, match='恢复记录无效'):
        ind.recover(root)


def test_backup_config_uses_consistent_snapshot(tmp_path):
    """config 备份要能读回完整数据库，而不是撕裂的 WAL 状态。"""
    import sqlite3
    from contextlib import closing

    root = _make_app(tmp_path)
    destination = tmp_path / 'backup-config'
    ind.backup_config(root / 'config', destination)

    assert (destination / 'ap.json').is_file()
    with closing(sqlite3.connect(str(destination / 'azurpilot.db'))) as connection:
        assert connection.execute('SELECT v FROM t').fetchone()[0] == 42
    # 不应复制 WAL 边车
    assert not (destination / 'azurpilot.db-wal').exists()


def test_check_target_rejects_symlink(tmp_path):
    """符号链接会让写入落到目录外。"""
    root = _make_app(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = root / 'module' / 'linked.py'
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip('当前环境不支持创建符号链接')

    with pytest.raises(ValueError, match='链接'):
        ind.check_target(root, 'module/linked.py')
