"""发布清单签名与校验的回归测试。

重点不是"签名能不能算出来"，而是四条边界：
1. 规范化必须稳定（键序、空白、非 ASCII 都不影响）
2. 篡改任一字节必须被检出
3. 换公钥必须被拒
4. 探活/日志目录必须被排除在签名范围外

后两条最容易在重构时悄悄失效，所以显式钉住。
另外还钉住 cryptography 与 pycryptodome 两个后端必须互操作——
打包机用前者，便携包侧只有后者。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

import release_manifest as manifest


def _any_backend_available():
    """至少有一个签名后端可用，整个文件才有意义。"""
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        from Crypto.PublicKey import ECC  # noqa: F401
        return True
    except ImportError:
        return False


if not _any_backend_available():
    pytest.skip('没有可用的签名后端', allow_module_level=True)


def _make_keys(tmp_path, name, backend):
    """按指定后端生成一对密钥。

    Args:
        tmp_path (Path): 临时目录。
        name (str): 文件名前缀。
        backend (str): 'cryptography' 或 'pycryptodome'。

    Returns:
        tuple[Path, Path]: (私钥路径, 公钥路径)。
    """
    private_path = tmp_path / f'{name}-private.pem'
    public_path = tmp_path / f'{name}-public.pem'
    if backend == 'cryptography':
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.generate()
        private_path.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        public_path.write_bytes(key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ))
    else:
        from Crypto.PublicKey import ECC

        key = ECC.generate(curve='ed25519')
        private_path.write_text(key.export_key(format='PEM'), encoding='ascii')
        public_path.write_text(key.public_key().export_key(format='PEM'), encoding='ascii')
    return private_path, public_path


@pytest.fixture()
def keys(tmp_path):
    """生成一对临时 Ed25519 密钥。

    用当前可用的后端生成，避免在只有 pycryptodome 的环境里直接跳过。
    """
    backend = 'cryptography' if manifest._has_cryptography() else 'pycryptodome'
    return _make_keys(tmp_path, 'fixture', backend)


@pytest.fixture()
def pkg(tmp_path):
    """最小的便携包骨架：必需文件 + 应被排除的目录。"""
    root = tmp_path / 'pkg'
    root.mkdir()
    (root / 'gui.py').write_text('print(1)\n', encoding='utf-8')
    (root / 'uv.lock').write_text('version = 1\n', encoding='utf-8')
    (root / 'pyproject.toml').write_text('[project]\nname = "x"\n', encoding='utf-8')
    (root / 'module').mkdir()
    (root / 'module' / 'a.py').write_text('A = 1\n', encoding='utf-8')
    # 这些目录不该进清单
    (root / 'runtime').mkdir()
    (root / 'runtime' / 'python.exe').write_text('binary', encoding='utf-8')
    (root / 'log').mkdir()
    (root / 'log' / 'latest.log').write_text('noise', encoding='utf-8')
    (root / 'config').mkdir()
    (root / 'config' / 'ap.json').write_text('{}', encoding='utf-8')
    return root


def test_canonical_is_stable_across_key_order():
    left = manifest.canonical({'b': 2, 'a': 1})
    right = manifest.canonical({'a': 1, 'b': 2})
    assert left == right
    # 不能有空格，且必须纯 ASCII
    assert b' ' not in left
    left.decode('ascii')


def test_canonical_escapes_non_ascii():
    assert manifest.canonical({'k': '中文'}) == b'{"k":"\\u4e2d\\u6587"}'


def test_build_excludes_runtime_log_and_config(pkg):
    payload = manifest.build(pkg)
    # gui.py / uv.lock / pyproject.toml / module/a.py 共 4 个
    assert payload['fileCount'] == 4
    assert payload['schema'] == manifest.SCHEMA
    assert len(payload['filesSha256']) == 64
    assert len(payload['dependencies']) == 64


def test_build_is_deterministic(pkg):
    assert manifest.build(pkg)['filesSha256'] == manifest.build(pkg)['filesSha256']


def test_build_reacts_to_content_change(pkg):
    before = manifest.build(pkg)['filesSha256']
    (pkg / 'module' / 'a.py').write_text('A = 2\n', encoding='utf-8')
    assert manifest.build(pkg)['filesSha256'] != before


def test_build_reacts_to_file_addition(pkg):
    before = manifest.build(pkg)['filesSha256']
    (pkg / 'module' / 'b.py').write_text('B = 1\n', encoding='utf-8')
    assert manifest.build(pkg)['filesSha256'] != before


def test_dependency_id_changes_with_lockfile(pkg):
    before = manifest.dependency_id(pkg)
    (pkg / 'uv.lock').write_text('version = 2\n', encoding='utf-8')
    assert manifest.dependency_id(pkg) != before


def test_dependency_id_requires_both_files(tmp_path):
    with pytest.raises(FileNotFoundError):
        manifest.dependency_id(tmp_path)


def test_sign_verify_round_trip(pkg, keys):
    private_path, public_path = keys
    payload = manifest.build(pkg)
    payload['revision'] = 'a' * 40
    signature = manifest.sign(payload, private_path)
    assert manifest.verify(payload, signature, public_path) is True


def test_verify_rejects_tampered_payload(pkg, keys):
    private_path, public_path = keys
    payload = manifest.build(pkg)
    payload['revision'] = 'a' * 40
    signature = manifest.sign(payload, private_path)
    payload['revision'] = 'b' * 40
    assert manifest.verify(payload, signature, public_path) is False


def test_verify_rejects_other_key(pkg, keys, tmp_path):
    """换一把公钥必须验不过。"""
    private_path, _ = keys
    _, other = _make_keys(tmp_path, 'other', 'cryptography' if manifest._has_cryptography() else 'pycryptodome')
    payload = manifest.build(pkg)
    payload['revision'] = 'a' * 40
    signature = manifest.sign(payload, private_path)
    assert manifest.verify(payload, signature, other) is False


def test_verify_tolerates_broken_signature(pkg, keys):
    _, public_path = keys
    payload = manifest.build(pkg)
    # 不是合法 base64，必须返回 False 而不是抛异常
    assert manifest.verify(payload, 'not*base64*', public_path) is False


@pytest.mark.parametrize('override,expected_fragment', [
    ({'schema': 2}, 'schema'),
    ({'sequence': 'x'}, 'sequence'),
    ({'revision': 'short'}, 'revision'),
    ({'filesSha256': 'x' * 10}, 'filesSha256'),
    ({'dependencies': ''}, 'dependencies'),
])
def test_validate_catches_bad_fields(override, expected_fragment):
    payload = {
        'schema': manifest.SCHEMA,
        'sequence': 1,
        'revision': 'a' * 40,
        'dependencies': 'c' * 64,
        'filesSha256': 'd' * 64,
    }
    payload.update(override)
    problems = manifest.validate(payload)
    assert any(expected_fragment in item for item in problems)


def test_validate_accepts_good_payload():
    payload = {
        'schema': manifest.SCHEMA,
        'sequence': 1,
        'revision': 'a' * 40,
        'dependencies': 'c' * 64,
        'filesSha256': 'd' * 64,
    }
    assert manifest.validate(payload) == []


def test_write_then_read_manifest(pkg, keys):
    private_path, _ = keys
    payload = manifest.build(pkg)
    payload['revision'] = 'a' * 40
    signature = manifest.sign(payload, private_path)
    manifest.write_manifest(pkg, payload, signature)

    document = manifest.read_manifest(pkg)
    assert document['release']['revision'] == 'a' * 40
    assert document['signature'] == signature
    # 单独的 .sig 文件也要在，方便不带 JSON 解析的校验工具
    assert (pkg / manifest.SIGNATURE_NAME).read_text(encoding='utf-8').strip() == signature


def test_manifest_itself_is_not_signed(pkg, keys):
    """release.json 不能被算进自己的清单，否则签名永远对不上。"""
    private_path, _ = keys
    payload = manifest.build(pkg)
    payload['revision'] = 'a' * 40
    signature = manifest.sign(payload, private_path)
    manifest.write_manifest(pkg, payload, signature)
    assert manifest.build(pkg)['filesSha256'] == payload['filesSha256']


# ── 双后端互操作 ───────────────────────────────────────────────────────────
# 打包机上有 cryptography，便携包侧只有 pycryptodome（pyproject 的直接依赖）。
# 两边必须能互相验签，否则包里签出来的东西用户那边根本验不了。

def _force_backend(monkeypatch, name):
    """把 manifest 的后端选择钉死到指定实现。

    Args:
        monkeypatch: pytest 的 monkeypatch fixture。
        name (str): 'cryptography' 或 'pycryptodome'。

    Returns:
        bool: 该后端在本机是否可用。
    """
    if name == 'cryptography':
        try:
            import cryptography  # noqa: F401
        except ImportError:
            return False
        monkeypatch.setattr(manifest, '_CRYPTOGRAPHY', True)
        return True
    try:
        from Crypto.PublicKey import ECC  # noqa: F401
    except ImportError:
        return False
    monkeypatch.setattr(manifest, '_CRYPTOGRAPHY', False)
    return True


def _payload():
    return {
        'schema': manifest.SCHEMA,
        'sequence': 1,
        'revision': 'a' * 40,
        'dependencies': 'c' * 64,
        'filesSha256': 'd' * 64,
    }


@pytest.mark.parametrize('backend', ['cryptography', 'pycryptodome'])
def test_backend_sign_verify_round_trip(monkeypatch, tmp_path, backend):
    """每个后端各自签、各自验，都要通过。"""
    if not _force_backend(monkeypatch, backend):
        pytest.skip(f'{backend} 在本机不可用')
    private_path, public_path = _make_keys(tmp_path, backend, backend)
    payload = _payload()
    signature = manifest.sign(payload, private_path)
    assert manifest.verify(payload, signature, public_path) is True


@pytest.mark.parametrize('signer,verifier', [
    ('cryptography', 'pycryptodome'),
    ('pycryptodome', 'cryptography'),
])
def test_backends_interoperate(monkeypatch, tmp_path, signer, verifier):
    """一个后端签出的签名，另一个后端必须能验。

    这条是整个方案能成立的前提：包里只有 pycryptodome，
    而包是打包机用 cryptography 签的。
    """
    if not _force_backend(monkeypatch, signer):
        pytest.skip(f'{signer} 在本机不可用')
    private_path, public_path = _make_keys(tmp_path, signer, signer)
    payload = _payload()
    signature = manifest.sign(payload, private_path)

    if not _force_backend(monkeypatch, verifier):
        pytest.skip(f'{verifier} 在本机不可用')
    assert manifest.verify(payload, signature, public_path) is True


@pytest.mark.parametrize('backend', ['cryptography', 'pycryptodome'])
def test_backend_rejects_tampering(monkeypatch, tmp_path, backend):
    """任一后端都不能放过被改过的清单。"""
    if not _force_backend(monkeypatch, backend):
        pytest.skip(f'{backend} 在本机不可用')
    private_path, public_path = _make_keys(tmp_path, backend, backend)
    payload = _payload()
    signature = manifest.sign(payload, private_path)
    payload['revision'] = 'b' * 40
    assert manifest.verify(payload, signature, public_path) is False


def test_has_cryptography_probe_is_cached():
    """后端探测只做一次，结果缓存在模块变量里。"""
    original = manifest._CRYPTOGRAPHY
    try:
        manifest._CRYPTOGRAPHY = None
        first = manifest._has_cryptography()
        assert manifest._CRYPTOGRAPHY is first
        # 再调一次不应重新探测，直接返回缓存
        assert manifest._has_cryptography() is first
    finally:
        manifest._CRYPTOGRAPHY = original


def test_read_manifest_returns_none_for_missing(pkg):
    assert manifest.read_manifest(pkg) is None


def test_read_manifest_returns_none_for_broken_json(pkg):
    (pkg / manifest.MANIFEST_NAME).write_text('{not json', encoding='utf-8')
    assert manifest.read_manifest(pkg) is None
