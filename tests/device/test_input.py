"""FastInputIME 输入路径的回归测试。

不依赖真机：所有设备交互都用替身记录调用，只验证三件事：

1. `needs_fastinput` 的判据对不对（一个非 ASCII 就要整段走广播）
2. 走广播时命令拼得对不对（base64 载荷、code 6 提交）
3. 输入法切换失败时会不会错误地继续发广播

第 3 条最关键——广播在错误的输入法上也能"发送成功"，但没人接，
表现就是文本静默丢失。所以切换确认必须是硬前置。
"""
import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from module.device.input import Input
from module.device.method.fastinput_ime import FastInputIME


class FakeShellResult:
    """模拟 uiautomator2 的 shell 返回对象。"""

    def __init__(self, output='', exit_code=0):
        self.output = output
        self.exit_code = exit_code


class _FakeU2:
    """真机 `u2` 对象的最小替身，只实现被测路径用到的方法。"""

    def __init__(self, owner):
        self._owner = owner

    def shell(self, command, **kwargs):
        return self._owner.u2_shell(command)

    def set_fastinput_ime(self, enable):
        self._owner.u2_set_fastinput_ime(enable)


class FakeInput(Input):
    """把真实设备的交互替换成记录器。

    不调用父类 `__init__`，因为那会连真机。
    """

    FASTINPUT_IME = 'com.github.uiautomator/.FastInputIME'

    # 触发 @retry 包装器时它可能会去读这些设备属性，给个占位
    serial = 'emulator-5554'

    def __init__(self, ime='com.github.uiautomator/.FastInputIME', shown=True,
                 switch_succeeds=True, broadcast_exit_code=0):
        self._ime = ime
        self._shown = shown
        self._switch_succeeds = switch_succeeds
        self._broadcast_exit_code = broadcast_exit_code
        self.calls = []
        self.broadcasts = []

    # ── 设备侧替身 ──────────────────────────────────────────────
    def adb_shell(self, cmd, **kwargs):
        self.calls.append(('adb_shell', cmd))
        cmd = cmd if isinstance(cmd, list) else [cmd]
        if cmd[:2] == ['settings', 'get']:
            return self._ime
        return ''

    def u2_set_fastinput_ime(self, enable):
        self.calls.append(('set_ime', enable))

    def current_ime_adb(self):
        return self._ime, self._shown

    def _set_fastinput_ime(self):
        self.calls.append(('_set_fastinput_ime',))
        if self._switch_succeeds:
            self._ime = self.FASTINPUT_IME
        return self._switch_succeeds

    def click(self, button):
        self.calls.append(('click', button))

    def sleep(self, second):
        pass

    def u2_shell(self, command):
        self.broadcasts.append(list(command))
        return FakeShellResult(exit_code=self._broadcast_exit_code)

    @property
    def u2(self):
        """`u2` 在真机上是 cached_property，会去建立 atx-agent 连接。

        这里返回一个只有 `shell` 的对象，让广播路径可测。
        """
        return _FakeU2(self)

    def u2_send_keys(self, text, clear=False):
        self.calls.append(('send_keys', text, clear))

    def u2_send_action(self, code):
        self.calls.append(('send_action', code))

    def u2_clear_text(self):
        self.calls.append(('clear_text',))

    # @retry 的恢复策略会回调这些，测试里全部变成空操作
    def adb_reconnect(self):
        self.calls.append(('adb_reconnect',))

    def adb_start_server(self):
        self.calls.append(('adb_start_server',))

    @property
    def is_bluestacks_air(self):
        return False

    @property
    def is_mumu_over_version_400(self):
        return False

    @property
    def sdk_ver(self):
        return 33

    def stuck_record_clear(self):
        pass

    def _enable_fastinput_ime(self):
        """测试里不真的去点系统设置页，记录一次调用即可。"""
        self.calls.append(('enable_in_settings',))


def raw_input(device, text, button):
    """绕开 @retry 包装，直接调未包装的实现。

    `@retry` 会把 OSError 归成"未知异常"并重试到 RequestHumanTakeover，
    那是生产环境的正确行为，但会把断言对象盖掉。测试只关心单次语义。

    从 FastInputIME 的类字典里取原始函数，不经过描述符绑定：
    `Input.input_text_fastinput_ime` 拿到的仍是 @retry 包装版，
    只有摸到 `__wrapped__` 才是未包装的实现。

    Args:
        device (FakeInput): 设备替身。
        text (str): 待输入文本。
        button: 输入框。

    Returns:
        未包装函数的返回值。
    """
    func = FastInputIME.__dict__['input_text_fastinput_ime']
    return func.__wrapped__(device, text, button)


@pytest.mark.parametrize('text,expected', [
    ('ABC123', False),
    ('hello world', False),
    ('', False),
    ('中文', True),
    ('装备码：abc', True),
    ('emoji 😀', True),
    ('全角ＡＢＣ', True),
    ('tab\there', False),
])
def test_needs_fastinput(text, expected):
    """判据是"能否用 ASCII 表示"，不是"含不含中文字符"。"""
    assert FakeInput.needs_fastinput(text) is expected


def test_ascii_text_uses_short_path():
    """纯 ASCII 仍走 uiautomator2，不该无谓地切换用户输入法。"""
    device = FakeInput()
    device.text_input_and_confirm('abc123')
    kinds = [c[0] for c in device.calls]
    assert 'send_keys' in kinds
    assert 'send_action' in kinds
    assert 'set_ime' not in kinds
    assert not device.broadcasts


def test_non_ascii_requires_button():
    """需要广播时必须给输入框，否则无处建立焦点。"""
    device = FakeInput()
    with pytest.raises(ValueError, match='button'):
        device.text_input_and_confirm('中文')


def test_non_ascii_broadcasts_base64_payload():
    """中文走广播，载荷是 UTF-8 的 base64，且以 code 6 提交。"""
    device = FakeInput()
    text = '装备码：ABC-中文'
    raw_input(device, text, 'BUTTON')

    assert len(device.broadcasts) == 2

    set_text, editor_code = device.broadcasts
    assert set_text[:4] == ['am', 'broadcast', '-a', 'ADB_SET_TEXT']
    assert '--es' in set_text and 'text' in set_text
    payload = set_text[set_text.index('text') + 1]
    # 载荷必须是纯 ASCII 的 base64，否则 shell 层会转义吃掉
    payload.encode('ascii')
    assert base64.b64decode(payload).decode('utf-8') == text

    assert editor_code[:4] == ['am', 'broadcast', '-a', 'ADB_EDITOR_CODE']
    assert editor_code[editor_code.index('code') + 1] == '6'


def test_switches_ime_before_broadcasting():
    """当前输入法不对时先切换，切换成功才发广播。"""
    device = FakeInput(ime='com.other.IME')
    raw_input(device, '中文', 'BUTTON')

    kinds = [c[0] for c in device.calls]
    assert '_set_fastinput_ime' in kinds
    assert len(device.broadcasts) == 2


def test_aborts_when_switch_impossible():
    """切换彻底失败时不能继续广播，否则文本会静默丢失。"""
    device = FakeInput(ime='com.other.IME', switch_succeeds=False)

    with pytest.raises(OSError, match='FastInputIME'):
        raw_input(device, '中文', 'BUTTON')
    assert not device.broadcasts
    # 切换失败后应当尝试进设置页启用
    assert ('enable_in_settings',) in device.calls


def test_aborts_when_keyboard_never_shows():
    """输入法对但键盘不弹，要超时报错，不能盲发广播。"""
    device = FakeInput(shown=False)
    device.IME_WAIT = 0  # 立刻超时，避免测试真的等 20 秒

    with pytest.raises(OSError, match='键盘'):
        raw_input(device, '中文', 'BUTTON')
    assert not device.broadcasts


def test_propagates_broadcast_failure():
    """广播返回非零退出码要抛出，不能当成功。"""
    device = FakeInput(broadcast_exit_code=1)
    with pytest.raises(OSError, match='广播失败'):
        raw_input(device, '中文', 'BUTTON')


def test_clear_is_applied_before_broadcast():
    """clear=True 时先清空输入框。"""
    device = FakeInput()
    device.text_input_and_confirm('中文', clear=True, button='BUTTON')
    kinds = [c[0] for c in device.calls]
    assert 'clear_text' in kinds
    assert kinds.index('clear_text') < kinds.index('click')


def test_click_happens_before_waiting():
    """第一次交互必须是点击输入框，否则焦点无处建立。"""
    device = FakeInput()
    device.text_input_and_confirm('中文', button='TARGET')
    assert device.calls[0] == ('click', 'TARGET')
