# Windows 便携包：探活与签名

打包脚本：`scripts/build_windows_portable.ps1`
配套工具：`scripts/probe_portable.py`、`scripts/release_manifest.py`、`scripts/release_manifest_cli.py`

## 一、这三块补的是什么

原来的脚本只负责「把文件摆好、压个 zip」。产物是不是真的能启动、传输过程中有没有被换过，
全靠人工双击试。现在补上两道闸：

| 环节 | 作用 | 失败时的表现 |
|---|---|---|
| 启动探活 | 真起一次 WebUI，验证 `/healthz` 与前端能挂上 | 打包中断，产物目录留 `PROBE-FAILED.txt` |
| 发布清单签名 | 每个文件算 SHA-256，整体用 Ed25519 签一次 | 校验时报出是签名不对还是内容被改 |

## 二、日常打包

```powershell
# 完整打包（含探活，约 10~15 分钟）
powershell -ExecutionPolicy Bypass -File scripts\build_windows_portable.ps1

# runtime 没变，只重打源码和前端
powershell -ExecutionPolicy Bypass -File scripts\build_windows_portable.ps1 -SkipRuntime

# 机器慢，探活给多点时间
powershell -ExecutionPolicy Bypass -File scripts\build_windows_portable.ps1 -SkipRuntime -ProbeTimeout 120
```

探活默认开。它会用 `runtime\python.exe` 在**内核分配的空闲端口**上起一次 `gui.py --host 127.0.0.1`，
然后双断言：

- `GET /healthz` 返回 200
- `GET /` 返回 200，且正文前 16 KiB 里有 `<div id="root">`

第二条比第一条重要得多：`/healthz` 只能证明 ASGI 应用起来了，证明不了 `frontend/dist` 挂上了。
很多「能启动但页面白屏」的包就是卡在这一步。

确认失败时，产物目录里会有 `PROBE-FAILED.txt`，内容是子进程 stdout 的最后 40 行。

## 三、正式发布：签名

### 3.1 一次性准备（只做一次）

```powershell
# 生成密钥对，私钥自己保管，公钥提交进仓库
.venv\Scripts\python.exe scripts\release_manifest_cli.py keygen --out keys
```

输出：

```
私钥: keys\ed25519-private.pem  （不要提交，不要分发）
公钥: keys\ed25519-public.pem  （提交进仓库，随包分发）
公钥 SHA-256: <64 位十六进制>
```

`.gitignore` 里要排除 `keys/ed25519-private.pem`。私钥丢了就只能重新生成一对，
届时旧包的签名全部作废。

### 3.2 打包并签名

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows_portable.ps1 `
    -SkipRuntime `
    -SignKey keys\ed25519-private.pem
```

`-Revision` 不传时自动取 `git rev-parse HEAD`。私钥带口令的加 `-SignPassword`。

签名会在**清理探活残留之后**执行，保证清单描述的是最终形态的包。

## 四、用户侧校验

```powershell
# 用仓库里的 .venv 跑（需要 cryptography）
.venv\Scripts\python.exe scripts\release_manifest_cli.py verify `
    --root D:\AzurPilot-Windows-x64 `
    --public keys\ed25519-public.pem
```

通过时输出：

```
签名校验通过（公钥 ...）
内容校验通过：12345 个文件，755000000 字节
依赖指纹一致
```

公钥查找顺序：`--public` → 包内 `keys/ed25519-public.pem` → 环境变量 `AZURPILOT_PUBKEY` → 脚本同目录 `keys/`。

三种失败会分别报出来：

| 输出 | 含义 |
|---|---|
| `签名校验失败` | 清单被改过，或不是用这把私钥签的 |
| `汇总哈希不一致` | 清单没问题，但磁盘上的文件被改过 |
| `依赖指纹不一致` | `uv.lock` 或 `pyproject.toml` 与清单不符 |

## 五、清单内容与签名的边界

`release.json`：

```json
{
  "release": {
    "schema": 1,
    "sequence": 1,
    "revision": "<40 位 commit sha>",
    "dependencies": "<64 位：sha256(uv.lock + \0 + pyproject.toml + \0)>",
    "fileCount": 12345,
    "totalBytes": 755000000,
    "filesSha256": "<64 位：全部文件 (path, sha256, size) 的规范 JSON 链式哈希>"
  },
  "signature": "<base64>"
}
```

签名对象是 `release` 字段的**规范序列化字节**：

```python
json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
```

所以 `signature` 字段本身可以自由追加，不影响验签，也不会因为换行或键序失效。
改这三条中任何一条，等于换协议，签名端和校验端必须同改。

**不参与签名的目录**（`release_manifest.EXCLUDE_DIRS`）：

```
runtime  log  cache  screenshots  node_modules  .git  __pycache__  .ruff_cache  .uv-cache  config
```

理由各不相同：

- `runtime` 由构建机同一个 Python 版本生成，签它只会让包大一倍、校验慢几倍
- `log` / `cache` / `screenshots` 每次启动都在变
- `config` 是用户数据，用户改配置不该让整包校验失败

**不参与签名的文件**：`release.json`、`release.sig`、`probe-summary.json`、`README.txt`、`AzurPilot.bat`、`PROBE-FAILED.txt`。

前两个是显然的（清单不能把自己算进去）。后四个是每次打包都会重新生成的，
签进去会导致同一份源码两次打包算出不同哈希。

## 六、为什么用 `dependency_id` 而不是逐包哈希

`dependency_id = sha256(uv.lock + \0 + pyproject.toml + \0)`

语义是「运行时装的是不是这一版依赖描述对应的东西」。逐包算哈希能更精确，但有两个问题：
`site-packages` 里混着 `.pth`、`__pycache__`、版本号目录，不同机器上会有微小差异；
而且 177 个包、1GB 的文件逐个算一遍要几分钟。

用锁文件指纹，配合 `runtime` 不参与签名，这个组合的精度足够——
真出问题（比如装错了 Python 版本），`fetch` 阶段的 `pyvenv.cfg` 就会暴露。

## 七、退出码约定

`probe_portable.py`：

| 退出码 | 含义 |
|---|---|
| 0 | 通过 |
| 1 | 前置检查失败 / 子进程崩溃 / 断言不通过 |
| 2 | 超时 |

`release_manifest_cli.py`：0 成功，1 失败。

## 八、单独跑探活

探活脚本可以脱离打包流程单独用，比如在用户的机器上排查：

```powershell
# 在解压目录里跑
runtime\python.exe scripts\probe_portable.py --root . --timeout 90
```

它会打印启动命令、每一步断言结果，失败时把子进程输出打出来。
`--root` 默认当前目录。

## 九、常见问题

**探活超时，但手动双击能启动**
首次 import 大量依赖（cv2、onnxruntime、scipy）在机械硬盘上可能要 30 秒以上。
把 `-ProbeTimeout` 调到 120。

**探活报「子进程提前退出」**
看 `PROBE-FAILED.txt` 里的 stderr。最常见的两个原因：
`runtime` 不完整（`ModuleNotFoundError`）、`frontend/dist` 没构建（`503`）。

**签名报 `revision 必须是 40 位十六进制`**
`-Revision` 传错了位数。不传让它自动取 `git rev-parse HEAD`。

**校验报「依赖指纹不一致」**
`uv.lock` 或 `pyproject.toml` 在签名之后被改过。重新打包并重新签名。
