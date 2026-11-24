"""启动期更新生命周期（gui 侧）的回归测试。

这里有两条路径，方向相反，必须都钉住：

1. `_recover_pending_update()` —— 启动最前面判断要不要回滚
   刚装完的首次启动必须放行，否则等于装完立刻自己撤掉；
   第二次启动仍是 trial，说明上次没跑到确认点，才回滚。

2. `_confirm_pending_update()` —— HTTP 真的监听成功后确认
   只有这一步才清掉回滚材料。

两条一起才构成完整事务：装 → 重启 → 能跑起来 → 确认，跑不起来 → 下轮回滚。

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
def restart_spy(monkeypatch):
    """记录是否触发了重启。"""
    calls = []
    monkeypatch.setattr(gui, '_restart_self', lambda: calls.append(1))
    return calls


def test_no_journal_starts_normally(monkeypatch, restart_spy):
    """没有待确认事务时返回 None，且不重启。"""
    monkeypatch.setattr(independent, 'bump_trial_attempt', lambda root: None)
    monkeypatch.setattr(independent, 'recover', lambda root: pytest.fail('不该回滚'))

    assert gui._recover_pending_update() is None
    assert restart_spy == []


def test_first_attempt_is_allowed_through(monkeypatch, restart_spy):
    """首次启动新版本要放行——这是刚装完的那一次。

    这里是最容易写错的地方：如果看到 trial 就回滚，用户点完更新、
    进程一重启就把刚装的东西撤了，永远更新不了。
    """
    monkeypatch.setattr(independent, 'bump_trial_attempt', lambda root: 1)
    monkeypatch.setattr(independent, 'recover', lambda: pytest.fail('首次尝试不该回滚'))

    assert gui._recover_pending_update() is None
    assert restart_spy == []


def test_second_attempt_rolls_back(monkeypatch, restart_spy):
    """第二次仍是 trial：上次没能跑到确认点，回滚。"""
    monkeypatch.setattr(independent, 'bump_trial_attempt', lambda root: 2)
    monkeypatch.setattr(independent, 'recover', lambda root: True)

    assert gui._recover_pending_update() == gui.EXIT_SUCCESS
    assert restart_spy == [1], '回滚后必须重启才能加载旧代码'


@pytest.mark.parametrize('attempts', [3, 5])
def test_repeated_failures_keep_rolling_back(monkeypatch, restart_spy, attempts):
    """连续失败多轮仍然回滚，不能因为计数变大就放行。"""
    monkeypatch.setattr(independent, 'bump_trial_attempt', lambda root: attempts)
    monkeypatch.setattr(independent, 'recover', lambda root: True)

    assert gui._recover_pending_update() == gui.EXIT_SUCCESS
    assert restart_spy == [1]


def test_recover_exception_aborts_startup(monkeypatch, restart_spy):
    """回滚本身失败时中止启动，否则会跑在半更新版本上。"""
    def boom(root):
        raise OSError('备份副本缺失')

    monkeypatch.setattr(independent, 'bump_trial_attempt', lambda root: 2)
    monkeypatch.setattr(independent, 'recover', boom)

    assert gui._recover_pending_update() == gui.EXIT_UPDATE_RECOVERY_FAILURE
    assert restart_spy == []


def test_journal_unreadable_aborts_startup(monkeypatch, restart_spy):
    """连 journal 都读不出来时无法判断，只能中止。"""
    def boom(root):
        raise ValueError('更新恢复记录无效')

    monkeypatch.setattr(independent, 'bump_trial_attempt', boom)
    monkeypatch.setattr(independent, 'recover', lambda root: pytest.fail('不该回滚'))

    assert gui._recover_pending_update() == gui.EXIT_UPDATE_RECOVERY_FAILURE
    assert restart_spy == []


def test_race_where_journal_disappears(monkeypatch, restart_spy):
    """两次检查之间 journal 被别的进程清掉：继续启动，不重启。"""
    monkeypatch.setattr(independent, 'bump_trial_attempt', lambda root: 2)
    monkeypatch.setattr(independent, 'recover', lambda root: False)

    assert gui._recover_pending_update() is None
    assert restart_spy == [], '没真的回滚就不该重启'


def test_restart_failure_is_reported(monkeypatch):
    """execv 失败时给出可读提示并返回启动失败码。"""
    def boom():
        raise OSError('execv not supported')

    monkeypatch.setattr(independent, 'bump_trial_attempt', lambda root: 2)
    monkeypatch.setattr(independent, 'recover', lambda root: True)
    monkeypatch.setattr(gui, '_restart_self', boom)

    assert gui._recover_pending_update() == gui.EXIT_STARTUP_FAILURE


def test_missing_module_does_not_block_startup(monkeypatch):
    """精简部署没有 deploy.independent 时，启动不能被拖住。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == 'deploy.independent':
            raise ImportError('No module named deploy.independent')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', fake_import)

    assert gui._recover_pending_update() is None


# ── 确认路径 ────────────────────────────────────────────────────────────────

def test_confirm_skipped_without_journal(monkeypatch):
    """没有 journal 时确认是空操作。"""
    monkeypatch.setattr(independent, 'trial_pending', lambda root: False)
    monkeypatch.setattr(independent, 'confirm', lambda root: pytest.fail('不该确认'))

    gui._confirm_pending_update()  # 不抛


def test_confirm_runs_when_trial_pending(monkeypatch):
    """有 journal 时调 confirm 清掉回滚材料。"""
    called = []
    monkeypatch.setattr(independent, 'trial_pending', lambda root: True)
    monkeypatch.setattr(independent, 'confirm', lambda root: called.append(1) or True)

    gui._confirm_pending_update()
    assert called == [1]


def test_confirm_failure_does_not_raise(monkeypatch):
    """确认失败只记日志：最坏是下次启动多回滚一次，不该影响当前服务。"""
    def boom(root):
        raise OSError('目录被占用')

    monkeypatch.setattr(independent, 'trial_pending', lambda root: True)
    monkeypatch.setattr(independent, 'confirm', boom)

    gui._confirm_pending_update()  # 不抛


# ── 退出码与重启参数 ────────────────────────────────────────────────────────

def test_recovery_exit_code_is_distinct():
    """新退出码不能与既有码冲突，启动器要靠它区分展示。"""
    existing = {
        gui.EXIT_SUCCESS, gui.EXIT_STARTUP_FAILURE, gui.EXIT_WORKER_CLEANUP_FAILURE,
        gui.EXIT_DEPENDENCY_SYNC_FAILURE, gui.EXIT_FRONTEND_BUILD_FAILURE,
        gui.EXIT_SUBPROCESS_SPAWN_FAILURE, gui.EXIT_PORT_LISTEN_TIMEOUT,
        gui.EXIT_WEBUI_RUNTIME_CRASH, gui.EXIT_PROCESS_TERMINATE_FAILURE,
        gui.EXIT_IPC_FAILURE,
    }
    assert gui.EXIT_UPDATE_RECOVERY_FAILURE not in existing
    assert 0 < gui.EXIT_UPDATE_RECOVERY_FAILURE < 128


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
