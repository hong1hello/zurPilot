"""任务排程保存实际时刻，配置界面仍使用本地时间。

`Scheduler.NextRun` 在磁盘上存的是"下一次要跑的具体时刻"。如果只存本地 naive 时间，
一到夏令时回拨那天，同一个本地时刻会出现两次，调度器无法判断哪一次已经跑过，
任务就会被重复执行或者被跳过一整天。

这里的做法是：**写盘时固定为带时区的 UTC 时刻**（读回来能按真实先后比较），
**展示和界面交互时再换回本地时间**（保留 `fold`，回拨的第二个小时不会和第一个混淆）。
老配置里已经是本地 naive 时间的，第一次读取时原地迁移一次，只改写真正变了的字段。
"""
import os
import time
from datetime import datetime, timezone

from module.config.config_generated import GeneratedConfig
from module.config.transaction import config_transaction
from module.config.utils import DEFAULT_TIME, read_file, write_file


if os.name == 'nt':
    # Windows 的 CRT 首次读取时区后会缓存，进程跑过夏令时边界也不会自己刷新。
    # ucrtbase 里的 _tzset 是唯一能强制重读系统时区的入口，标准库的 time.tzset 只在
    # Unix 上存在，所以这里必须走 ctypes。
    from ctypes import CDLL

    _tzset = CDLL('ucrtbase')._tzset
    _tzset.restype = None
    _tzset.argtypes = []
else:
    _tzset = getattr(time, 'tzset', None)


def refresh_local_timezone():
    """刷新进程的本地时区缓存。

    Windows 的 CRT 首次读取后会缓存时区，跨夏令时边界时不会自动更新。
    长跑进程（调度器）在每个排程周期前调一次即可。
    """
    if _tzset is not None:
        _tzset()


def is_next_run_path(keys):
    """判断键路径是否指向某个任务的 Scheduler.NextRun。

    Args:
        keys (str | list | tuple): 键路径，如 'Alas.Scheduler.NextRun'。

    Returns:
        bool: 命中 '*.Scheduler.NextRun' 这种三段式路径时返回 True。
    """
    path = keys.split('.') if isinstance(keys, str) else keys
    return (
        isinstance(path, (list, tuple))
        and len(path) == 3
        and path[1] == 'Scheduler'
        and path[2] == 'NextRun'
    )


def parse_next_run(value):
    """把配置里的 NextRun 解析成 datetime。

    Args:
        value: 磁盘上的原始值，可能是 ISO 字符串、datetime，或历史遗留的空值。

    Returns:
        datetime | None: 能解析时返回 datetime，否则返回 None。
    """
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return value if isinstance(value, datetime) else None


def _to_utc(value):
    """把 datetime 转成 UTC，转换不了时返回 None。

    Windows 的本地时间转换不支持某些很早或很晚的日期（会抛 OSError / OverflowError），
    这类值原样保留，不要因为迁移把用户配置搞坏。
    """
    try:
        return value.astimezone(timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def schedule_utc(value):
    """将排程时间或当前时间转换为可以按实际先后比较的 UTC 时间。

    Args:
        value (datetime): 排程时间或当前时间。

    Returns:
        datetime: 带 UTC 时区的时刻。无法转换的极值按原样附上 UTC 时区。
    """
    return _to_utc(value) or value.replace(tzinfo=timezone.utc)


def schedule_local(value):
    """保留任务使用的本地 datetime 接口，包括夏令时回拨时的 fold。

    Args:
        value (datetime): 带时区的时刻。

    Returns:
        datetime: 本地 naive datetime；入参本身是 naive 时原样返回。
    """
    if not isinstance(value, datetime) or value.tzinfo is None:
        return value
    try:
        return datetime.fromtimestamp(value.timestamp())
    except (OSError, OverflowError, ValueError):
        return value.replace(tzinfo=None)


def normalize_next_run(value):
    """固定已排定的实际时刻，保留代表立即运行的默认值和无法转换的旧值。

    Args:
        value: 配置里的 NextRun 原始值。

    Returns:
        归一化后的值：能转 UTC 的转成 UTC datetime，否则原样返回。
    """
    moment = parse_next_run(value)
    if moment is None or moment in (DEFAULT_TIME, GeneratedConfig.Scheduler_NextRun):
        # 默认值代表"立即运行"，转成 UTC 反而会让语义变模糊，保持原样。
        return value
    return _to_utc(moment) or value


def encode_next_run(value):
    """写盘格式：带时区的 ISO 字符串。

    Args:
        value: 配置里的 NextRun 原始值。

    Returns:
        str | 原值: 能转换的写成 ISO 字符串，转换不了的保持原样。
    """
    value = normalize_next_run(value)
    return str(value) if isinstance(value, datetime) else value


def decode_next_run(value):
    """读盘转换：带时区的字符串 → 本地 naive datetime。

    这是 `encode_next_run` 的逆运算，也是业务侧看到的口径。
    必须转成 naive：调度比较用的 `current_time()` 是本地 naive，
    如果这里留着 tzinfo，`next_run > limit` 会抛
    `TypeError: can't compare offset-naive and offset-aware datetimes`。

    已是 naive datetime 的值原样返回；解析不了的（默认值、非法字符串）也原样返回，
    交给下游的 isinstance 判断兜住。

    Args:
        value: 配置里的 NextRun 原始值。

    Returns:
        datetime | 原值: 本地 naive datetime，或无法转换时的原值。
    """
    moment = parse_next_run(value)
    if moment is None:
        return value
    if moment.tzinfo is None:
        # 已经是本地 naive（老配置、或刚被其它代码写过），不用动
        return moment
    return schedule_local(moment)


def display_next_run(value):
    """读盘后转成本地时间用于界面展示。

    Args:
        value: 配置里的 NextRun 原始值。

    Returns:
        str | 原值: 带时区的值转成本地时间字符串，其余原样返回。
    """
    moment = parse_next_run(value)
    if moment is not None and moment.tzinfo is not None:
        return str(schedule_local(moment))
    return value


def map_next_runs(data, convert):
    """仅转换实例各任务的 Scheduler.NextRun，返回是否有字段变化。

    Args:
        data (dict): 整份配置文件内容，形如 {'Alas': {'Scheduler': {...}}, ...}。
        convert (callable): 单值转换函数，如 encode_next_run。

    Returns:
        bool: 有字段被改写时返回 True。
    """
    if not isinstance(data, dict):
        return False
    changed = False
    for groups in data.values():
        if not isinstance(groups, dict):
            continue
        scheduler = groups.get('Scheduler')
        if not isinstance(scheduler, dict) or 'NextRun' not in scheduler:
            continue
        value = convert(scheduler['NextRun'])
        if value != scheduler['NextRun']:
            scheduler['NextRun'] = value
            changed = True
    return changed


def read_schedule_config(path):
    """首次读取旧实例时固定已有排程，只写回实际变化的 NextRun。

    先读一次判断要不要迁移，需要迁移才进事务锁；进锁之后再读一次并以锁内的数据为准，
    这样期间别的进程写进来的改动不会被这次迁移覆盖。

    Args:
        path (str | Path): 配置文件路径，对应 ./config/{name}.json。

    Returns:
        dict: 迁移后的配置内容。
    """
    data = read_file(path)
    if map_next_runs(data, encode_next_run):
        with config_transaction(path):
            # 锁内重读：外层那次读取到进锁之间可能有别的进程写过。
            data = read_file(path)
            if map_next_runs(data, encode_next_run):
                write_file(path, data)
    return data
