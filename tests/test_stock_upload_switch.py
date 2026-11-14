"""行动力上传开关：关闭时只更新状态，不向上游上报。"""
import threading
import time
import unittest
from unittest.mock import Mock, patch

from module.api.stock_exchange_service import StockExchangeService, exchange_url


def make_service():
    """只装配 _sync_history 需要的字段，避免真实网络与文件。"""
    service = object.__new__(StockExchangeService)
    service.lock = threading.RLock()
    service.bindings = {'demo': {'stockId': 1, 'bindingKey': 'k', 'username': '股票', 'url': exchange_url()}}
    service.retired = set()
    service.statuses = {}
    service.last_status = {'demo': time.monotonic() + 600}   # 跳过状态查询
    service.protection = Mock()
    service.protection.resolve.return_value = 'identifier'
    service.history = Mock()
    service.history.scanned = {'demo': time.monotonic() + 600}
    service.history.ready.return_value = True
    service.history.pending = {}
    service.history.legacy_warnings = {}
    service._read_snapshot = Mock()
    service._remote = Mock()
    return service


class StockUploadSwitchTests(unittest.TestCase):
    def test_enabled_reports_history(self):
        service = make_service()
        with patch('module.api.stock_exchange_service.stock_upload_enabled', return_value=True), \
                patch.object(StockExchangeService, '_identity', return_value=('identifier', Mock())):
            service._sync_history('demo', False)
        self.assertTrue(service.history.synchronize.called)

    def test_disabled_skips_report_and_keeps_status(self):
        service = make_service()
        with patch('module.api.stock_exchange_service.stock_upload_enabled', return_value=False), \
                patch.object(StockExchangeService, '_identity', side_effect=AssertionError('不应取私钥')):
            service._sync_history('demo', False)
        self.assertFalse(service.history.synchronize.called, '关闭后不能上报')
        self.assertIn('关闭', service.statuses['demo'])
        self.assertTrue(service._read_snapshot.called, '状态刷新应照常')

    def test_unreadable_config_defaults_to_enabled(self):
        from module.api.stock_exchange_service import stock_upload_enabled
        fake_state = Mock()
        fake_state.deploy_config = object()   # 没有 StockUploadEnabled 属性
        with patch('module.runtime.setting.State', fake_state):
            self.assertTrue(stock_upload_enabled())


if __name__ == '__main__':
    unittest.main()
