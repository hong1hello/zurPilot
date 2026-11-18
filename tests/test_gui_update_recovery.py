"""启动前回滚（gui._recover_pending_update）的回归测试。

这条路径平时不触发，一旦触发就是用户"每次启动都跑在坏版本上"的救命逻辑，
所以每个分支都要钉住，包括「模块不存在」「竞态消失」「重启失败」。

用 monkeypatch 替换 deploy.independent 的成员，不真的动文件系统，
也不真的启动 WebUI。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deploy.independent as independent
import gui


@pytest.fixture()
def no_journal(monkeypatch):
    """默认场景：没有待恢复事务。"""
    monkeypatch.setattr(independent, 'trial_pending', lambda: False)
    monkeypatch.setattr(independent, 'recover', lambda: pytest.fail('不该调用 recover'))


@pytest.fixture()
def restart_spy(monkeypatch):
    """记录是否触发了重启。"""
    calls = []
    monkeypatch.setattr(gui, '_restart_self', lambda: calls.append(1))
    return calls


def test_no_journal_starts_normally(no_journal, restart_spy):
    """没有待恢复事务时返回 None，且不重启。"""
    assert gui._recover_pending_update() is None
    assert restart_spy == []


def test_recovers_then_restarts(monkeypatch, restart_spy):
    """有事务则回滚，回滚成功后重启以加载旧版本。"""
    monkeypatch.setattr(independent, 'trial_pending', lambda: True)
    monkeypatch.setattr(independent, 'recover', lambda: True)

    result = gui._recover_pending_update()

    assert result == gui.EXIT_SUCCESS
    assert restart_spy == [1], '回滚成功后必须重启'


def test_recover_exception_aborts_startup(monkeypatch, restart_spy):
    """回滚本身失败时不能继续启动，否则跑在半更新版本上。"""
    def boom():
        raise OSError('备份副本缺失')

    monkeypatch.setattr(independent, 'trial_pending', lambda: True)
    monkeypatch.setattr(independent, 'recover', boom)

    assert gui._recover_pending_update() == gui.EXIT_UPDATE_RECOVERY_FAILURE
    assert restart_spy == []


def test_journal_unreadable_aborts_startup(monkeypatch, restart_spy):
    """连 journal 都读不出来时，无法判断是否需要回滚，只能中止。"""
    def boom():
        raise ValueError('更新恢复记录无效')

    monkeypatch.setattr(independent, 'trial_pending', boom)
    monkeypatch.setattr(independent, 'recover', lambda: pytest.fail('不该调用 recover'))

    assert gui._recover_pending_update() == gui.EXIT_UPDATE_RECOVERY_FAILURE
    assert restart_spy == []


def test_race_where_journal_disappears(monkeypatch, restart_spy):
    """两次检查之间 journal 被别的进程清掉：继续启动，不重启。"""
    monkeypatch.setattr(independent, 'trial_pending', lambda: True)
    monkeypatch.setattr(independent, 'recover', lambda: False)

    assert gui._recover_pending_update() is None
    assert restart_spy == [], '没真的回滚就不该重启'


def test_restart_failure_is_reported(monkeypatch):
    """execv 失败时给出可读提示并返回启动失败码。"""
    def boom():
        raise OSError('execv not supported')

    monkeypatch.setattr(independent, 'trial_pending', lambda: True)
    monkeypatch.setattr(independent, 'recover', lambda: True)
    monkeypatch.setattr(gui, '_restart_self', boom)

    assert gui._recover_pending_update() == gui.EXIT_STARTUP_FAILURE


def test_missing_module_does_not_block_startup(monkeypatch):
    """精简部署没有 deploy.independent 时，启动不能被拖住。

    用 __import__ 打桩模拟 ImportError：真把模块从 sys.modules 摘掉
    会连带污染其它测试。
    """
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == 'deploy.independent':
            raise ImportError('No module named deploy.independent')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', fake_import)

    assert gui._recover_pending_update() is None


def test_recovery_exit_code_is_distinct():
    """新退出码不能与既有码段冲突，启动器要靠它区分展示。"""
    existing = {
        gui.EXIT_SUCCESS, gui.EXIT_STARTUP_FAILURE, gui.EXIT_WORKER_CLEANUP_FAILURE,
        gui.EXIT_DEPENDENCY_SYNC_FAILURE, gui.EXIT_FRONTEND_BUILD_FAILURE,
        gui.EXIT_SUBPROCESS_SPAWN_FAILURE, gui.EXIT_PORT_LISTEN_TIMEOUT,
        gui.EXIT_WEBUI_RUNTIME_CRASH, gui.EXIT_PROCESS_TERMINATE_FAILURE,
        gui.EXIT_IPC_FAILURE,
    }
    assert gui.EXIT_UPDATE_RECOVERY_FAILURE not in existing
    assert 0 < gui.EXIT_UPDATE_RECOVERY_FAILURE < 128, '退出码要落在常规区间以便 bat 读'


def test_restart_self_uses_current_argv(monkeypatch):
    """重启必须沿用原命令行参数，否则会丢掉 --port 之类的设置。"""
    captured = {}

    def fake_execv(executable, argv):
        captured['executable'] = executable
        captured['argv'] = argv

    monkeypatch.setattr(gui.os, 'execv', fake_execv)
    monkeypatch.setattr(gui.sys, 'argv', ['gui.py', '--port', '25548'])
    monkeypatch.setattr(gui.sys, 'executable', r'C:\pack\runtime\python.exe')

    gui._restart_self()

    assert captured['executable'] == r'C:\pack\runtime\python.exe'
    assert captured['argv'] == [r'C:\pack\runtime\python.exe', 'gui.py', '--port', '25548']
