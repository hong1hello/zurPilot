"""便携包发布清单命令行工具。

三条子命令：

    # 1. 构建机上生成一对密钥（私钥自己留着，公钥随源码提交）
    python scripts/release_manifest.py keygen --out keys

    # 2. 打包完成后签名
    python scripts/release_manifest.py sign --root ../build-win/out/AzurPilot-Windows-x64 \\
        --key keys/ed25519-private.pem --revision <40 位 commit>

    # 3. 用户侧校验
    python scripts/release_manifest.py verify --root <解压目录> \\
        --public keys/ed25519-public.pem

`verify` 不带 `--public` 时会依次找：包内 `keys/ed25519-public.pem`、
环境变量 `AZURPILOT_PUBKEY`、脚本同目录下的 `keys/ed25519-public.pem`。
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_manifest as manifest


def cmd_keygen(args):
    """生成 Ed25519 密钥对。"""
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    private_path = out / 'ed25519-private.pem'
    public_path = out / 'ed25519-public.pem'
    if private_path.exists() and not args.force:
        print(f'{private_path} 已存在，加 --force 覆盖（覆盖后旧签名全部失效）')
        return 1

    if not _write_keys(private_path, public_path, args.password or None):
        return 1
    print(f'私钥: {private_path}  （不要提交，不要分发）')
    print(f'公钥: {public_path}  （提交进仓库，随包分发）')
    print(f'公钥 SHA-256: {manifest.digest(public_path)}')
    return 0


def _write_keys(private_path, public_path, password):
    """按可用后端生成密钥对并写盘。

    Args:
        private_path (Path): 私钥输出路径。
        public_path (Path): 公钥输出路径。
        password (str | None): 私钥口令，None 表示不加密。

    Returns:
        bool: 成功写盘返回 True。

    Raises:
        SystemExit: 两个后端都不可用时抛出，提示先跑 uv sync。
    """
    if manifest._has_cryptography():
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        secret = password.encode() if password else None
        encryption = (
            serialization.BestAvailableEncryption(secret) if secret
            else serialization.NoEncryption()
        )
        key = Ed25519PrivateKey.generate()
        private_path.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=encryption,
        ))
        public_path.write_bytes(key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ))
        return True

    try:
        from Crypto.PublicKey import ECC
    except ImportError:
        print('两个签名后端都不可用：先跑 uv sync（需要 cryptography 或 pycryptodome）', file=sys.stderr)
        raise SystemExit(1)

    key = ECC.generate(curve='ed25519')
    private_pem = key.export_key(
        format='PEM', passphrase=password, protection='PBKDF2WithHMAC-SHA512AndAES256-CBC'
    ) if password else key.export_key(format='PEM')
    private_path.write_text(private_ensure_pkcs8(private_pem), encoding='ascii')
    public_path.write_text(key.public_key().export_key(format='PEM'), encoding='ascii')
    return True


def private_ensure_pkcs8(pem):
    """pycryptodome 导出的私钥头与 cryptography 一致，这里只做换行规范化。

    Args:
        pem (str): PEM 文本。

    Returns:
        str: 以换行结尾的 PEM 文本。
    """
    return pem if pem.endswith('\n') else pem + '\n'


def cmd_sign(args):
    """给目录签名，写出 release.json 与 release.sig。"""
    root = Path(args.root).resolve()
    payload = manifest.build(root)
    if args.revision:
        if not manifest._HEX40.fullmatch(args.revision):
            print('revision 必须是 40 位十六进制 commit sha', file=sys.stderr)
            return 1
        payload['revision'] = args.revision
    if args.sequence:
        payload['sequence'] = args.sequence

    problems = manifest.validate(payload)
    if problems:
        print('清单格式不合规：', file=sys.stderr)
        for item in problems:
            print(f'  - {item}', file=sys.stderr)
        return 1

    try:
        signature = manifest.sign(
            payload, args.key,
            password=args.password.encode() if args.password else None,
        )
    except ImportError:
        print('缺少 cryptography，先跑 uv sync', file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError) as e:
        print(f'签名失败: {e}', file=sys.stderr)
        return 1

    manifest.write_manifest(root, payload, signature)
    print(f'已写出 {root / manifest.MANIFEST_NAME}')
    print(f'  文件数: {payload["fileCount"]}')
    print(f'  总字节: {payload["totalBytes"]}')
    print(f'  汇总哈希: {payload["filesSha256"]}')
    print(f'  依赖指纹: {payload["dependencies"]}')
    print(f'  revision: {payload["revision"]}')
    return 0


def find_public_key(args, root):
    """按优先级找公钥。"""
    if args.public:
        return Path(args.public)
    candidates = [
        root / 'keys' / 'ed25519-public.pem',
        Path(os.environ.get('AZURPILOT_PUBKEY', '')),
        Path(__file__).resolve().parent / 'keys' / 'ed25519-public.pem',
    ]
    for path in candidates:
        if str(path) and path.is_file():
            return path
    return None


def cmd_verify(args):
    """校验目录与清单是否一致。"""
    root = Path(args.root).resolve()
    document = manifest.read_manifest(root)
    if document is None:
        print(f'{root} 里没有可解析的 {manifest.MANIFEST_NAME}', file=sys.stderr)
        return 1
    payload = document.get('release')
    signature = document.get('signature', '')

    problems = manifest.validate(payload)
    if problems:
        print('清单格式不合规：', file=sys.stderr)
        for item in problems:
            print(f'  - {item}', file=sys.stderr)
        return 1

    public_key = find_public_key(args, root)
    if public_key is None:
        print('找不到公钥，用 --public 指定，或设 AZURPILOT_PUBKEY', file=sys.stderr)
        return 1

    if not manifest.verify(payload, signature, public_key):
        print(f'签名校验失败（公钥 {public_key}）', file=sys.stderr)
        return 1
    print(f'签名校验通过（公钥 {public_key}）')

    # 签名只证明清单本身没被改；清单与磁盘是否一致要重算一遍汇总哈希。
    current = manifest.build(root)
    if current['filesSha256'] != payload['filesSha256']:
        print('汇总哈希不一致，文件被改动过', file=sys.stderr)
        print(f'  清单: {payload["filesSha256"]}', file=sys.stderr)
        print(f'  实测: {current["filesSha256"]}', file=sys.stderr)
        print(f'  清单 {payload["fileCount"]} 个文件 / {payload["totalBytes"]} 字节', file=sys.stderr)
        print(f'  实测 {current["fileCount"]} 个文件 / {current["totalBytes"]} 字节', file=sys.stderr)
        return 1
    print(f'内容校验通过：{current["fileCount"]} 个文件，{current["totalBytes"]} 字节')

    if not args.skip_dependency:
        try:
            actual_dependencies = manifest.dependency_id(root)
        except FileNotFoundError as e:
            print(f'依赖指纹无法计算: {e}', file=sys.stderr)
            return 1
        if actual_dependencies != payload['dependencies']:
            print('依赖指纹不一致：uv.lock 或 pyproject.toml 与清单不符', file=sys.stderr)
            return 1
        print('依赖指纹一致')
    return 0


def cmd_inspect(args):
    """只打印清单内容，不做校验。"""
    document = manifest.read_manifest(Path(args.root).resolve())
    if document is None:
        print('读不到清单', file=sys.stderr)
        return 1
    print(json.dumps(document, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    """命令行入口。"""
    parser = argparse.ArgumentParser(description='便携包发布清单工具')
    sub = parser.add_subparsers(dest='command', required=True)

    keygen = sub.add_parser('keygen', help='生成 Ed25519 密钥对')
    keygen.add_argument('--out', default='keys', help='输出目录，默认 keys')
    keygen.add_argument('--password', default='', help='私钥口令，默认不加密')
    keygen.add_argument('--force', action='store_true', help='覆盖已有私钥')
    keygen.set_defaults(func=cmd_keygen)

    sign = sub.add_parser('sign', help='给目录签名')
    sign.add_argument('--root', default='.', help='便携包根目录')
    sign.add_argument('--key', required=True, help='PEM 私钥路径')
    sign.add_argument('--password', default='', help='私钥口令')
    sign.add_argument('--revision', default='', help='40 位 commit sha')
    sign.add_argument('--sequence', type=int, default=0, help='发布序号')
    sign.set_defaults(func=cmd_sign)

    verify = sub.add_parser('verify', help='校验目录与清单')
    verify.add_argument('--root', default='.', help='便携包根目录')
    verify.add_argument('--public', default='', help='PEM 公钥路径')
    verify.add_argument('--skip-dependency', action='store_true',
                        help='跳过依赖指纹比对')
    verify.set_defaults(func=cmd_verify)

    inspect = sub.add_parser('inspect', help='打印清单内容')
    inspect.add_argument('--root', default='.', help='便携包根目录')
    inspect.set_defaults(func=cmd_inspect)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
