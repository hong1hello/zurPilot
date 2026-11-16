"""茗交所账号档案：导入校验、导出内容、切换计划与身份改写。"""
import base64
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from module.api.protocol import ApiError
from module.api.stock_exchange_accounts import StockAccountService
from module.api.stock_exchange_identity import binding_key
from module.runtime.game_data import INSTANCE_FIELD, GameDataProtector

EXCHANGE = 'https://stock.azurpilot.com'
INSTANCE = '3f2504e0-4f89-11d3-9a0c-0305e82c3301'


def make_key():
    key = Ed25519PrivateKey.generate()
    private = base64.b64encode(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode()
    public = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    return key, private, public


def make_payload(username='测试股票', stock_id=4242, **overrides):
    key, private, public = make_key()
    payload = {
        'format': 'azurpilot-stock-account', 'version': 1,
        'exportedAt': '2026-11-14T00:00:00+00:00',
        'instanceId': INSTANCE, 'privateKey': private, 'publicKey': public,
        'bindingKey': binding_key(INSTANCE, key),
        'username': username, 'stockId': stock_id, 'stockUrl': EXCHANGE, 'note': '',
    }
    payload.update(overrides)
    return payload


class StubService:
    """只记录身份刷新调用。"""

    def __init__(self):
        self.reloaded = []

    def reload_identity(self, instance):
        self.reloaded.append(instance)


class StockAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'config').mkdir()
        self.config_path = self.root / 'config' / 'test.json'
        self.config_path.write_text(json.dumps({'Alas': {}}), encoding='utf-8')

        def path(name):
            result = self.root / 'config' / (name + '.json')
            if not result.exists():
                raise ApiError('NOT_FOUND', '实例不存在')
            return result

        self.configs = SimpleNamespace(root=self.root, path=Mock(side_effect=path))
        self.stub = StubService()
        self.service = StockAccountService(self.configs, self.stub)
        self.protection = GameDataProtector(self.root)

    def import_payload(self, payload, note=''):
        return self.service.import_account(json.dumps(payload, ensure_ascii=False), note)

    # ------------------------------------------------------------------ 导入
    def test_import_rejects_broken_files(self):
        cases = [
            ('不是 json', 'STOCK_ACCOUNT_INVALID'),
            (json.dumps({'format': 'other'}), 'STOCK_ACCOUNT_INVALID'),
            (json.dumps({'format': 'azurpilot-stock-account', 'version': 99}), 'STOCK_ACCOUNT_INVALID'),
            (json.dumps({'format': 'azurpilot-stock-account', 'version': 1, 'instanceId': 'x'}), 'STOCK_ACCOUNT_INVALID'),
        ]
        for text, code in cases:
            with self.subTest(text=text[:24]):
                with self.assertRaises(ApiError) as caught:
                    self.service.import_account(text)
                self.assertEqual(caught.exception.code, code)
        with self.assertRaises(ApiError) as caught:
            self.service.import_account('')
        self.assertEqual(caught.exception.code, 'INVALID_PARAMS')

    def test_import_rejects_tampered_public_key_or_binding(self):
        """公钥/绑定码是私钥的派生值，对不上说明文件被改过。"""
        for field in ('publicKey', 'bindingKey'):
            with self.subTest(field=field):
                payload = make_payload(**{field: base64.b64encode(b'1' * 32).decode()})
                with self.assertRaises(ApiError) as caught:
                    self.import_payload(payload)
                self.assertEqual(caught.exception.code, 'STOCK_ACCOUNT_INVALID')

    def test_import_is_idempotent_per_identity(self):
        payload = make_payload()
        first = self.import_payload(payload, note='第一次')
        second = self.import_payload(payload, note='第二次')
        self.assertFalse(first['updated'])
        self.assertTrue(second['updated'])
        self.assertEqual(first['accountId'], second['accountId'])
        listing = self.service.list('test')
        self.assertEqual(len(listing['accounts']), 1)
        # 重复导入按更新处理：新备注覆盖旧的，accountId 保持不变。
        self.assertEqual(listing['accounts'][0]['note'], '第二次')

    def test_import_tolerates_utf8_bom(self):
        """记事本 / PowerShell 保存的 UTF-8 会带 BOM，不该因此被拒。"""
        payload = make_payload(username='BOM 测试')
        text = '\ufeff' + json.dumps(payload, ensure_ascii=False, indent=2)
        result = self.service.import_account(text)
        self.assertEqual(result['username'], 'BOM 测试')

    def test_import_tolerates_uppercase_instance_id(self):
        """UUID 大小写等价；归档时统一成小写。"""
        key, private, public = make_key()
        upper = INSTANCE.upper()
        payload = {
            'format': 'azurpilot-stock-account', 'version': 1, 'exportedAt': '2026-11-14T00:00:00+00:00',
            'instanceId': upper, 'privateKey': private, 'publicKey': public,
            'bindingKey': binding_key(upper, key),
            'username': '大写测试', 'stockId': 1, 'stockUrl': EXCHANGE,
        }
        result = self.import_payload(payload)
        self.assertEqual(result['instanceId'], INSTANCE)
        self.assertEqual(self.service.list('test')['accounts'][0]['instanceId'], INSTANCE)

    def test_import_accepts_files_without_derived_fields(self):
        """只带私钥也能导入：派生字段缺失不是错误。"""
        payload = make_payload()
        for field in ('publicKey', 'bindingKey', 'stockUrl'):
            payload.pop(field)
        result = self.import_payload(payload)
        self.assertEqual(result['instanceId'], INSTANCE)

    def test_activate_drops_previous_action_point_chain_and_anchor(self):
        """换身份后旧链必须撤掉：否则新身份用不同密钥校验会报「哈希链丢失」。"""
        previous = self.protection.resolve('test')
        database = self.root / 'config' / 'scheduler' / 'test.sqlite3'
        database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(database))) as db, db:
            db.execute('CREATE TABLE observations (resource TEXT, total INTEGER, observed_at TEXT)')
            db.execute('INSERT INTO observations VALUES(?,?,?)', ('ActionPoint', 1234, '2026-11-16 00:00:00'))
            db.execute('CREATE TABLE action_point_chain (seq INTEGER PRIMARY KEY, observed_at TEXT NOT NULL,'
                       ' total INTEGER NOT NULL, previous_hash TEXT NOT NULL, hash TEXT NOT NULL)')
            db.execute('CREATE TABLE action_point_chain_owner (id INTEGER PRIMARY KEY, instance_id TEXT NOT NULL)')
            db.execute('INSERT INTO action_point_chain_owner VALUES(1,?)', (previous,))
        self.protection.anchor(previous + '/action-point-history', 'a' * 64, prepare=True)
        self.assertTrue(self.protection.has_anchor(previous + '/action-point-history'))

        account = self.import_payload(make_payload())
        self.service.activate('test', account['accountId'], confirm=True)

        with closing(sqlite3.connect(str(database))) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            left = db.execute('SELECT COUNT(*) FROM observations').fetchone()[0]
        self.assertNotIn('action_point_chain', tables)
        self.assertNotIn('action_point_chain_owner', tables)
        self.assertIn('observations', tables, '普通调度数据不能被清掉')
        self.assertEqual(left, 1)
        self.assertFalse(self.protection.has_anchor(previous + '/action-point-history'),
                         '链表撤销后 anchor 必须一起清')
        backup = self.root / 'config' / 'backup' / ('stock-' + previous) / 'scheduler' / 'test.sqlite3'
        self.assertTrue(backup.exists(), '旧库要先备份')

    # ------------------------------------------------------------------ 导出
    def test_export_contains_private_key_and_derived_fields(self):
        payload = make_payload(username='导出测试', stock_id=777)
        account = self.import_payload(payload)
        exported = self.service.export('test', account['accountId'])
        content = json.loads(exported['content'])
        self.assertEqual(content['privateKey'], payload['privateKey'])
        self.assertEqual(content['publicKey'], payload['publicKey'])
        self.assertEqual(content['bindingKey'], payload['bindingKey'])
        self.assertEqual(content['stockId'], 777)
        self.assertTrue(exported['filename'].startswith('zurpilot-stock-'))
        self.assertTrue(exported['filename'].endswith('.json'))

    def test_export_without_account_id_uses_current_identity(self):
        """当前身份还没入档时，导出应就地快照而不是报错。"""
        identity = self.protection.resolve('test')
        exported = self.service.export('test')
        content = json.loads(exported['content'])
        self.assertEqual(content['instanceId'], identity)
        self.assertEqual(content['privateKey'], self.protection.read_file('identities/' + identity + '.json')['privateKey'])

    # ------------------------------------------------------------------ 切换
    def test_activate_requires_confirmation_then_switches_identity(self):
        payload = make_payload(username='切过去', stock_id=999)
        account = self.import_payload(payload)
        before = self.protection.resolve('test')

        plan = self.service.activate('test', account['accountId'])
        self.assertFalse(plan['switched'])
        self.assertEqual(plan['currentInstanceId'], before)
        self.assertEqual(plan['targetInstanceId'], payload['instanceId'])
        self.assertEqual(self.protection.resolve('test'), before, '未确认时不能改身份')

        done = self.service.activate('test', account['accountId'], confirm=True)
        self.assertTrue(done['switched'])
        self.assertTrue(Path(done['backup']).is_dir())
        self.assertEqual(self.protection.resolve('test'), payload['instanceId'])
        self.assertEqual(self.stub.reloaded, ['test'])

        saved = json.loads(self.config_path.read_text(encoding='utf-8'))
        self.assertEqual(saved[INSTANCE_FIELD], payload['instanceId'])
        listing = self.service.list('test')
        self.assertTrue(listing['accounts'][0]['active'])

    def test_activate_same_identity_is_noop(self):
        identity = self.protection.resolve('test')
        exported = json.loads(self.service.export('test')['content'])
        account = self.import_payload(exported)
        plan = self.service.activate('test', account['accountId'])
        self.assertFalse(plan['switched'])
        self.assertEqual(plan['targetInstanceId'], identity)
        self.assertEqual(self.stub.reloaded, [])

    def test_activate_rejects_retired_identity(self):
        payload = make_payload()
        account = self.import_payload(payload)
        self.protection.write_file('stocks-retired-v2.json', {payload['instanceId']: True})
        with self.assertRaises(ApiError) as caught:
            self.service.activate('test', account['accountId'])
        self.assertEqual(caught.exception.code, 'STOCK_DELETED')

    def test_activate_restores_binding_for_imported_stock(self):
        payload = make_payload(stock_id=1234)
        account = self.import_payload(payload)
        self.service.activate('test', account['accountId'], confirm=True)
        binding = self.protection.read_file('stocks-v2.json')[payload['instanceId']]
        self.assertEqual(binding['stockId'], 1234)
        self.assertEqual(binding['url'], EXCHANGE)
        self.assertEqual(binding['bindingKey'], binding_key(payload['instanceId'], Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(payload['privateKey']))))

    # ------------------------------------------------------------------ 删除
    def test_remove_only_deletes_local_profile(self):
        account = self.import_payload(make_payload())
        identity = self.protection.resolve('test')
        self.service.remove(account['accountId'])
        self.assertEqual(self.service.list('test')['accounts'], [])
        self.assertEqual(self.protection.resolve('test'), identity, '删档案不能影响当前身份')
        with self.assertRaises(ApiError):
            self.service.remove(account['accountId'])


if __name__ == '__main__':
    unittest.main()
