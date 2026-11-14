import sys
from typing import Optional, Union

from deploy.config_transaction import DeployConfigTransaction
from deploy.logger import logger
from deploy.utils import *


# 本 fork：关闭云端上传与遥测的本地化版本，也是唯一允许的更新源。
# This fork: local-only build with cloud upload and telemetry pinned off, and the
# only update source this build accepts.
FORK_REPOSITORY = 'https://github.com/hong1hello/zurPilot'
GIT_OVER_CDN_REPOSITORY = FORK_REPOSITORY
GIT_OVER_CDN_FALLBACK_REPOSITORIES = [FORK_REPOSITORY, FORK_REPOSITORY]
GIT_OVER_CDN_FALLBACK_REPOSITORY = FORK_REPOSITORY


def choose_fallback_repository() -> str:
    """镜像通道已停用，任何回退都指向本 fork。 / Mirrors are disabled; always this fork."""
    return FORK_REPOSITORY


# 上游官方仓库。只作为「是否为官方源」的判据使用（读到就替换成本 fork），
# 不再作为默认更新源；现有测试仍以它作为官方源基准。
# Upstream official repository. Kept only as the mirror-redirect criterion.
GITHUB_REPOSITORY = 'https://github.com/wess09/AzurPilot'
# 隐私分支名，仅作记录；默认更新分支是 master。
# The privacy branch name, kept for reference; the default branch is master.
PRIVACY_BRANCH = 'privacy/local-only'
# 默认跟踪的分支。
# Default tracked branch.
DEFAULT_BRANCH = 'master'
# 旧配置里可能出现的更新源写法：官方地址、官方国内镜像、历史上的 pull 别名。
# 它们都会把带云端上报与遥测的上游代码拉回来，读到就统一归一到本 fork。
# Legacy update sources — the official repo, its CN mirrors and the old pull alias.
# They would pull upstream code that carries cloud upload, so they are normalised.
LEGACY_REPOSITORIES = (
    'global',
    'cn',
    'cnb',
    'gitcode',
    'git://git.pull/AzurPilot',
    'https://cnb.cool/AzurPilot/AzurPilot',
    'https://gitcode.com/ddl2/AzurPilot',
    GITHUB_REPOSITORY,
)


class ErrorCodes:
    """部署错误码：同时作为进程退出码返回，启动器按码分类展示。

    码段与 gui.py 的 EXIT_*（70-76）互不重叠；新增码值必须同步
    启动器（alas-launcher）的错误码映射表。
    """

    EXEC_UNKNOWN = 1
    COMMAND_FAILED = 10      # execute() 通用命令执行失败
    GIT_FETCH_FAILED = 11    # git fetch 主源与全部镜像重试穷尽
    GIT_MERGE_FAILED = 12    # git pull/merge 合并失败
    DEPS_SYNC_FAILED = 13    # 依赖同步（uv sync / pip）失败


class ExecutionError(Exception):
    """部署流程执行失败；code 取 ErrorCodes，供上游结构化识别。"""

    def __init__(self, code: int = ErrorCodes.EXEC_UNKNOWN, message: str = ''):
        super().__init__(message or f'deploy failed with error code {code}')
        self.code = code


class ConfigModel:
    """部署配置模型，定义所有配置项及其默认值。"""
    # Git 配置
    Repository: str = FORK_REPOSITORY
    Branch: str = DEFAULT_BRANCH
    GitExecutable: str = "./.venv/Scripts/git/cmd/git.exe" if sys.platform == "win32" else "./.venv/bin/git"
    GitProxy: Optional[str] = None
    SSLVerify: bool = False
    # 茗交所行动力上传开关；关闭后只保留本地历史，不再上报。
    StockUploadEnabled: bool = True

    # Python 配置
    PythonExecutable: str = "./.venv/Scripts/python.exe" if sys.platform == "win32" else "./.venv/bin/python"
    PypiMirror: Optional[str] = None
    InstallDependencies: bool = True

    # ADB 配置
    AdbExecutable: str = "./.venv/Scripts/adb.exe" if sys.platform == "win32" else "./.venv/bin/adb"
    ReplaceAdb: bool = True
    AutoConnect: bool = True
    InstallUiautomator2: bool = True

    # OCR 配置
    UseOcrServer: bool = False
    StartOcrServer: bool = False
    OcrServerPort: int = 22268
    OcrClientAddress: str = "127.0.0.1:22268"

    # 更新配置
    EnableReload: bool = True
    CheckUpdateInterval: int = 5
    AutoRestartTime: str = "03:50"

    # 杂项
    DiscordRichPresence: bool = False

    # 远程访问
    EnableRemoteAccess: bool = False
    RemoteAccessMode: str = "auto"
    CloudflareRegistry: str = "https://remote.azurpilot.com"
    CloudflareDeviceId: str = ""
    SSHUser: Optional[str] = None
    SSHServer: Optional[str] = None
    SSHExecutable: Optional[str] = None
    AllowedRedirectHosts: Optional[str] = None
    MaxRedirects: int = 2
    SignalingServer: Optional[str] = None
    StunServers: Optional[str] = '["stun:stun.l.google.com:19302"]'
    TurnServers: Optional[str] = None
    TurnCredentialMode: str = "static"

    # WebUI 配置
    WebuiHost: str = "0.0.0.0"
    WebuiPort: int = 25548
    WebuiSSLKey: Optional[str] = None
    WebuiSSLCert: Optional[str] = None
    Language: str = "en-US"
    Theme: str = "default"
    DpiScaling: bool = True
    Password: Optional[str] = None
    CDN: Union[str, bool] = False
    # --watermark. 关闭未经验证版本的水印。默认 False，即默认显示水印，
    # 用于提醒当前运行的是未经验证的构建（见 module/webui/app_shell.py）。
    DisableBranchWatermark: bool = False
    Run: Optional[str] = None

    # 动态配置
    GitOverCdn: bool = False


class DeployConfig(DeployConfigTransaction, ConfigModel):
    """部署配置管理器，负责配置的读取、保存、重定向与执行环境管理。"""

    def __init__(self, file=DEPLOY_CONFIG):
        """初始化部署配置。

        Args:
            file (str): 用户部署配置文件路径。
        """
        self.file = file
        self.template_file = get_deploy_template()
        self.config = {}
        self.config_template = {}
        self.read()

        self.show_config()

    def show_config(self):
        """打印与默认模板不同的配置项。"""
        logger.hr("Show deploy config", 1)
        for k, v in self.config.items():
            if k in ("Password", "SSHUser"):
                continue
            if self.config_template.get(k) == v:
                continue
            logger.info(f"{k}: {v}")

        logger.info(f"Rest of the configs are the same as default")

    def config_redirect(self):
        """部署配置重定向，处理旧配置到新配置的迁移。

        每次 `read()` 之后必须调用。
        """
        self.config.pop('AutoUpdate', None)
        # 旧配置里的官方地址 / 官方镜像 / 旧 pull 别名一律归一到本 fork，并同步内存里的
        # config 副本：系统设置页读的是 config，只改属性会让界面继续显示官方地址。
        # 这里不落盘（没走 __setattr__ 的写盘路径），用户保存设置时才写回文件。
        # Legacy official sources are normalised to this fork and mirrored into the
        # in-memory config because the settings page renders `config`, not the
        # attribute. Nothing is written to deploy.yaml until the user saves.
        if self.Repository in LEGACY_REPOSITORIES:
            logger.info(f'更新源 {self.Repository} 已归一为 {FORK_REPOSITORY}')
            object.__setattr__(self, 'Repository', FORK_REPOSITORY)
            self.config['Repository'] = FORK_REPOSITORY
        if self.Repository in [
            'https://gitee.com/LmeSzinc/AzurLaneAutoScript',
            'https://gitee.com/lmeszinc/azur-lane-auto-script-mirror',
            'https://e.coding.net/llop18870/alas/AzurLaneAutoScript.git',
            'https://e.coding.net/saarcenter/alas/AzurLaneAutoScript.git',
            'https://git.saarcenter.com/LmeSzinc/AzurLaneAutoScript.git',
            'git://git.lyoko.io/AzurLaneAutoScript',
            'https://gitcode.com/ZhangMusan/AzurLaneAutoScript',
            'https://gitcode.com/nerom/AzurLaneAutoScript',
            'https://gitee.com/wqeaxc/AzurLaneAutoScript1',
            'https://git.nanoda.work/git/AzurLaneAutoScript',
            'https://git.nanoda.work/git/AzurPilot',
            'https://git.nanoda.work',
        ]:
            object.__setattr__(self, 'Repository', FORK_REPOSITORY)
            self.config['Repository'] = FORK_REPOSITORY
        if self.PypiMirror in [
            'https://pypi.tuna.tsinghua.edu.cn/simple'
        ]:
            object.__setattr__(self, 'PypiMirror', 'https://mirrors.aliyun.com/pypi/simple')
            self.config['PypiMirror'] = 'https://mirrors.aliyun.com/pypi/simple'

        # 绕过 webui.config.DeployConfig.__setattr__()，不写入 deploy.yaml。
        # 镜像通道已停用：更新一律走普通 git，避免被判定成 CDN 客户端。
        # Bypasses DeployConfig.__setattr__ so this is not written to deploy.yaml.
        # The mirror channel is disabled, so updates always use plain git.
        super().__setattr__('GitOverCdn', False)

    def filepath(self, key):
        """根据配置键获取绝对文件路径。

        Args:
            key (str): 配置键名。

        Returns:
            str: 绝对文件路径。
        """
        path = self.config[key]
        # 启动器可写入带双引号的 YAML 路径，引号不是文件名的一部分。
        if len(path) >= 2 and path[0] == path[-1] == '"':
            path = path[1:-1]
        return (
            os.path.abspath(os.path.join(self.root_filepath, path))
            .replace(r"\\", "/")
            .replace("\\", "/")
            .replace('"', '"')
        )

    @cached_property
    def root_filepath(self):
        """获取项目根目录绝对路径。

        Returns:
            str: 格式化为斜杠分隔的根目录绝对路径。
        """
        return (
            os.path.abspath(os.path.join(os.path.dirname(__file__), "../"))
            .replace(r"\\", "/")
            .replace("\\", "/")
            .replace('"', '"')
        )

    def execute(self, command, allow_failure=False, output=True):
        """执行系统命令。

        Args:
            command (str): 要执行的命令。
            allow_failure (bool): 是否允许失败。
            output (bool): 是否显示输出。

        Returns:
            bool: 是否成功。失败且不允许失败时终止安装流程。
        """
        command = command.replace(r"\\", "/").replace("\\", "/").replace('"', '"')
        if not output:
            command = command + ' >nul 2>nul'
        logger.info(command)
        # Windows cmd /c 会剥掉首尾引号；额外包一层，保留程序路径和参数的引号。
        shell_command = f'"{command}"' if os.name == 'nt' and command.startswith('"') else command
        error_code = os.system(shell_command)
        if error_code:
            if allow_failure:
                logger.info(f"[ allowed failure ], error_code: {error_code}")
                return False
            else:
                logger.info(f"[ failure ], error_code: {error_code}")
                self.show_error(command)
                raise ExecutionError(
                    ErrorCodes.COMMAND_FAILED,
                    f'Command failed (exit {error_code}): {command}',
                )
        else:
            logger.info(f"[ success ]")
            return True

    def show_error(self, command=None):
        """展示更新失败信息及排查指引。

        Args:
            command (str, optional): 导致失败的命令。
        """
        logger.hr("Update failed", 0)
        self.show_config()
        logger.info("")
        logger.info(f"Last command: {command}")
        logger.info(
            "Please check your deploy settings in config/deploy.yaml "
            "and re-open AzurPilot.exe"
        )
        logger.info("Take the screenshot of entire window if you need help")
