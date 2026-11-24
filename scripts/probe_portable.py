"""便携包启动探活：真正起一次 WebUI，确认后端能 import、前端能挂上。

打包脚本的产物如果不是"能起"的，用户双击后只会看到一堆 traceback。这个脚本把
第三方独立包里的 probe 思路拿过来：在临时端口上起一个真实子进程，双断言
`GET /healthz` 返回 200 且 `GET /` 正文里出现 `<div id="root">`，任何一条不满足
就报失败并带上子进程的 stderr 尾巴。

用法（在待测目录里跑，不要用仓库自己的 .venv）::

    <stage>\\runtime\\python.exe scripts\\probe_portable.py --root <stage>
    <stage>\\runtime\\python.exe scripts\\probe_portable.py --root <stage> --timeout 60

退出码：0 通过，1 断言失败，2 超时。
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# 探活用这两个端点：/healthz 证明 ASGI 应用起来了，/ 证明 frontend/dist 真的挂上了。
HEALTH_PATH = '/healthz'
INDEX_PATH = '/'
ROOT_MARKER = b'<div id="root">'
# 只看正文前 16KiB，index.html 的 root 节点一定在最前面，读多了浪费。
MARKER_WINDOW = 16 * 1024
# 单次 HTTP 请求超时。子进程刚起来时连接会被拒，靠外层轮询重试。
REQUEST_TIMEOUT = 1.5
POLL_INTERVAL = 0.2


def free_port():
    """向系统要一个空闲端口，拿到就释放。

    直接写死端口会和用户机器上正在跑的实例撞车；bind 到 0 让内核挑，关掉后
    在极短时间内被别人抢走的概率可以忽略。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


PROBE_CONFIG = '.probe-config'


def probe_env(root):
    """给子进程准备一份干净的环境。

    宿主可能带着仓库自己的 PYTHONPATH / VIRTUAL_ENV，那会让探活用的是开发环境而不是
    便携包里的 runtime，测出来的结论没有意义。这里全部剔掉。
    """
    env = dict(os.environ)
    for name in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'PYTHONSTARTUP',
                 'AZURPILOT_CONFIG_DIR'):
        env.pop(name, None)
    # 不写 __pycache__，避免污染待测目录。
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    # 让子进程知道自己是探活模式，业务侧可以据此跳过副作用。
    env['AZURPILOT_PROBE'] = '1'
    # 指向一次性配置目录，不要碰 root/config。
    #
    # 两个理由：
    # 1. 探活可能发生在 WebUI 已经运行的时候（用户点"检查更新"再点"应用"）。
    #    共用 root/config 会让两边抢同一个 SQLite 的总库初始化锁，
    #    initialize() 会一直等，探活只能耗到超时被误判成"候选版起不来"。
    # 2. 探活要验证的是"应用能起来 + dist 能挂上"，与用户的真实配置无关，
    #    空库走首次启动路径反而更干净。
    env['AZURPILOT_CONFIG_DIR'] = str(Path(root) / PROBE_CONFIG)
    runtime = str(Path(root) / 'runtime')
    # cv2 / onnxruntime 的 .pyd 会在自己目录和 PATH 上找 DLL。
    env['PATH'] = os.pathsep.join([runtime, os.path.join(runtime, 'Library', 'bin'),
                                   env.get('PATH', '')])
    return env


def get(url):
    """发一个 GET，返回 (status_code, body_bytes)。

    连接被拒 / 超时都算"还没起来"，返回 (None, b'') 让外层继续轮询。
    """
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as response:
            return response.status, response.read(MARKER_WINDOW)
    except urllib.error.HTTPError as e:
        # 有响应但状态码不对，说明服务已经起来了，应把这视为失败而不是"还没起"。
        return e.code, b''
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return None, b''


def tail(path, lines=40):
    """取日志文件最后几行，失败时原样返回异常文本。"""
    try:
        text = Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError as e:
        return f'<读取日志失败: {e}>'
    return '\n'.join(text.splitlines()[-lines:])


def probe(root, timeout=40.0):
    """真正起一次 WebUI 并验证两个端点。

    Args:
        root (str | Path): 待测的便携包根目录（含 runtime/、gui.py、frontend/dist）。
        timeout (float): 总等待秒数，到点仍未通过就判定超时。

    Returns:
        int: 0 通过，1 断言失败，2 超时。
    """
    root = Path(root).resolve()
    python = root / 'runtime' / ('python.exe' if os.name == 'nt' else 'python')
    if not python.is_file():
        print(f'[probe] 找不到解释器: {python}', file=sys.stderr)
        return 1
    if not (root / 'module' / 'api' / 'app.py').is_file():
        # 探活直接 import create_app，这个文件不在就说明包结构不对
        print(f'[probe] 找不到 module/api/app.py: {root}', file=sys.stderr)
        return 1
    if not (root / 'frontend' / 'dist' / 'index.html').is_file():
        print('[probe] 找不到 frontend/dist/index.html，前端没构建', file=sys.stderr)
        return 1

    port = free_port()
    log_path = root / 'log' / 'probe-stdout.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # 直接起 ASGI 应用，不走 gui.py。
    #
    # gui.py 启动前会检查前端资源并「必要时 npm install + vite build」，
    # 一次要十几秒到几分钟，探活的超时窗口根本等不到服务起来——第一版就是
    # 在这里误判成"候选版起不来"。而且探活想验证的是「应用能起来 + dist 能挂上」，
    # 这两件事跟 gui.py 的启动前置无关。
    #
    # manage_runtime=False 关掉调度器与 RPC 生命周期：探活不该真的去连模拟器。
    # mount_mcp=False 省掉 MCP 端点，让它更快就绪。
    # 密码传空 + 只听 127.0.0.1，避免把用户密码带进命令行。
    launcher = (
        'import uvicorn; '
        'from module.api.app import create_app; '
        f"uvicorn.run(create_app(password='', manage_runtime=False, mount_mcp=False), "
        f"host='127.0.0.1', port={port})"
    )
    command = [str(python), '-B', '-c', launcher]
    print(f'[probe] 启动: {python} -B -c "<uvicorn create_app port={port}>"')
    handle = open(log_path, 'w', encoding='utf-8', errors='replace')
    try:
        child = subprocess.Popen(
            command, cwd=str(root), env=probe_env(root),
            stdout=handle, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
    except OSError as e:
        handle.close()
        print(f'[probe] 子进程启动失败: {e}', file=sys.stderr)
        return 1

    base = f'http://127.0.0.1:{port}'
    deadline = time.monotonic() + timeout
    health_ok = False
    try:
        while time.monotonic() < deadline:
            if child.poll() is not None:
                print(f'[probe] 子进程提前退出，退出码 {child.returncode}', file=sys.stderr)
                print(tail(log_path), file=sys.stderr)
                return 1

            if not health_ok:
                status, _ = get(base + HEALTH_PATH)
                if status == 200:
                    health_ok = True
                    print(f'[probe] {HEALTH_PATH} -> 200')
                elif status is not None:
                    print(f'[probe] {HEALTH_PATH} -> {status}', file=sys.stderr)
                    return 1

            if health_ok:
                status, body = get(base + INDEX_PATH)
                if status == 200:
                    if ROOT_MARKER in body:
                        print(f'[probe] {INDEX_PATH} -> 200 且含 <div id="root">')
                        print('[probe] 通过')
                        return 0
                    # 200 但没有 root 节点，说明 dist 里是别的东西（或被中间件换了内容）。
                    print(f'[probe] {INDEX_PATH} -> 200 但正文没有 {ROOT_MARKER!r}',
                          file=sys.stderr)
                    print(body[:400].decode('utf-8', 'replace'), file=sys.stderr)
                    return 1
                elif status is not None:
                    print(f'[probe] {INDEX_PATH} -> {status}', file=sys.stderr)
                    return 1

            time.sleep(POLL_INTERVAL)

        print(f'[probe] {timeout:.0f}s 内未通过', file=sys.stderr)
        print(tail(log_path), file=sys.stderr)
        return 2
    finally:
        # 无论怎么退出都不能留孤儿进程，否则用户下次启动会撞端口。
        if child.poll() is None:
            child.kill()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print('[probe] 子进程未能回收', file=sys.stderr)
        handle.close()
        # 一次性配置目录用完就删，别在包里留垃圾，也别让它被签名清单扫到
        shutil.rmtree(Path(root) / PROBE_CONFIG, ignore_errors=True)


def dump_summary(root):
    """把探活结果写成 JSON，方便 CI / 打包脚本读取。"""
    root = Path(root).resolve()
    python = root / 'runtime' / ('python.exe' if os.name == 'nt' else 'python')
    summary = {
        'root': str(root),
        'python': str(python),
        'pythonExists': python.is_file(),
        'gui': (root / 'gui.py').is_file(),
        'frontend': (root / 'frontend' / 'dist' / 'index.html').is_file(),
    }
    out = root / 'probe-summary.json'
    try:
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    except OSError as e:
        print(f'[probe] 写汇总失败: {e}', file=sys.stderr)
    return summary


def main(argv=None):
    """命令行入口。"""
    parser = argparse.ArgumentParser(description='便携包启动探活')
    parser.add_argument('--root', default='.', help='便携包根目录，默认当前目录')
    parser.add_argument('--timeout', type=float, default=40.0, help='总等待秒数，默认 40')
    args = parser.parse_args(argv)

    dump_summary(args.root)
    return probe(args.root, timeout=args.timeout)


if __name__ == '__main__':
    raise SystemExit(main())
