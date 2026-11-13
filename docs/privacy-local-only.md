# 本地优先：关闭云端上传

本 fork 相对上游 `wess09/AzurPilot` 的差异，只有一件事：**不允许任何外发上报**。

## 锁在哪

单一开关在 `module/statistics/privacy.py`：

```python
CLOUD_UPLOAD_ENABLED = False      # 云端掉落/模板/截图上传
TELEMETRY_REPORT_ENABLED = False  # 侵蚀 1 月度遥测
```

两处消费点：

| 位置 | 作用 |
|---|---|
| `module/statistics/cloud_upload.py` `permitted()` | 恒返回 False → `resolver_for()` 给出 None → 走本地识别路径 |
| `module/os/tasks/hazard_leveling.py` | 遥测提交前判断，锁定关闭 |

配置默认值也同步改成关闭（`config_generated.py`）：

```python
DropRecord_CloudUpload = False
DropRecord_ResearchRecord = 'save'
DropRecord_CommissionRecord = 'save'
DropRecord_TelemetryReport = False
```

`'save'` 而不是 `'do_not'`：截图仍写本地 `./screenshots`，掉落仍解析进本地数据库，
只是不外发。

## 顺带去掉的性能开销

上游在开关打开时有四处只为上传服务的工作，关掉后全部消失：

1. `item.py` 每件物品调用云端 `resolver(...)` → `CloudService.identify()` 的
   两次模板匹配（formal + temporary）、DCT 特征、未知图标的 SHA-256 与 PNG 编码、
   `_capacity()` 的目录 glob + stat 扫描
2. `cloud_upload.RewardResolver.capture()` → 整帧 1280×720 无损 PNG 编码 + SHA-256
3. `azurstats.add()` 委托分支 → 每帧 `classify_frame()` 与整帧 `np.array_equal` 去重
4. `capture_commission_status()` → 又一次 `classify_frame()`

## 恢复上游行为

把 `CLOUD_UPLOAD_ENABLED` / `TELEMETRY_REPORT_ENABLED` 改回 `True`，
再把 `config_generated.py` 的四个默认值改回即可。
