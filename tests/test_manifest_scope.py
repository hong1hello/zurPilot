"""签名范围与更新范围必须一致。

这是事务更新的核心不变量：
- `release_manifest.iter_files()` 决定哪些文件进签名清单
- `deploy.independent.app_path()` 决定哪些文件允许被更新替换

两者一旦错位，就会出问题：
- 签名多出来的文件：被篡改也校验不出来（因为更新不会碰它，也没人比对）
- 更新多出来的文件：清单里没有它，apply 时哈希比对会把它当成"本机修改"而中止

所以这里直接断言「签名里的每个文件都必须是可更新的」。
反向不要求（模板文件可更新但不签名，它由代码生成）。
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'scripts'))
sys.path.insert(0, str(REPO))

import release_manifest as manifest
from deploy import independent as ind


def test_every_signed_file_is_updatable():
    """签名清单里的每个文件，都必须能通过 app_path 白名单。"""
    offenders = []
    root = REPO
    for path in manifest.iter_files(root):
        name = path.relative_to(root).as_posix()
        if not (ind.app_path(name) or ind.app_path(name, frontend=True)):
            offenders.append(name)

    assert not offenders, (
        f'以下文件会进签名但不在可更新白名单里，共 {len(offenders)} 个：\n  '
        + '\n  '.join(offenders[:20])
    )


@pytest.mark.parametrize('directory', [
    'tests', 'docs', 'dev_tools', '.github', '.azurpilot',
    'config-after-failed-update', 'AzurPilot_Data_Backup',
])
def test_dev_and_runtime_dirs_are_excluded(directory):
    """开发目录与运行痕迹不能被签名，否则清单随开发机状态漂移。"""
    assert directory in manifest.EXCLUDE_DIRS


@pytest.mark.parametrize('name', [
    '.gitignore', '.gitattributes', '.dockerignore', 'AGENTS.md',
])
def test_repo_meta_files_are_not_signed(name):
    """仓库元文件不随包分发，也不该进签名。

    它们要么在 EXCLUDE_NAMES 里，要么因为不在白名单而被跳过；
    这里断言最终结果：确实不出现。
    """
    assert ind.app_path(name) is False


def test_scripts_is_updatable():
    """scripts 必须可更新：release_manifest_cli.py 是用户端验签入口。"""
    assert ind.app_path('scripts/release_manifest_cli.py') is True
    assert ind.app_path('scripts/probe_portable.py') is True


def test_config_templates_updatable_but_not_signed():
    """模板可更新但不签名——它是唯一被允许的不对称。

    原因：config 整个目录排除在签名外（用户配置不该被校验），
    但模板由代码生成，随版本更新是必要的。
    """
    assert ind.app_path('config/template.json') is True
    assert 'config' in manifest.EXCLUDE_DIRS


def test_signable_dirs_match_updatable_dirs():
    """两边的目录白名单必须逐字相同。

    签名侧（release_manifest.SIGNABLE_DIRS）与更新侧（independent.APP_DIRS）
    是同一件事的两个实现，故意解耦是为了让签名工具不依赖 deploy 包。
    代价是可能改一边忘另一边，所以这里直接比对。
    """
    assert tuple(manifest.SIGNABLE_DIRS) == tuple(ind.APP_DIRS)


def test_signable_root_files_match_updatable_files():
    """根文件白名单同理。"""
    assert tuple(manifest.SIGNABLE_ROOT_FILES) == tuple(ind.APP_FILES)


def test_is_signable_matches_app_path_for_sample_paths():
    """两条规则在常见路径上判定一致。"""
    samples = [
        'gui.py', 'alas.py', 'uv.lock', 'LICENSE',
        'module/api/app.py', 'deploy/independent.py', 'scripts/release_manifest.py',
        'assets/cn/ui/shop.png', 'licenses/GPL-3.0.txt',
        'frontend/dist/index.html', 'frontend/dist/assets/a.js',
        'frontend/src/App.tsx', 'frontend/e2e/fleet.spec.ts', 'frontend/package.json',
        'config/template.json', 'runtime/python.exe', 'tests/test_x.py',
        'README.md', 'AGENTS.md', '.gitignore',
    ]
    mismatched = []
    for name in samples:
        signed = manifest.is_signable(name)
        updatable = bool(ind.app_path(name) or ind.app_path(name, frontend=True))
        # config 模板是唯一被允许的不对称：可更新但不签名
        if name == 'config/template.json':
            continue
        if signed != updatable:
            mismatched.append((name, signed, updatable))
    assert not mismatched, f'签名与更新判定不一致: {mismatched}'


def test_runtime_never_signed_never_updated():
    """runtime 既不签名也不更新：它由打包机生成，靠依赖指纹守护。"""
    assert 'runtime' in manifest.EXCLUDE_DIRS
    assert ind.app_path('runtime/python.exe') is False
    assert ind.app_path('runtime/git/cmd/git.exe') is False
