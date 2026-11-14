"""茗交所账号档案：导出、导入与切换实例的交易所身份。

私钥由本机生成并加密保存在游戏数据保护区里（`identities/<instanceId>.json`）。
这里的"账号档案"把「身份私钥 + 股票绑定」打包成一份可迁移的 JSON，
供换设备、备份和同一实例切换身份使用。

安全边界：导出文件内含 Ed25519 私钥，等同账号本身；导入只校验格式与密钥
可用性，不会联网；激活会改写实例的 identity，因此强制备份且必须显式确认。
"""
import base64
import json
import os
import shutil
import uuid
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from deploy.atomic import atomic_write
from module.api.protocol import ApiError
from module.api.stock_exchange_identity import binding_key
from module.config.transaction import config_transaction
from module.runtime.game_data import INSTANCE_FIELD, GameDataProtector

ACCOUNT_FORMAT = 'azurpilot-stock-account'
ACCOUNT_VERSION = 1
ACCOUNT_FILE = 'stock-accounts.json'
REGISTRY = 'stocks-v2.json'
RETIRED = 'stocks-retired-v2.json'


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _load_private_key(value):
    """解析 base64 原始 32 字节 Ed25519 私钥；失败返回 None 而不是抛底层异常。"""
    if not isinstance(value, str):
        return None
    try:
        raw = base64.b64decode(value, validate=True)
        if len(raw) != 32:
            return None
        return Ed25519PrivateKey.from_private_bytes(raw)
    except (ValueError, TypeError):
        return None


def _public_b64(key):
    return base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


class StockAccountService:
    """账号档案的增删查、导入导出与身份切换。"""

    def __init__(self, configs, service):
        self.configs = configs
        self.service = service
        self.protection = GameDataProtector(configs.root)

    # ------------------------------------------------------------------ 存储
    def _read(self):
        data = self.protection.read_file(ACCOUNT_FILE)
        if data is None:
            # 首次使用还没有档案文件，返回空表而不是当成损坏。
            return {'version': ACCOUNT_VERSION, 'accounts': {}}
        if not isinstance(data, dict) or not isinstance(data.get('accounts'), dict):
            raise ApiError('STOCK_ACCOUNT_DAMAGED', '账号档案文件格式无效，请恢复备份')
        accounts = {}
        for account_id, row in data['accounts'].items():
            if not isinstance(row, dict) or not isinstance(row.get('instanceId'), str):
                raise ApiError('STOCK_ACCOUNT_DAMAGED', '账号档案内容损坏，请恢复备份')
            if _load_private_key(row.get('privateKey')) is None:
                raise ApiError('STOCK_ACCOUNT_DAMAGED', '账号档案私钥损坏，请重新导入')
            accounts[account_id] = row
        return {'version': ACCOUNT_VERSION, 'accounts': accounts}

    def _write(self, data):
        self.protection.write_file(ACCOUNT_FILE, data)

    def _binding(self):
        """读取当前股票登记（可能不存在）。"""
        value = self.protection.read_file(REGISTRY) or {}
        return value if isinstance(value, dict) else {}

    def _retired(self):
        value = self.protection.read_file(RETIRED) or {}
        return value if isinstance(value, dict) else {}

    # ------------------------------------------------------------------ 查询
    def list(self, instance):
        """列出全部档案，并标出与当前实例身份一致的那一条。"""
        self.configs.path(instance)
        current = self.protection.resolve(instance)
        rows = []
        for account_id, row in sorted(self._read()['accounts'].items(),
                                      key=lambda item: item[1].get('importedAt') or ''):
            rows.append({
                'accountId': account_id,
                'instanceId': row['instanceId'],
                'username': row.get('username') or '',
                'stockId': row.get('stockId') or 0,
                'stockUrl': row.get('stockUrl') or '',
                'note': row.get('note') or '',
                'importedAt': row.get('importedAt') or '',
                'active': row['instanceId'] == current,
            })
        return {'instance': instance, 'instanceId': current, 'accounts': rows}

    # ------------------------------------------------------------------ 导出
    def export(self, instance, account_id=None):
        """导出账号为明文 JSON 文本；未指定 accountId 时导出当前身份。"""
        self.configs.path(instance)
        data = self._read()
        current = self.protection.resolve(instance)
        if account_id:
            row = data['accounts'].get(account_id)
            if row is None:
                raise ApiError('NOT_FOUND', '账号档案不存在')
        else:
            row = next((item for item in data['accounts'].values()
                        if item['instanceId'] == current), None)
            if row is None:
                # 当前身份还没被收进档案：就地取一次，保证"导出当前账号"总能成功。
                row = self._snapshot_current(instance, current)
        key = _load_private_key(row['privateKey'])
        if key is None:
            raise ApiError('STOCK_ACCOUNT_DAMAGED', '私钥损坏，无法导出')
        payload = {
            'format': ACCOUNT_FORMAT,
            'version': ACCOUNT_VERSION,
            'exportedAt': _now(),
            'instanceId': row['instanceId'],
            'privateKey': row['privateKey'],
            'publicKey': _public_b64(key),
            'bindingKey': binding_key(row['instanceId'], key),
            'username': row.get('username') or '',
            'stockId': row.get('stockId') or 0,
            'stockUrl': row.get('stockUrl') or '',
            'note': row.get('note') or '',
        }
        name = 'zurpilot-stock-%s-%s.json' % (
            (row.get('username') or 'account').replace('/', '_').replace('\\', '_')[:24],
            row['instanceId'][:8],
        )
        return {'filename': name, 'content': json.dumps(payload, ensure_ascii=False, indent=2)}

    def _snapshot_current(self, instance, identity):
        """把实例当前身份读成档案行，不写盘。"""
        key_data = self.protection.read_file('identities/' + identity + '.json')
        if not isinstance(key_data, dict) or key_data.get('privateKey') is None:
            raise ApiError('STOCK_IDENTITY_DAMAGED', '实例交易身份损坏，请恢复备份')
        binding = self._binding().get(identity) or {}
        return {'accountId': str(uuid.uuid4()), 'instanceId': identity,
                'privateKey': key_data['privateKey'], 'username': binding.get('username') or '',
                'stockId': binding.get('stockId') or 0, 'stockUrl': binding.get('url') or '',
                'note': '', 'importedAt': _now()}

    # ------------------------------------------------------------------ 导入
    def import_account(self, payload, note=''):
        """校验并保存一份导出文件；只校验格式与密钥，不联网、不改当前身份。"""
        if not isinstance(payload, str) or not payload.strip():
            raise ApiError('INVALID_PARAMS', '请提供账号文件内容')
        if len(payload) > 64 * 1024:
            raise ApiError('INVALID_PARAMS', '账号文件过大')
        try:
            row = json.loads(payload)
        except ValueError:
            raise ApiError('STOCK_ACCOUNT_INVALID', '账号文件不是合法 JSON') from None
        if not isinstance(row, dict) or row.get('format') != ACCOUNT_FORMAT:
            raise ApiError('STOCK_ACCOUNT_INVALID', '不是 zurPilot 账号文件')
        if row.get('version') != ACCOUNT_VERSION:
            raise ApiError('STOCK_ACCOUNT_INVALID', '账号文件版本不受支持')
        instance_id = row.get('instanceId')
        try:
            if str(uuid.UUID(instance_id)) != instance_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ApiError('STOCK_ACCOUNT_INVALID', '账号文件 instanceId 无效') from None
        key = _load_private_key(row.get('privateKey'))
        if key is None:
            raise ApiError('STOCK_ACCOUNT_INVALID', '账号文件私钥无效')
        # 公钥/绑定码是派生值，与私钥不一致说明文件被改过，直接拒绝。
        expected_public = _public_b64(key)
        if row.get('publicKey') and row['publicKey'] != expected_public:
            raise ApiError('STOCK_ACCOUNT_INVALID', '账号文件公钥与私钥不匹配')
        expected_binding = binding_key(instance_id, key)
        if row.get('bindingKey') and row['bindingKey'] != expected_binding:
            raise ApiError('STOCK_ACCOUNT_INVALID', '账号文件绑定码与私钥不匹配')
        stock_id = row.get('stockId') or 0
        if type(stock_id) is not int or stock_id < 0:
            raise ApiError('STOCK_ACCOUNT_INVALID', '账号文件股票编号无效')
        username = (row.get('username') or '').strip()
        if username and not 2 <= len(username) <= 32:
            raise ApiError('STOCK_ACCOUNT_INVALID', '账号文件股票名称长度无效')

        with self.protection.transaction():
            data = self._read()
            existing = next((item for item in data['accounts'].values()
                             if item['instanceId'] == instance_id), None)
            if existing is not None:
                # 同一身份重复导入按更新处理，保留原 accountId，避免列表堆积。
                existing.update({'privateKey': row['privateKey'], 'username': username or existing.get('username') or '',
                                 'stockId': stock_id or existing.get('stockId') or 0,
                                 'stockUrl': row.get('stockUrl') or existing.get('stockUrl') or '',
                                 'note': note or existing.get('note') or ''})
                self._write(data)
                return {'accountId': existing['accountId'], 'updated': True,
                        'instanceId': instance_id, 'username': existing.get('username') or '',
                        'stockId': existing.get('stockId') or 0}
            account_id = str(uuid.uuid4())
            data['accounts'][account_id] = {
                'accountId': account_id, 'instanceId': instance_id, 'privateKey': row['privateKey'],
                'username': username, 'stockId': stock_id,
                'stockUrl': row.get('stockUrl') or '', 'note': note or '', 'importedAt': _now(),
            }
            self._write(data)
        return {'accountId': account_id, 'updated': False, 'instanceId': instance_id,
                'username': username, 'stockId': stock_id}

    def remove(self, account_id):
        """删除档案（只删本地档案，不影响交易所上的股票与当前身份）。"""
        with self.protection.transaction():
            data = self._read()
            if account_id not in data['accounts']:
                raise ApiError('NOT_FOUND', '账号档案不存在')
            data['accounts'].pop(account_id)
            self._write(data)
        return {'removed': True}

    # ------------------------------------------------------------------ 切换
    def plan(self, instance, account_id):
        """返回切换计划；confirm=False 时由前端展示影响范围后再确认。"""
        self.configs.path(instance)
        data = self._read()
        row = data['accounts'].get(account_id)
        if row is None:
            raise ApiError('NOT_FOUND', '账号档案不存在')
        current = self.protection.resolve(instance)
        if row['instanceId'] == current:
            return {'instance': instance, 'accountId': account_id, 'currentInstanceId': current,
                    'targetInstanceId': current, 'username': row.get('username') or '',
                    'stockId': row.get('stockId') or 0, 'switched': False,
                    'message': '当前实例已经在使用这个账号'}
        if row['instanceId'] in self._retired():
            raise ApiError('STOCK_DELETED', '该身份的股票已被管理员删除，不能切换过去')
        return {'instance': instance, 'accountId': account_id, 'currentInstanceId': current,
                'targetInstanceId': row['instanceId'], 'username': row.get('username') or '',
                'stockId': row.get('stockId') or 0, 'switched': False,
                'message': '切换后本实例的 identity 会变为该账号，历史资源与调度历史按新身份重新归属'}

    def activate(self, instance, account_id, confirm=False):
        """把档案设为实例身份：备份 → 写私钥 → 写绑定 → 改配置 Identity → 登记状态。"""
        plan = self.plan(instance, account_id)
        if not plan['switched'] and not confirm:
            return plan
        if plan['switched']:
            return plan

        row = self._read()['accounts'][account_id]
        key = _load_private_key(row['privateKey'])
        if key is None:
            raise ApiError('STOCK_ACCOUNT_DAMAGED', '私钥损坏，无法切换')
        target = row['instanceId']

        with self.protection.transaction() as (state, _):
            config_path = self.configs.path(instance)
            content = json.loads(config_path.read_bytes())
            if not isinstance(content, dict) or not isinstance(content.get('Alas'), dict):
                raise ApiError('CONFIG_INVALID', '实例配置损坏，请先修复配置')
            with config_transaction(config_path):
                backup = self._backup(instance, config_path)
                self.protection.write_file('identities/' + target + '.json',
                                           {'instanceId': target, 'privateKey': row['privateKey']})
                binding = self._binding()
                if row.get('stockId') and row.get('stockUrl'):
                    binding[target] = {'stockId': int(row['stockId']), 'username': row.get('username') or '',
                                       'bindingKey': binding_key(target, key), 'url': row['stockUrl'],
                                       'capabilities': []}
                    self.protection.write_file(REGISTRY, binding)
                previous = content.get(INSTANCE_FIELD)
                content[INSTANCE_FIELD] = target
                atomic_write(str(config_path), json.dumps(content, ensure_ascii=False, indent=2))
                predecessors = [k for k, v in state['instances'].items() if v.get('name') == instance]
                state['instances'][target] = {
                    'name': instance, 'active': True, 'schedulerName': instance, 'legacy': False,
                    'predecessor': predecessors[-1] if predecessors else None,
                    'baseline': False, 'legacyStatsName': None,
                }
                if previous and previous != target and previous in state['instances']:
                    state['instances'][previous]['active'] = False

        # 切换后让服务重建缓存与监视，避免继续用旧身份上传。
        self.service.reload_identity(instance)
        result = self.plan(instance, account_id)
        result.update({'switched': True, 'backup': str(backup),
                       'message': '已切换账号；历史资源按新身份重新归属，需要时可用备份回退'})
        return result

    def _backup(self, instance, config_path):
        root = self.protection.root / 'config' / 'backup' / ('stock-switch-' + uuid.uuid4().hex)
        self.protection._safe(root)
        root.mkdir(parents=True, mode=0o700)
        if self.protection.directory.exists():
            shutil.copytree(self.protection.directory, root / 'stock-exchange',
                            ignore=shutil.ignore_patterns('*.lock'))
        shutil.copyfile(config_path, root / config_path.name)
        return root
