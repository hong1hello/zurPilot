<#
.SYNOPSIS
    把本仓库打成 Windows 免安装便携包（带完整 Python 运行时，解压即用）。

.DESCRIPTION
    AzurPilot 有 177 个依赖、解压后 1GB 左右，塞单文件 exe 不现实（生态里的 Windows 版
    也是"启动器 + 按需装"）。这里做的是自包含目录包：

        AzurPilot-Windows-x64/
        ├─ AzurPilot.bat            双击启动 WebUI 并打开浏览器
        ├─ runtime/                 Python 3.14 运行时 + 全部依赖（可重定位）
        ├─ module/ deploy/ config/  源码
        ├─ frontend/dist/           已构建的前端产物
        └─ gui.py alas.py ...

.PARAMETER OutDir
    输出目录，默认 <repo 的上一级>\build-win\out（放仓库外，避免 1GB 产物被 git 扫到）

.PARAMETER SkipRuntime
    跳过运行时组装（runtime 已经就位时用，省 10 分钟）

.PARAMETER NoZip
    只生成目录，不压 zip

.PARAMETER NoProbe
    跳过启动探活。默认会真起一次 WebUI，验证 /healthz 与前端能挂上；
    只有明明知道产物有问题、只想快速出个目录时才用。

.PARAMETER ProbeTimeout
    探活总等待秒数，默认 60。机器慢或者首次 import 大量依赖时往上调。

.PARAMETER SignKey
    Ed25519 私钥（PEM）路径。给了就对产物签名，写出 release.json。
    私钥用 scripts\release_manifest_cli.py keygen 生成，只放构建机上。

.PARAMETER SignPassword
    私钥口令。不加密的私钥不用传。

.PARAMETER Revision
    写进清单的 40 位 commit sha，默认取 git rev-parse HEAD。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\build_windows_portable.ps1

.EXAMPLE
    # 第二次迭代，运行时没变，只重打源码和前端
    powershell -ExecutionPolicy Bypass -File scripts\build_windows_portable.ps1 -SkipRuntime

.EXAMPLE
    # 正式发布：签名
    powershell -ExecutionPolicy Bypass -File scripts\build_windows_portable.ps1 -SkipRuntime -SignKey keys\ed25519-private.pem
#>
[CmdletBinding()]
param(
    [string]$OutDir = "",
    [switch]$SkipRuntime,
    [switch]$NoZip,
    [switch]$NoProbe,
    [int]$ProbeTimeout = 60,
    [string]$SignKey = "",
    [string]$SignPassword = "",
    [string]$Revision = ""
)

$ErrorActionPreference = "Stop"

# 仓库根 = 本脚本的上一级
$Repo = Split-Path -Parent $PSScriptRoot
# 输出放仓库外一层，避免 1GB 产物被 git / ruff / 打包脚本自己扫到
if (-not $OutDir) { $OutDir = Join-Path (Split-Path -Parent $Repo) "build-win\out" }
$Stage = Join-Path $OutDir "AzurPilot-Windows-x64"

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Copy-Tree([string]$From, [string]$To, [string[]]$ExcludeDirs = @(), [string[]]$ExcludeFiles = @()) {
    if (-not (Test-Path -LiteralPath $From)) {
        Write-Host "    skip (missing): $From" -ForegroundColor DarkGray
        return
    }
    $args = @($From, $To, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/R:0", "/W:0")
    if ($ExcludeDirs.Count) { $args += "/XD"; $args += $ExcludeDirs }
    if ($ExcludeFiles.Count) { $args += "/XF"; $args += $ExcludeFiles }
    & robocopy @args | Out-Null
    # robocopy 的退出码 0-7 都算成功，>=8 才是真失败
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE): $From -> $To" }
}

# ── 1. 运行时 ────────────────────────────────────────────────────────────────
if (-not $SkipRuntime) {
    Write-Step "组装 Python 运行时"

    # 优先用 uv 下载的可重定位 CPython（python-build-standalone）；
    # 退而求其次找系统里的 python.exe 同级目录。
    $Candidates = @()
    $UvRoot = Join-Path $env:APPDATA "uv\python"
    if (Test-Path -LiteralPath $UvRoot) {
        $Candidates += Get-ChildItem -LiteralPath $UvRoot -Directory |
            Where-Object { $_.Name -like "cpython-3.14*" -and (Test-Path (Join-Path $_.FullName "python.exe")) } |
            Sort-Object Name -Descending |
            ForEach-Object { $_.FullName }
    }
    $VenvCfg = Join-Path $Repo ".venv\pyvenv.cfg"
    if (Test-Path -LiteralPath $VenvCfg) {
        $Home = (Get-Content -LiteralPath $VenvCfg | Where-Object { $_ -match "^home\s*=" }) -replace "^home\s*=\s*", ""
        if ($Home -and (Test-Path (Join-Path $Home "python.exe"))) { $Candidates = @($Home) + $Candidates }
    }

    if (-not $Candidates.Count) {
        throw "找不到可重定位的 CPython 3.14。先跑 `uv python install 3.14` 或手动把 python.exe 目录放到 $env:APPDATA\uv\python 下。"
    }
    $PyRoot = $Candidates[0]
    Write-Host "    python: $PyRoot"

    $Runtime = Join-Path $Stage "runtime"
    if (Test-Path -LiteralPath $Runtime) { Remove-Item -LiteralPath $Runtime -Recurse -Force }
    Copy-Tree $PyRoot $Runtime

    $SiteSrc = Join-Path $Repo ".venv\Lib\site-packages"
    if (-not (Test-Path -LiteralPath $SiteSrc)) { throw "缺少 .venv\Lib\site-packages，先跑 uv sync" }

    # 1GB 的小文件用 robocopy 要跑十几分钟；tar 一次归档再解压，30 秒到 2 分钟
    $SiteDst = Join-Path $Runtime "Lib\site-packages"
    if ([System.IO.Directory]::Exists($SiteDst)) { [System.IO.Directory]::Delete($SiteDst, $true) }
    $TarPath = Join-Path $OutDir "site-packages.tar"
    Write-Host "    归档 site-packages（约 1GB）..."
    Push-Location (Join-Path $Repo ".venv\Lib")
    try {
        & tar.exe -cf $TarPath "site-packages"
        if ($LASTEXITCODE -ne 0) { throw "tar 归档失败 ($LASTEXITCODE)" }
        Write-Host "    解压到 runtime..."
        & tar.exe -xf $TarPath -C (Join-Path $Runtime "Lib")
        if ($LASTEXITCODE -ne 0) { throw "tar 解压失败 ($LASTEXITCODE)" }
    }
    finally {
        Pop-Location
        if (Test-Path -LiteralPath $TarPath) { Remove-Item -LiteralPath $TarPath -Force }
    }
    $Count = @(Get-ChildItem -LiteralPath $SiteDst -Directory -ErrorAction SilentlyContinue).Count
    Write-Host "    site-packages 包数: $Count"

    # pywin32 的 .pth 会 import pywin32_bootstrap；部分包不带这个模块，留着会在每次启动时刷错误
    $Pth = Join-Path $Runtime "Lib\site-packages\pywin32.pth"
    if (Test-Path -LiteralPath $Pth) {
        $bootstrap = Join-Path $Runtime "Lib\site-packages\pywin32_bootstrap.py"
        if (-not (Test-Path -LiteralPath $bootstrap)) {
            Remove-Item -LiteralPath $Pth -Force
            Write-Host "    已移除失效的 pywin32.pth" -ForegroundColor DarkYellow
        }
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $Stage "runtime\python.exe"))) {
    throw "runtime\python.exe 不存在；先不带 -SkipRuntime 跑一次。"
}

# ── 2. 源码 ─────────────────────────────────────────────────────────────────
Write-Step "复制源码"
$ExcludeDirs = @(
    (Join-Path $Repo ".venv"), (Join-Path $Repo ".git"), (Join-Path $Repo "build-win"),
    (Join-Path $Repo "node_modules"), (Join-Path $Repo "frontend\node_modules"),
    (Join-Path $Repo ".ruff_cache"), (Join-Path $Repo ".uv-cache"), (Join-Path $Repo "log"),
    (Join-Path $Repo "cache"), (Join-Path $Repo "screenshots"), (Join-Path $Repo "config\storage-backups")
)
Copy-Tree $Repo $Stage -ExcludeDirs $ExcludeDirs

# 前端已构建产物单独确认（build 产物被上面整体带走，这里只做存在性检查）
$Dist = Join-Path $Stage "frontend\dist\index.html"
if (-not (Test-Path -LiteralPath $Dist)) {
    Write-Host "    ! frontend/dist 缺失，WebUI 会 404。先跑：cd frontend; npm ci; npm run build" -ForegroundColor Yellow
}

# ── 3. 启动脚本 ─────────────────────────────────────────────────────────────
Write-Step "生成启动脚本"
$Bat = @'
@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PYTHON=%~dp0runtime\python.exe"
if not exist "%PYTHON%" (
    echo [ERROR] runtime\python.exe 不存在，请重新解压完整压缩包。
    pause
    exit /b 1
)

rem 运行时目录必须在 PATH 里：cv2 / onnxruntime 的 .pyd 会找同目录的 DLL
set "PATH=%~dp0runtime;%~dp0runtime\Library\bin;%PATH%"

echo 正在启动 AzurPilot WebUI...
echo 关闭此窗口即停止服务。
echo.
"%PYTHON%" gui.py %*
if errorlevel 1 (
    echo.
    echo [ERROR] 启动失败，退出码 %errorlevel%。日志见 log\ 目录。
    pause
)
endlocal
'@
Set-Content -LiteralPath (Join-Path $Stage "AzurPilot.bat") -Value $Bat -Encoding OEM

# 一个最小说明，免得用户不知道先点哪个
$Readme = @"
AzurPilot 便携版（Windows x64）
================================

1. 双击 AzurPilot.bat 启动服务
2. 启动后浏览器会自动打开 http://127.0.0.1:22267
   若没有自动打开，手动访问该地址
3. 关闭黑色窗口即停止服务

目录说明
  runtime\        Python 3.14 + 全部依赖（不要删）
  module\         自动化核心
  deploy\         部署与更新逻辑
  config\         实例配置（首次启动会自动生成模板）
  frontend\dist\  已构建的 WebUI

排错
  - 启动失败先看 log\ 目录里最新的日志
  - 报 DLL 缺失：把 runtime 目录加入系统 PATH 后重试
  - 端口被占：改 config\deploy.yaml 的 WebuiPort
"@
Set-Content -LiteralPath (Join-Path $Stage "README.txt") -Value $Readme -Encoding UTF8

# ── 4. 启动探活 ─────────────────────────────────────────────────────────────
# 产物不是"能起"的，用户双击只会看到一屏 traceback。这里真起一次 WebUI：
# 用内核分配的空闲端口，断言 /healthz 返回 200 且 / 正文里有 <div id="root">。
# 探活用便携包自己的 runtime 跑，所以测的就是用户拿到手的那套依赖。
if (-not $NoProbe) {
    Write-Step "启动探活"
    $ProbeScript = Join-Path $Stage "scripts\probe_portable.py"
    $StagePython = Join-Path $Stage "runtime\python.exe"
    if (-not (Test-Path -LiteralPath $ProbeScript)) {
        throw "缺少 $ProbeScript，无法探活"
    }
    Write-Host "    在空闲端口上真起一次 WebUI（最多等 ${ProbeTimeout}s）..."
    # 探活子进程同样要带 runtime 到 PATH，跟 AzurPilot.bat 保持一致
    $env:PATH = (Join-Path $Stage "runtime") + ";" + (Join-Path $Stage "runtime\Library\bin") + ";" + $env:PATH
    & $StagePython $ProbeScript --root $Stage --timeout $ProbeTimeout
    if ($LASTEXITCODE -ne 0) {
        # 失败时不直接 throw，把日志和标记留下，方便用户/CI 取证
        $Marker = Join-Path $Stage "PROBE-FAILED.txt"
        $LogFile = Join-Path $Stage "log\probe-stdout.log"
        $Detail = ""
        if (Test-Path -LiteralPath $LogFile) {
            $Detail = (Get-Content -LiteralPath $LogFile -Tail 40 -ErrorAction SilentlyContinue) -join "`r`n"
        }
        $MarkerText = "启动探活失败（退出码 $LASTEXITCODE）`r`n时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')`r`n`r`n--- log\probe-stdout.log 末尾 ---`r`n$Detail`r`n"
        Set-Content -LiteralPath $Marker -Value $MarkerText -Encoding UTF8
        throw "启动探活失败，产物不可用。详情见 $Marker"
    }
    Write-Host "    探活通过" -ForegroundColor Green
}

# ── 5. 打包 ─────────────────────────────────────────────────────────────────
Write-Step "汇总"
# 探活留下的临时文件不该进用户手里的包：日志是构建机现场的，summary 只给 CI 看。
$ProbeLog = Join-Path $Stage "log\probe-stdout.log"
if (Test-Path -LiteralPath $ProbeLog) { Remove-Item -LiteralPath $ProbeLog -Force }
$ProbeSummary = Join-Path $Stage "probe-summary.json"
if (Test-Path -LiteralPath $ProbeSummary) { Remove-Item -LiteralPath $ProbeSummary -Force }
$ProbeMarker = Join-Path $Stage "PROBE-FAILED.txt"
if (Test-Path -LiteralPath $ProbeMarker) { Remove-Item -LiteralPath $ProbeMarker -Force }

# 签名放在清理之后：清单必须描述最终形态的包，早签会把探活残留算进哈希。
if ($SignKey) {
    Write-Step "签名发布清单"
    $key = $SignKey
    if (-not [System.IO.Path]::IsPathRooted($key)) { $key = Join-Path $Repo $key }
    if (-not (Test-Path -LiteralPath $key)) { throw "找不到私钥: $key" }

    if (-not $Revision) {
        # 打包机上从仓库当前 HEAD 取，保证清单能对上源码
        Push-Location $Repo
        try {
            $Revision = (& git rev-parse HEAD 2>$null)
            if ($LASTEXITCODE -ne 0 -or -not $Revision) {
                Write-Host "    ! 取不到 git HEAD，revision 留全 0" -ForegroundColor Yellow
                $Revision = ""
            }
            else {
                $Revision = $Revision.Trim()
            }
        }
        finally { Pop-Location }
    }

    $SignScript = Join-Path $Repo "scripts\release_manifest_cli.py"
    # 用仓库自己的 .venv：打包机上有 cryptography，便携包里的 runtime 未必有
    $HostPython = Join-Path $Repo ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $HostPython)) { $HostPython = "python" }

    $SignArgs = @($SignScript, "sign", "--root", $Stage, "--key", $key)
    if ($SignPassword) { $SignArgs += @("--password", $SignPassword) }
    if ($Revision) { $SignArgs += @("--revision", $Revision) }

    & $HostPython @SignArgs
    if ($LASTEXITCODE -ne 0) { throw "签名失败（退出码 $LASTEXITCODE）" }
}

$Size = [math]::Round(((Get-ChildItem -LiteralPath $Stage -Recurse -File | Measure-Object -Property Length -Sum).Sum) / 1MB, 1)
Write-Host "    目录: $Stage"
Write-Host "    体积: ${Size}MB"

if (-not $NoZip) {
    $Zip = Join-Path $OutDir "AzurPilot-Windows-x64.zip"
    if (Test-Path -LiteralPath $Zip) { Remove-Item -LiteralPath $Zip -Force }
    Write-Host "    压缩中（1GB 大约 3-6 分钟）..."
    Compress-Archive -Path (Join-Path $Stage "*") -DestinationPath $Zip -CompressionLevel Fastest
    $ZipSize = [math]::Round((Get-Item $Zip).Length / 1MB, 1)
    Write-Host "    $Zip  (${ZipSize}MB)" -ForegroundColor Green
}

Write-Step "完成"
Write-Host "    实测启动：$Stage\AzurPilot.bat"
