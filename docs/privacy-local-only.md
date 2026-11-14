# 本地优先：云端上报已删除

本 fork 相对上游 `wess09/AzurPilot` 的差异只有一件事，但做得很彻底：
**云端上报不是被关掉，而是被删掉。**

## 删掉了什么

| 文件 | 原职责 |
|---|---|
| `module/cloud/protocol.py` | `PublicAPI`：唯一发起 HTTP 请求的地方（register / submit / screenshot / statistics） |
| `module/cloud/service.py` | 云端队列、模板目录同步、`identify` / `capture` |
| `module/cloud/files.py` | 仅供云端队列使用的原子写 |
| `module/statistics/cloud_upload.py` | `permitted` / `resolver_for` / `enqueue_record` / `queue_for` |
| `module/statistics/cl1_data_submitter.py` | CL1 月度遥测提交 |
| `module/statistics/privacy.py` | 原先的总开关，删除后已无消费点 |

`module/cloud/catalog.py` 只保留 `map_items`：它是纯函数（把 source id 映射成
canonical id 并聚合数量），本地统计仍在用，没有任何网络或加密依赖。
`module/cloud/__init__.py` 现在只是一个说明包用途的空模块。

## 仍然保留的本地能力

- 掉落截图保存（`./screenshots`，由 `DropRecord_SaveFolder` 决定）
- 本地统计入库（SQLite，`module/persistence/database.py` 托管）
- 科研掉落解析入库（`record_research_drop`）
- 委托掉落解析入库（`record_commission_local`，只写本地库，不上报）
- 物品识别走本地模板匹配（`ItemGrid.match_template`）

## 为什么删除而不是留开关

此前用的是 `CLOUD_UPLOAD_ENABLED = False` 这类硬锁：行为上确实不发请求，但代码
仍在。任何人改一行常量，或从上游合并一次，上报就会完整复活。

删除之后不存在可用的代码路径。要恢复上报必须重新实现，而不是改一个布尔值。

## 恢复上游行为

本 fork 不再提供开关。需要从上游取回上表列出的文件，并把
`azurstats.py` / `commission_drop.py` / `research_drop.py` / `item.py` /
`hazard_leveling.py` 里的调用点逐一还原——等价于回退删除上报的那次提交。

## 相关提交

- `2bdbd5d6` privacy: delete the cloud upload path instead of only switching it off
