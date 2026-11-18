"""通过 FastInputIME 输入中文与特殊字符。

uiautomator2 的 `send_keys` / `send_action` 走的是它自带的 legacy IME 通道，
只对 ASCII 可靠。装备码、昵称、活动口令这类含中文或全角符号的文本用它输入会
静默丢字，或者干脆没反应。

FastInputIME 是 atx-agent 自带的一个输入法，它监听的广播才是正路：

    am broadcast -a ADB_SET_TEXT    --es text <base64>
    am broadcast -a ADB_EDITOR_CODE --ei code 6        # code 6 = IME_ACTION_DONE

文本用 base64 传，中文、emoji、换行都不会在 shell 层被转义吃掉。

两个前提必须确认，否则广播发出去也没人接：

1. FastInputIME 是当前输入法（`current_ime` 返回它）
2. 软键盘真的弹出来了（`shown=True`）

第 2 条容易被忽略：模拟器开了"物理键盘"模式时，输入法会切过去但键盘不显示，
此时广播能到但焦点框可能还没建立。所以这里等的是 `(FASTINPUT_IME, shown=True)`，
不是只等切换成功。
"""
import base64

from module.base.timer import Timer
from module.device.method.uiautomator_2 import Uiautomator2, retry
from module.exception import EmulatorNotRunningError, GameStuckError, GameTooManyClickError, RequestHumanTakeover
from module.logger import logger


class FastInputIME(Uiautomator2):
    """FastInputIME 输入方式 mixin，适用于 uiautomator2 控制的设备。"""

    # atx-agent 内置输入法的组件名，`current_ime` 返回的就是这个字符串
    FASTINPUT_IME = 'com.github.uiautomator/.FastInputIME'
    # 等键盘弹出的总时限（秒），以及点击输入框的重试间隔
    IME_WAIT = 20
    CLICK_INTERVAL = 1

    def _fastinput_ime_prepare_onscreen_keyboard(self):
        """尽量打开软键盘显示；模拟器侧的偏好设置可能还得手工改。

        BlueStacks Air 默认把软键盘藏起来（它认为有物理键盘），
        `show_ime_with_hard_keyboard` 是控制这个的系统设置项。
        """
        if not self.is_bluestacks_air:
            return
        setting = 'show_ime_with_hard_keyboard'
        try:
            if self.adb_shell(['settings', 'get', 'secure', setting]).strip() == '1':
                return
            self.adb_shell(['settings', 'put', 'secure', setting, '1'])
            # 写完再读一次，设置可能被系统策略挡住
            if self.adb_shell(['settings', 'get', 'secure', setting]).strip() == '1':
                return
        except (EmulatorNotRunningError, RequestHumanTakeover):
            raise
        except Exception as e:
            logger.warning(f'[设备-输入] 无法开启软键盘显示: {e}')
        self._fastinput_ime_log_failure_hint()

    def _fastinput_ime_log_failure_hint(self):
        """给用户一条能自己动手的提示，而不是只丢一句失败。"""
        if self.is_bluestacks_air:
            logger.warning('[设备-输入] 请在 BlueStacks Air 的输入设置里启用屏幕键盘')
        elif self.is_mumu_over_version_400:
            logger.warning('[设备-输入] MuMu 可能禁止修改默认输入法，请在 Android 设置里手动选择 FastInputIME')

    def _set_fastinput_ime(self):
        """切换输入法并回读确认。

        Returns:
            bool: 切换后 `current_ime` 确实是 FastInputIME。
        """
        try:
            self.u2_set_fastinput_ime(True)
        except (EmulatorNotRunningError, RequestHumanTakeover):
            raise
        except Exception as e:
            logger.warning(f'[设备-输入] 切换 FastInputIME 失败: {e}')
        return self.current_ime_adb()[0] == self.FASTINPUT_IME

    def current_ime_adb(self):
        """用 adb shell 读当前输入法与键盘可见性。

        不走 uiautomator2 的 HTTP 接口，是因为输入法相关查询在 atx-agent
        侧也偶发超时，而 `settings get secure default_input_method` 更稳。

        Returns:
            tuple[str, bool]: (输入法组件名, 软键盘是否显示)。
        """
        name = self.adb_shell(['settings', 'get', 'secure', 'default_input_method']).strip()
        shown = self.adb_shell(['dumpsys', 'input_method']).strip()
        # `mInputShown=true` 出现在 dumpsys 输出里；不同 ROM 大小写一致，但保险起见都判
        visible = 'mInputShown=true' in shown
        return name, visible

    def _enable_fastinput_ime(self):
        """到系统设置页勾选 FastInputIME，并逐个关掉竞争输入法。

        需要进设置页是因为 MuMu 新版等模拟器会拒绝 `ime set` 直接切换，
        只认用户在界面上点的这一次。
        """
        if self.is_mumu_over_version_400 or self.sdk_ver >= 35:
            switch_resource_ids = ('com.android.settings:id/switchWidget', 'android:id/switch_widget')
        else:
            switch_resource_ids = ('android:id/switch_widget', 'com.android.settings:id/switchWidget')
        # 不同 Android 版本开关的 resource-id 不一样，两种布局都留
        switch_resources = ' or '.join(f'@resource-id="{value}"' for value in switch_resource_ids)
        # 除 FastInputIME 之外、已经打开的其他输入法，一次关一个再重新扫描
        other_enabled = (
            '//*[@resource-id="android:id/title" and @text!="FastInputIME"]'
            f'/following-sibling::*[({switch_resources}) and @enabled="true" and @checked="true"]'
        )
        logger.info('[设备-输入] 在 Android 设置中启用 FastInputIME')
        self.stuck_record_clear()
        self.adb_shell(['am', 'start', '-a', 'android.settings.INPUT_METHOD_SETTINGS'])
        timeout = Timer(self.IME_WAIT).start()
        try:
            while not timeout.reached():
                self.dump_hierarchy()
                # 部分 ROM 会先弹一个确认对话框
                button = self.xpath_to_button('//*[@resource-id="android:id/button1"]')
                if button:
                    self.click(button)
                    continue
                for resource_id in switch_resource_ids:
                    button = self.xpath_to_button(
                        '//*[@resource-id="android:id/title" and @text="FastInputIME"]'
                        f'/following-sibling::*[@resource-id="{resource_id}"]')
                    if button:
                        break
                else:
                    continue
                checked = button.attrib.get('checked')
                if checked == 'false':
                    self.click(button)
                    continue
                if checked != 'true':
                    continue
                # 已经勾上 FastInputIME，再去关掉一个抢占的输入法
                button = self.xpath_to_button(f'({other_enabled})[1]')
                if button:
                    self.click(button)
                    continue
                return
            raise OSError('等待在 Android 设置中启用 FastInputIME 超时')
        finally:
            # 无条件返回键退出设置页，异常路径也不能把游戏留在后台
            self.adb_shell(['input', 'keyevent', '4'])
            self.stuck_record_clear()

    @retry(passthrough=(GameStuckError, GameTooManyClickError))
    def input_text_fastinput_ime(self, text, button):
        """替换焦点输入框的文本并提交；重试时整段重来。

        Args:
            text (str): 要输入的文本，可含中文与特殊字符。
            button (HierarchyButton | tuple): 需要点击以获得焦点的输入框。

        Raises:
            OSError: 输入法切换失败或等待键盘超时。
        """
        self._fastinput_ime_prepare_onscreen_keyboard()
        # 先点一下输入框建立焦点；`mInputShown` 为真不代表目标框已聚焦
        self.click(button)
        click_timer = Timer(self.CLICK_INTERVAL, count=3).start()
        timeout = Timer(self.IME_WAIT).start()
        while not timeout.reached():
            name, shown = self.current_ime_adb()
            if name != self.FASTINPUT_IME:
                if not self._set_fastinput_ime():
                    logger.warning('[设备-输入] adb 切换 FastInputIME 未生效，改到设置页启用')
                    self._enable_fastinput_ime()
                    if not self._set_fastinput_ime():
                        self._fastinput_ime_log_failure_hint()
                        raise OSError('无法选择 FastInputIME')
                self.click(button)
                click_timer.reset()
            elif shown:
                break
            elif click_timer.reached_and_reset():
                # 键盘还没弹就周期性补点，有些 ROM 首次点击会被忽略
                self.click(button)
            self.sleep(0.1)
        else:
            self._fastinput_ime_log_failure_hint()
            raise OSError('等待文本输入键盘弹出超时')

        # uiautomator2 的 send_keys/send_action 内部会重复做 legacy-only IME 检查，
        # 直接走广播绕开它。base64 保证中文/emoji/换行在 shell 层不被转义。
        payload = base64.b64encode(text.encode('utf-8')).decode('ascii')
        for command in (
            ['am', 'broadcast', '-a', 'ADB_SET_TEXT', '--es', 'text', payload],
            ['am', 'broadcast', '-a', 'ADB_EDITOR_CODE', '--ei', 'code', '6'],
        ):
            result = self.u2.shell(command)
            exit_code = getattr(result, 'exit_code', 0)
            if exit_code:
                output = getattr(result, 'output', '')
                raise OSError(f'FastInputIME 广播失败: {output}')
        logger.info(f'[设备-输入] 已通过 FastInputIME 提交 {len(text)} 个字符')
