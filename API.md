# BILILIVE-WATCH 本地接口

基础地址：`http://127.0.0.1:8765`

接口只监听本机 `127.0.0.1`，不会向局域网或外网开放。使用前需要先通过客户端打开“互动记录”。

## 查看手动补录格式

```http
GET /api/manual
```

PowerShell：

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8765/api/manual"
```

## 手动补录

```http
POST /api/manual
Content-Type: application/json
```

字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `time` | 否 | `YYYY-MM-DD HH:MM:SS`，不填使用当前时间 |
| `room` | 是 | 直播间号 |
| `type` | 是 | `danmu` / `gift` / `sc` / `guard` / `entry`，也可填写中文类型 |
| `content` | 视类型 | 弹幕内容、礼物名称、SC 内容、舰长类型等 |
| `amount` | 视类型 | 金额，单位为元；礼物填写总额 |
| `quantity` | 否 | 数量，默认 `1` |
| `estimated` | 否 | 时间或金额为估算时填 `1` |
| `note` | 否 | 补录备注 |

礼物示例：

```powershell
$body = @{
  time = "2026-11-05 20:30:00"
  room = 80397
  type = "gift"
  content = "粉丝团灯牌"
  amount = 0.1
  quantity = 1
  note = "手机账单补录"
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8765/api/manual" `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
```

醒目留言示例：

```json
{
  "time": "2026-11-05 21:10:00",
  "room": 80397,
  "type": "sc",
  "content": "补录的醒目留言内容",
  "amount": 30,
  "note": "录屏补录"
}
```

上舰示例：

```json
{
  "time": "2026-11-05 21:20:00",
  "room": 80397,
  "type": "guard",
  "content": "舰长",
  "amount": 138,
  "quantity": 1,
  "note": "账单补录"
}
```

弹幕与进入记录：

```json
{
  "time": "2026-11-05 21:30:00",
  "room": 80397,
  "type": "danmu",
  "content": "手动补录的弹幕"
}
```

```json
{
  "time": "2026-11-05 21:31:00",
  "room": 80397,
  "type": "entry",
  "content": "进入直播间"
}
```

成功响应：

```json
{
  "ok": true,
  "msg": "手动补录已保存",
  "event": {
    "ts": 1793881800000,
    "room": 80397,
    "uid": 12345678,
    "src": "manual",
    "cmd": "SEND_GIFT"
  }
}
```

所有补录固定写入当前登录的本人账号，不能通过接口指定其他 UID。每条补录都有唯一 `manual_id`，同一秒内的相同记录也不会互相覆盖。
