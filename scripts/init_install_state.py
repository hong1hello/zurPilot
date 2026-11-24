"""给便携包写入初始安装状态：RECEIPT + FILES。

事务更新靠这两份文件判断"本地是什么版本"和"哪些文件是原装的"：

RECEIPT（.azurpilot/independent.json）
    {schema, revision, dependencies}
    - revision 让 check() 知道本地对应哪个提交，否则会被当成"全 0"，
      用户拿到包后永远显示有更新
    - dependencies 是依赖指纹，换版本时用它挡住"依赖不匹配就别装"

FILES（.azurpilot/files.json）
    {相对路径: sha256}
    - apply() 拿它当基线：目标文件存在但哈希与基线不同 = 用户改过，拒绝覆盖
    - 缺了这份文件，每个待更新文件都会被判成"本机修改"，更新永远装不上

必须在签名**之前**跑：这两份文件本身不参与签名（.azurpilot 在
release_manifest 的 EXCLUDE_DIRS 里），但 FILES 的哈希必须与包内实际
文件一致，所以要等源码复制完、前端就位之后才能算。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deploy import independent as ind


def main(argv=None):
    """命令行入口。

    Args:
        argv (list[str] | None): 命令行参数。

    Returns:
        int: 进程退出码。
    """
    parser = argparse.ArgumentParser(description='初始化便携包的更新状态')
    parser.add_argument('--root', default='.', help='便携包根目录')
    parser.add_argument('--revision', required=True, help='写入 RECEIPT 的提交哈希')
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if len(args.revision) != 40:
        print(f'[init] revision 必须是 40 位: {args.revision}', file=sys.stderr)
        return 2
    if not (root / 'gui.py').is_file():
        print(f'[init] 不像便携包根目录（缺 gui.py）: {root}', file=sys.stderr)
        return 2

    # 按白名单收集文件，与 apply() 的比对范围严格一致。
    # 多收（比如把 __pycache__ 记进来）会让真实更新时的 changed 计算出现
    # 本地根本没有的条目，进而误判"本机修改"。
    files = {}
    skipped = 0
    for path in root.rglob('*'):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if not (ind.app_path(relative) or ind.app_path(relative, frontend=True)):
            skipped += 1
            continue
        files[relative] = ind.digest(path)

    receipt = {
        'schema': 1,
        'revision': args.revision,
        'dependencies': ind.dependency_id(root),
    }
    ind.write_json(root / ind.RECEIPT, receipt)
    ind.write_json(root / ind.FILES, files)
    # 清掉可能从上一轮打包残留的事务记录，否则新包一启动就去回滚
    journal = root / ind.WORK / 'transaction.json'
    if journal.exists():
        journal.unlink()

    print(f'[init] RECEIPT: revision={args.revision} dependencies={receipt["dependencies"][:16]}…')
    print(f'[init] FILES: {len(files)} 个文件（跳过 {skipped} 个非白名单项）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
