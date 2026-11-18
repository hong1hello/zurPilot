"""发布清单签名与校验的回归测试。

重点不是"签名能不能算出来"，而是四条边界：
1. 规范化必须稳定（键序、空白、非 ASCII 都不影响）
2. 篡改任一字节必须被检出
3. 换公钥必须被拒
4. 探活/日志目录必须被排除在签名范围外

后两条最容易在重构时悄悄失效，所以显式钉住。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

import release_manifest as manifest


cryptography = pytest.importorskip('cryptography')


@pytest.fixture()
def keys(tmp_path):
    """生成一对临时 Ed25519 密钥。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    private_path = tmp_path / 'private.pem'
    public_path = tmp_path / 'public.pem'
    private_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    public_path.write_bytes(key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    return private_path, public_path


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
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_path, _ = keys
    other = tmp_path / 'other-public.pem'
    other.write_bytes(Ed25519PrivateKey.generate().public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
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


def test_read_manifest_returns_none_for_missing(pkg):
    assert manifest.read_manifest(pkg) is None


def test_read_manifest_returns_none_for_broken_json(pkg):
    (pkg / manifest.MANIFEST_NAME).write_text('{not json', encoding='utf-8')
    assert manifest.read_manifest(pkg) is None
