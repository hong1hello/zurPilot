"""Scheduler.NextRun 时区固定与本地时间展示的回归测试。

覆盖三类输入：磁盘上的带时区 ISO 字符串、老配置的本地 naive datetime、
以及代表"立即运行"的默认值。核心断言是 **往返等价**——本地时间编码成 UTC
再解回本地，必须还是同一个时刻，否则调度器会在夏令时切换那天错位一小时。
"""
import datetime
import os
from datetime import timezone

import pytest

from module.config.schedule_time import (
    display_next_run,
    encode_next_run,
    is_next_run_path,
    map_next_runs,
    normalize_next_run,
    parse_next_run,
    refresh_local_timezone,
    schedule_local,
    schedule_utc,
)
from module.config.utils import DEFAULT_TIME


def test_parse_next_run_accepts_iso_and_datetime():
    assert parse_next_run('2026-11-18T12:30:00+00:00') == datetime.datetime(
        2026, 11, 18, 12, 30, tzinfo=timezone.utc)
    naive = datetime.datetime(2026, 11, 18, 20, 30)
    assert parse_next_run(naive) is naive


@pytest.mark.parametrize('value', [None, '', 'not-a-date', 123, [], {}])
def test_parse_next_run_rejects_garbage(value):
    assert parse_next_run(value) is None


def test_round_trip_preserves_wall_clock():
    """本地 → UTC → 本地 必须回到同一时刻。"""
    local = datetime.datetime(2026, 11, 18, 20, 30, 0)
    encoded = encode_next_run(local)
    assert isinstance(encoded, str)
    # 编码后必须带时区偏移，否则说明被当成 naive 处理了
    assert '+' in encoded or encoded.endswith('Z')
    assert display_next_run(encoded) == str(local)


def test_aware_input_normalizes_to_utc():
    """已经是 UTC 的值再编码不该漂移。"""
    aware = datetime.datetime(2026, 11, 18, 12, 30, tzinfo=timezone.utc)
    encoded = encode_next_run(aware)
    assert parse_next_run(encoded).astimezone(timezone.utc) == aware


def test_default_time_is_kept_as_is():
    """默认值代表立即运行，不该被转成 UTC。"""
    assert normalize_next_run(DEFAULT_TIME) is DEFAULT_TIME
    encoded = encode_next_run(DEFAULT_TIME)
    assert '+' not in encoded
    # 往返后仍然是那个默认时刻
    assert parse_next_run(encoded) == DEFAULT_TIME


def test_schedule_helpers_are_inverse():
    moment = datetime.datetime(2026, 3, 8, 2, 30, tzinfo=timezone.utc)
    local = schedule_local(moment)
    assert local.tzinfo is None
    assert schedule_utc(local).astimezone(timezone.utc) == moment


def test_map_next_runs_touches_only_scheduler_next_run():
    data = {
        'Alas': {
            'Scheduler': {'NextRun': datetime.datetime(2026, 11, 18, 20, 0), 'Enable': True},
            'Other': {'NextRun': 'untouched'},
        },
        'Research': {'Scheduler': {'NextRun': '2023-01-01 00:00:00'}},
    }
    assert map_next_runs(data, encode_next_run) is True
    # 只改 Scheduler.NextRun，同级的 Enable 与别组的 NextRun 都不动
    assert '+' in data['Alas']['Scheduler']['NextRun']
    assert data['Alas']['Scheduler']['Enable'] is True
    assert data['Alas']['Other']['NextRun'] == 'untouched'
    assert data['Research']['Scheduler']['NextRun'] == '2023-01-01 00:00:00'


def test_map_next_runs_is_idempotent():
    """已经归一化过的数据再跑一次不该再报变更。"""
    data = {'Alas': {'Scheduler': {'NextRun': datetime.datetime(2026, 11, 18, 20, 0)}}}
    assert map_next_runs(data, encode_next_run) is True
    assert map_next_runs(data, encode_next_run) is False


def test_map_next_runs_tolerates_malformed_input():
    assert map_next_runs(None, encode_next_run) is False
    assert map_next_runs('text', encode_next_run) is False
    assert map_next_runs({'Alas': 'not-a-dict'}, encode_next_run) is False
    assert map_next_runs({'Alas': {'Scheduler': 'not-a-dict'}}, encode_next_run) is False


@pytest.mark.parametrize('keys,expected', [
    ('Alas.Scheduler.NextRun', True),
    (['Alas', 'Scheduler', 'NextRun'], True),
    ('Alas.Scheduler.Enable', False),
    ('Alas.NextRun', False),
    ('Alas.Scheduler.NextRun.extra', False),
    (None, False),
])
def test_is_next_run_path(keys, expected):
    assert is_next_run_path(keys) is expected


@pytest.mark.skipif(os.name != 'nt', reason='Windows CRT 时区缓存是平台特有问题')
def test_refresh_local_timezone_is_callable_on_windows():
    """Windows 上必须能拿到 ucrtbase 的 _tzset，否则长跑进程跨夏令时会偏一小时。"""
    refresh_local_timezone()
