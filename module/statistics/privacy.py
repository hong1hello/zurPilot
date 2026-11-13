"""本地隐私锁定：集中控制是否允许任何外发上报。

上游默认开启云端掉落上传与遥测（`DropRecord_CloudUpload=True`、
`DropRecord_TelemetryReport=True`），并且每帧掉落解析都会走云端识别器。
本 fork 把这两个能力锁定为关闭：即使配置文件里写着 `True`，也不会发出任何请求，
掉落数据只保留在本地数据库与截图目录。

Local privacy lock: the single place deciding whether anything leaves this host.

Upstream defaults both switches to True and routes every reward item through the
cloud resolver. This fork pins them off, so a configuration file that still says
True cannot re-enable any upload; drop data stays local.
"""

# 只有显式改成 True 才恢复上游行为。默认锁定关闭。
# Flip to True to restore upstream behaviour; locked off by default.
CLOUD_UPLOAD_ENABLED = False
TELEMETRY_REPORT_ENABLED = False
