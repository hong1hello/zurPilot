import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from deploy import config as deploy_config
from deploy import geo
from deploy import git as deploy_git
from deploy.Windows import config as windows_config
from deploy.Windows import git as windows_git


class TestIp9Location(unittest.TestCase):
    @patch('deploy.geo.requests.get')
    def test_returns_lowercase_country_code(self, get):
        response = get.return_value
        response.json.return_value = {'data': {'country_code': 'CN'}}

        self.assertEqual(geo.get_country_code(), 'cn')

        get.assert_called_once_with(
            geo.IP9_LOCATION_URL,
            timeout=5,
            headers={'User-Agent': 'AzurPilot'},
        )
        response.raise_for_status.assert_called_once_with()

    @patch('deploy.geo.requests.get')
    def test_invalid_response_returns_none(self, get):
        response = get.return_value
        response.json.return_value = {'data': {}}

        self.assertIsNone(geo.get_country_code())


class TestDeployLocation(unittest.TestCase):
    def make_config(self, config_module, repository=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        template = root / 'template.yaml'
        file = root / 'deploy.yaml'
        text = f'Repository: {repository or config_module.GITHUB_REPOSITORY}\nBranch: master\n'
        template.write_text(text)
        file.write_text(text)
        template_patch = (patch.object(config_module, 'get_deploy_template', return_value=str(template))
                          if config_module is deploy_config
                          else patch.object(config_module, 'DEPLOY_TEMPLATE', str(template)))
        with template_patch, patch.object(config_module.DeployConfig, 'show_config'):
            return config_module.DeployConfig(str(file))

    def test_official_repository_is_redirected_to_fork(self):
        for config_module in (deploy_config, windows_config):
            with self.subTest(config_module=config_module.__name__):
                config = self.make_config(config_module)

                # 官方地址不再按网络位置分流，一律归一；设置页读的是 config，必须一起改，
                # 否则界面会继续显示官方地址。
                self.assertEqual(config.Repository, config_module.FORK_REPOSITORY)
                self.assertEqual(config.config['Repository'], config_module.FORK_REPOSITORY)
                self.assertEqual(config.config['Repository'], config_module.GIT_OVER_CDN_REPOSITORY)
                self.assertFalse(config.GitOverCdn)
                self.assertIn(config.Repository, config_module.GIT_OVER_CDN_FALLBACK_REPOSITORIES)

    def test_legacy_sources_are_redirected_to_fork(self):
        """旧 pull 别名、官方镜像与官方地址都要归一，并写进 config 供设置页显示。"""
        legacy_sources = (
            'git://git.pull/AzurPilot',
            'https://cnb.cool/AzurPilot/AzurPilot',
            'https://gitcode.com/ddl2/AzurPilot',
            'cn',
            'cnb',
            'gitcode',
            'global',
            deploy_config.GITHUB_REPOSITORY,
        )
        for config_module in (deploy_config, windows_config):
            for source in legacy_sources:
                with self.subTest(config_module=config_module.__name__, source=source):
                    config = self.make_config(config_module, source)
                    config.config_redirect()

                    self.assertEqual(config.Repository, config_module.FORK_REPOSITORY)
                    self.assertEqual(config.config['Repository'], config_module.FORK_REPOSITORY)

    def test_fallback_repositories_no_longer_point_at_official_mirrors(self):
        """镜像通道停用：CNB / GitCode 指向官方上游，会把带云端上报的代码拉回来。"""
        for config_module in (deploy_config, windows_config):
            with self.subTest(config_module=config_module.__name__):
                mirrors = config_module.GIT_OVER_CDN_FALLBACK_REPOSITORIES
                self.assertTrue(mirrors)
                for item in mirrors:
                    self.assertEqual(item, config_module.FORK_REPOSITORY)
                self.assertNotIn('https://cnb.cool/AzurPilot/AzurPilot', mirrors)
                self.assertNotIn('https://gitcode.com/ddl2/AzurPilot', mirrors)

    def test_cnb_alias_and_repository(self):
        for config_module in (deploy_config, windows_config):
            with self.subTest(config_module=config_module.__name__):
                config = self.make_config(config_module, 'cnb')
                config.config_redirect()
                self.assertEqual(config.Repository, config_module.GIT_OVER_CDN_FALLBACK_REPOSITORIES[0])

    def test_gitcode_alias_and_repository(self):
        for config_module in (deploy_config, windows_config):
            with self.subTest(config_module=config_module.__name__):
                config = self.make_config(config_module, 'gitcode')
                config.config_redirect()
                self.assertEqual(config.Repository, config_module.GIT_OVER_CDN_FALLBACK_REPOSITORIES[1])

    def test_redirect_is_idempotent(self):
        for config_module in (deploy_config, windows_config):
            with self.subTest(config_module=config_module.__name__):
                config = self.make_config(config_module)

                config.config_redirect()
                config.config_redirect()

                self.assertEqual(config.Repository, config_module.FORK_REPOSITORY)
                self.assertFalse(config.GitOverCdn)

    def test_custom_repository_is_kept(self):
        """用户自己填的第三方地址不改写，只归一已知的官方源。"""
        for config_module in (deploy_config, windows_config):
            with self.subTest(config_module=config_module.__name__):
                config = self.make_config(config_module, 'https://github.com/example/custom')

                config.config_redirect()

                self.assertEqual(config.Repository, 'https://github.com/example/custom')

    def test_peer_fallback_repository(self):
        for git_mod in (deploy_git, windows_git):
            with self.subTest(git_module=git_mod.__name__):
                # 镜像通道停用：官方 CNB / GitCode 已不在回退列表里，没有可切换的 peer，
                # 更新失败时不会再被悄悄指回官方镜像。
                # The mirror channel is disabled, so the official mirrors have no peer.
                self.assertIsNone(
                    git_mod.peer_fallback_repository('https://cnb.cool/AzurPilot/AzurPilot'),
                )
                self.assertIsNone(
                    git_mod.peer_fallback_repository('https://gitcode.com/ddl2/AzurPilot'),
                )
                # 列表里只剩本 fork：它是自己的 peer，函数仍按环形规则返回自身。
                self.assertEqual(
                    git_mod.peer_fallback_repository('https://github.com/hong1hello/zurPilot'),
                    'https://github.com/hong1hello/zurPilot',
                )
                self.assertIsNone(git_mod.peer_fallback_repository('https://github.com/wess09/AzurPilot'))
                self.assertIsNone(git_mod.peer_fallback_repository(''))


if __name__ == '__main__':
    unittest.main()
