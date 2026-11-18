"""设备文本输入模块。

封装 Android 设备端的文本输入功能：输入法窗口状态检测、ASCII 快速路径，
以及含中文/特殊字符时走 FastInputIME 广播的可靠路径。

为什么分两条路：
uiautomator2 的 `send_keys` 走 atx-agent 的 legacy IME 通道，只对 ASCII 可靠。
含中文、全角符号、emoji 的文本用它输入会丢字或毫无反应，所以这类文本改走
FastInputIME 广播（见 module.device.method.fastinput_ime）。
纯 ASCII 仍然用短路径——它更快，也不会无谓地切换用户的输入法。
"""
from module.device.method.fastinput_ime import FastInputIME
from module.logger import logger


class Input(FastInputIME):
    """设备文本输入处理器。

    继承链：Input → FastInputIME → Uiautomator2，底层输入接口一路可用。

    Methods:
        ime_shown: 检测输入法窗口是否显示。
        text_input_and_confirm: 输入文本并发送确认动作。
        needs_fastinput: 判断这段文本是否必须走 FastInputIME。
    """

    def ime_shown(self) -> bool:
        """检测当前输入法（IME）窗口是否正在显示。

        Returns:
            bool: 输入法窗口可见返回 True，否则返回 False。
        """
        _, shown = self.current_ime_adb()
        return shown

    @staticmethod
    def needs_fastinput(text: str) -> bool:
        """判断文本是否必须走 FastInputIME 广播。

        判据是"能不能用 ASCII 表示"。只要出现一个非 ASCII 字符，
        uiautomator2 的 send_keys 就不可靠了，整段都要走广播——
        不能只把非 ASCII 部分挑出来单独发，那样会打乱原有顺序。

        Args:
            text (str): 待输入文本。

        Returns:
            bool: 含非 ASCII 字符时返回 True。
        """
        try:
            text.encode('ascii')
            return False
        except UnicodeEncodeError:
            return True

    def text_input_and_confirm(self, text: str, clear: bool=False, button=None):
        """向当前焦点输入框发送文本并按确认键（IME_ACTION_DONE）。

        含非 ASCII 字符且传了 `button` 时走 FastInputIME；否则走原来的
        uiautomator2 短路径。后者最多重试 3 次，覆盖输入法偶发无响应。

        Args:
            text (str): 要输入的文本内容。
            clear (bool): 输入前是否清空输入框已有内容。
            button (HierarchyButton | tuple, optional): 输入框控件。走
                FastInputIME 时必填（要先点击建立焦点）；纯 ASCII 时可省略。

        Raises:
            ValueError: 文本需要 FastInputIME 但没给 `button`。
        """
        if self.needs_fastinput(text):
            if button is None:
                raise ValueError(f'输入含非 ASCII 字符的文本需要传入 button 以建立焦点: {text!r}')
            logger.info(f'[设备-输入] 文本含非 ASCII 字符，改用 FastInputIME: {len(text)} 字符')
            if clear:
                # 广播的 ADB_SET_TEXT 是整体替换，clear 语义天然满足；
                # 这里额外清一次是为了旧版 FastInputIME 仅做追加的场景。
                self.u2_clear_text()
            self.input_text_fastinput_ime(text, button)
            return

        for fail_count in range(3):
            try:
                self.u2_send_keys(text=text, clear=clear)
                self.u2_send_action(6)
                break
            except EnvironmentError as e:
                if fail_count >= 2:
                    raise e
                logger.exception(f'{e} 正在重试 {fail_count + 1}/3')
