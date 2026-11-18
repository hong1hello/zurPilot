"""远程源地址归一的回归测试。

这个函数决定「要不要改写 .git/config 的 remote」。

判据错了会出两种问题，方向相反：
- 太宽松：上游地址没被纠正，更新器继续对着旧仓库算（用户看到项目地址回退）
- 太严格：把开发机的 SSH remote 改写成 HTTPS，推送立刻要凭据

所以两侧都要钉住。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deploy.git import GitManager


ident = GitManager._repository_identity

FORK_HTTPS = 'https://github.com/hong1hello/zurPilot'


@pytest.mark.parametrize('url', [
    'git@github.com:hong1hello/zurPilot.git',
    'https://github.com/hong1hello/zurPilot',
    'https://github.com/hong1hello/zurPilot.git',
    'https://github.com/hong1hello/zurPilot/',
    'ssh://git@github.com/hong1hello/zurPilot.git',
])
def test_same_repository_in_different_forms(url):
    """同一仓库的各种写法必须归一成同一个标识。

    重点是 SSH 与 HTTPS 等价：开发机用 SSH 推送，配置里是 HTTPS 拉取，
    两者共存才对。
    """
    assert ident(url) == ident(FORK_HTTPS) == 'github.com/hong1hello/zurPilot'


@pytest.mark.parametrize('url', [
    'https://github.com/wess09/AzurPilot',
    'git@github.com:wess09/AzurPilot.git',
    'https://gitcode.com/ddl2/AzurPilot',
    'https://cnb.cool/AzurPilot/AzurPilot',
])
def test_different_repository_is_detected(url):
    """上游与其他镜像必须被判成不同仓库，才会触发纠正。"""
    assert ident(url) != ident(FORK_HTTPS)


def test_case_insensitive_host():
    """host 大小写不影响判等（path 保持原样，GitHub 的 path 区分大小写）。"""
    assert ident('https://GitHub.com/hong1hello/zurPilot') == ident(FORK_HTTPS)


def test_empty_url_is_safe():
    """空值不能让调用方误判成"需要改写"。"""
    assert ident('') == ''
    assert ident(None) == ''


def test_git_suffix_is_stripped_once():
    """只去掉结尾的 .git，不能把路径中间的 .git 也吃掉。"""
    assert ident('https://github.com/a/.gitrepo.git') == 'github.com/a/.gitrepo'
