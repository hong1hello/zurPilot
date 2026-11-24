"""便携包更新接入（UpdateService 的 portable 分支）的回归测试。

这条路径决定"用户点检查更新 / 点应用更新"在便携包里做什么。
开发环境走的是 Git 分支，只有便携包（带 runtime/）才会进这里，
所以必须有独立的测试，不能靠 Git 那套的用例覆盖。

覆盖：
- portable 判定
- status 字段（本地版本来自 RECEIPT、上游来自 check 缓存、journal 在时算忙）
- fetch 结果缓存
- apply 走 prepare → apply → 请求重启
- 不允许取消（事务性安装自带回滚）
- commits 从缓存裸仓库读
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deploy.independent as ind
from module.api.protocol import ApiError
from module.api.update_service import UpdateService


def _fake_portable(tmp_path, revision='a' * 40):
    """造一个最小的便携包目录：runtime/python.exe 是空的占位文件。

    portable 判定只看这个文件存不存在，不需要真解释器。
    """
    root = tmp_path / 'pack'
    (root / 'runtime').mkdir(parents=True)
    (root / 'runtime' / 'python.exe').write_bytes(b'')
    (root / 'config').mkdir()
    if revision:
        ind.write_json(root / ind.RECEIPT, {
            'schema': 1, 'revision': revision, 'dependencies': 'b' * 64,
        })
    return root


@pytest.fixture()
def restart_event(monkeypatch):
    """替换 State.restart_event，记录是否被 set。"""
    from module.runtime import setting

    class FakeEvent:
        def __init__(self):
            self.calls = 0

        def set(self):
            self.calls += 1

    event = FakeEvent()
    monkeypatch.setattr(setting.State, 'restart_event', event, raising=False)
    return event


def test_portable_detection(tmp_path):
    """有 runtime/python.exe 就是便携包；没有就是开发环境。"""
    dev = tmp_path / 'dev'
    dev.mkdir()
    assert UpdateService(root=dev).portable is False

    pack = _fake_portable(tmp_path)
    assert UpdateService(root=pack).portable is True


def test_status_reports_local_revision_from_receipt(tmp_path, restart_event):
    """本地版本取 RECEIPT，而不是 rev-parse（便携包没有 .git）。"""
    root = _fake_portable(tmp_path, revision='c' * 40)
    status = UpdateService(root=root).status()

    assert status['portable'] is True
    assert status['localHead'] == 'c' * 40
    assert status['upstreamHead'] is None
    assert status['available'] is False
    assert status['canApply'] is False, '没检查过更新就不能装'
    assert status['busy'] is False


def test_status_marks_busy_when_journal_exists(tmp_path, restart_event):
    """journal 还在说明上一轮装完没确认，不能再装第二次。"""
    root = _fake_portable(tmp_path)
    journal = root / ind.WORK / 'transaction.json'
    journal.parent.mkdir(parents=True)
    ind.write_json(journal, {'ready': 'ready-x', 'changed': [], 'phase': 'trial', 'attempts': 1})

    status = UpdateService(root=root).status()
    assert status['busy'] is True
    assert status['state'] == 'trial'
    assert status['canApply'] is False


def test_status_with_cached_offer(tmp_path, restart_event):
    """有缓存 offer 且 revision 不同时，available 为真且 canApply 跟随重启通道。"""
    root = _fake_portable(tmp_path, revision='a' * 40)
    service = UpdateService(root=root)
    service._offer = {'revision': 'd' * 40, 'repository': 'https://example.com/r', 'branch': 'master'}

    status = service.status()
    assert status['available'] is True
    assert status['upstreamHead'] == 'd' * 40
    assert status['behind'] == 1
    assert status['canApply'] is True


def test_status_cannot_apply_without_restart_channel(tmp_path, monkeypatch):
    """没有 supervisor 时拿不到重启通道，不能装作能装。"""
    from module.runtime import setting
    monkeypatch.setattr(setting.State, 'restart_event', None, raising=False)

    root = _fake_portable(tmp_path, revision='a' * 40)
    service = UpdateService(root=root)
    service._offer = {'revision': 'd' * 40, 'repository': 'x', 'branch': 'master'}

    status = service.status()
    assert status['available'] is True
    assert status['canApply'] is False, '装完没法重启就别让装'


def test_fetch_caches_offer(tmp_path, restart_event, monkeypatch):
    """fetch 走 check()，结果留在 _offer 里供 status 用。"""
    root = _fake_portable(tmp_path)
    service = UpdateService(root=root)
    offer = {'revision': 'e' * 40, 'repository': 'https://example.com/r', 'branch': 'master'}
    monkeypatch.setattr(ind, 'check', lambda r: offer)

    service._run_portable('fetch')
    assert service._offer == offer
    assert service.error == ''


def test_fetch_clears_offer_when_up_to_date(tmp_path, restart_event, monkeypatch):
    """已是最新时 check() 返回 None，缓存要跟着清空。"""
    root = _fake_portable(tmp_path)
    service = UpdateService(root=root)
    service._offer = {'revision': 'old', 'repository': 'x', 'branch': 'master'}
    monkeypatch.setattr(ind, 'check', lambda r: None)

    service._run_portable('fetch')
    assert service._offer is None


def test_apply_runs_transaction_and_requests_restart(tmp_path, restart_event, monkeypatch):
    """apply 依次调 prepare / apply，然后 set 重启事件。"""
    root = _fake_portable(tmp_path)
    service = UpdateService(root=root)
    service._offer = {'revision': 'f' * 40, 'repository': 'https://example.com/r', 'branch': 'master'}

    calls = []
    ready = tmp_path / 'ready-stub'
    monkeypatch.setattr(ind, 'prepare', lambda offer, r: calls.append(('prepare', offer)) or ready)
    monkeypatch.setattr(ind, 'apply', lambda rd, r: calls.append(('apply', rd)))

    service._run_portable('apply')

    assert calls[0][0] == 'prepare'
    assert calls[1] == ('apply', ready)
    assert restart_event.calls == 1, '装完必须请求重启，否则跑的还是旧代码'
    assert service._offer is None, '装完要清掉 offer，避免重复安装'


def test_apply_requires_fetch_first(tmp_path, restart_event):
    """没检查过更新就点应用，应当被拒绝而不是盲装。"""
    root = _fake_portable(tmp_path)
    service = UpdateService(root=root)
    with pytest.raises(ApiError) as excinfo:
        service._run_portable('apply')
    assert '请先检查更新' in str(excinfo.value)


def test_apply_without_supervisor_tells_user_to_restart(tmp_path, monkeypatch):
    """直接运行模式没有重启通道：文件已落地，提示手动重启，不算失败。"""
    from module.runtime import setting
    monkeypatch.setattr(setting.State, 'restart_event', None, raising=False)

    root = _fake_portable(tmp_path)
    service = UpdateService(root=root)
    service._offer = {'revision': 'g' * 40, 'repository': 'x', 'branch': 'master'}
    monkeypatch.setattr(ind, 'prepare', lambda offer, r: tmp_path / 'ready')
    monkeypatch.setattr(ind, 'apply', lambda rd, r: None)

    service._run_portable('apply')
    assert '请重启程序' in service.error


def test_prepare_failure_propagates_readable_message(tmp_path, restart_event, monkeypatch):
    """事务层给的错误文本要能透到 status.error，用户看得懂。"""
    root = _fake_portable(tmp_path)
    service = UpdateService(root=root)
    service._offer = {'revision': 'h' * 40, 'repository': 'x', 'branch': 'master'}

    def boom(offer, r):
        raise RuntimeError('检测到本机修改，保留现有文件 module/core.py')

    monkeypatch.setattr(ind, 'prepare', boom)
    service._run('apply')

    assert '检测到本机修改' in service.error
    assert service.operation is None, '失败也要释放操作槽位'


def test_cancel_rejected_in_portable(tmp_path, restart_event):
    """事务性安装不允许取消，避免留下半装目录。"""
    root = _fake_portable(tmp_path)
    with pytest.raises(ApiError) as excinfo:
        UpdateService(root=root).cancel()
    assert '不可中途取消' in str(excinfo.value)


def test_commits_without_cache_returns_empty(tmp_path, restart_event):
    """没有缓存裸仓库时返回空历史，不应该抛异常。"""
    root = _fake_portable(tmp_path, revision='i' * 40)
    service = UpdateService(root=root)
    service._offer = {'revision': 'j' * 40, 'repository': 'x', 'branch': 'master'}

    result = service.commits()
    assert result['entries'] == []
    assert result['total'] == 0
    assert result['localHead'] == 'i' * 40
    assert result['upstreamHead'] == 'j' * 40


def test_commits_reads_cached_bare_repo(tmp_path, restart_event):
    """有缓存裸仓库时能读出真实提交历史。"""
    root = _fake_portable(tmp_path, revision=None)
    work = root / ind.WORK
    work.mkdir(parents=True)

    # 造一个真仓库当缓存
    dev = tmp_path / 'dev'
    dev.mkdir()
    def git(*args):
        return subprocess.run(['git', *args], cwd=str(dev), capture_output=True,
                              text=True, encoding='utf-8', errors='replace')
    git('init', '-q', '-b', 'master')
    (dev / 'a.txt').write_text('x\n', encoding='utf-8')
    git('add', '-A')
    git('-c', 'user.name=t', '-c', 'user.email=t@e.com', 'commit', '-m', 'first commit')
    head = git('rev-parse', 'HEAD').stdout.strip()
    git('clone', '-q', '--bare', str(dev), str(work / 'source-test.git'))

    service = UpdateService(root=root)
    service._offer = {'revision': head, 'repository': 'x', 'branch': 'master'}

    result = service.commits()
    assert result['total'] >= 1
    assert result['entries'], '应当能读到提交条目'
    assert 'first commit' in result['entries'][0]['message']
    assert result['entries'][0]['sha'] == head
