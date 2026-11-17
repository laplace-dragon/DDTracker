# danmakus 数据同步

基础地址：`https://api.ukamnads.icu`

官网：<https://danmakus.com/>

## 使用接口

### 用户历史

```http
GET /api/v3/users/{userId}/history
```

参数：

| 参数 | 说明 |
|---|---|
| `page` | 页码，从 1 开始 |
| `pageSize` | 每页直播场数 |
| `channelUserId` | 只取指定主播的记录 |
| `hideOnlyEnter` | 隐藏仅有入场记录的直播场 |

返回每场直播的：

- 主播 UID、昵称、房间号
- 直播 ID、标题、分区、开播时间、下播时间
- 本人弹幕、礼物、上舰、SC、进入事件
- 事件精确时间戳、内容和金额

### 单场用户事件

```http
GET /api/v3/lives/{liveId}/danmakus?userId={userId}
```

支持 `offset`、`limit` 和重复传入 `type` 过滤。用于对单场历史做进一步交叉校验。

### 看过直播间

```http
GET /api/v2/user/watchedChannels?uid={userId}
```

只返回看过的频道及频道统计，不包含个人停留时长或退出时间。

## 本地增量同步

默认增量同步：

```powershell
DD轨迹.exe sync-danmakus
```

首次运行或本地没有同步状态时，会建立全量远程基线。之后：

1. 读取 `danmakus_v3_sync_state.json` 中的上次同步时间；
2. 历史列表按开播时间倒序分页，只翻到上次同步边界；
3. 上次仍在直播的场次会继续覆盖，避免漏掉跨同步时间的新事件；
4. 只处理上次同步时间之后的新事件；
5. 将全部远程事件保存在本地，并与实时采集、手工补录匹配；
6. 只把新发现的本地缺失事件追加到补充集。

历史列表接口没有 `startTime` 参数，因此无法让服务器直接按日期返回；
客户端利用倒序分页提前停止，不会每次重新拉取全部 22 页。

使用已有快照，不联网：

```powershell
DD轨迹.exe sync-danmakus --cached
```

强制重建全量远程基线：

```powershell
DD轨迹.exe sync-danmakus --full
```

输出：

- `data\danmakus_v3_snapshot.json`
- `data\danmakus_v3_remote_events.jsonl`：全部远程事件本地基线（匹配后的规范化数据）
- `data\danmakus_v3_events.jsonl`
- `data\danmakus_v3_sync_audit.json`
- `data\danmakus_v3_sync_state.json`：上次同步时间、远程事件数和补充集大小

审计字段中：

- `remote_only_added`：本次同步真正新增到补充集的条数；
- `supplement_total`：当前补充集总条数；
- `new_remote_events`：本次请求范围内、之前本地远程基线中不存在的新事件数。

## 客户端与每日次数

客户端的按钮名称为“同步danmakus数据”。同步成功后会自动重启互动记录并生成报告。

- 每个香港自然日最多执行 5 次。
- 第 1～5 次正常执行，不显示额度或剩余次数。
- 第 6 次起弹出对话框“已到当日使用上限”，不会发出请求或改写数据。
- 每次获准执行都会立即计次；即使随后因网络异常失败，该次仍计入当天次数。
- 计数保存在程序目录的 `danmaku_sync_quota.json`，跨客户端重启仍然有效。
- 额度检查和同步任务都有跨进程互斥保护，不能靠连续点击或多开命令行绕过。

## 数据限制

- 当前接口标记 422 场，实际分页可能少于该数量。
- 不提供粉丝牌名称、粉丝牌等级或勋章升级历史。
- 不提供退出直播间事件和个人实际播放时长。
- 观看时长仍只能根据进入、弹幕、礼物等公开事件估算。
- `/api/v3/user-analysis-jobs/{jobId}/events` 是分析进度 SSE，不是用户历史明细。
- 服务端限流约为每分钟 100 次，每日 5 次是 DD轨迹 自身的产品限制。
