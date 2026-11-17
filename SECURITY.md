# 安全与隐私

本项目的 `cookie.txt` 与 B 站账号登录凭证等价，严禁提交到 Git 仓库。

不应提交的内容包括：

- `cookie.txt`
- `account_identity.json`
- `account_avatar_*.png`
- `settings.json`
- `data/`
- `live_events*.jsonl`
- `history_events*.jsonl`
- `danmakus_history.json`
- `rooms.json`
- `live_account.json`
- 报告 HTML
- 日志、心跳文件和截图录屏

仓库中的 `.gitignore` 已默认排除这些文件。提交前仍应运行：

```powershell
git status --short
git grep -n "cookie\|SESSDATA\|DedeUserID"
```

如凭证意外提交，应立即在 B 站退出全部会话并更换登录凭证，同时清理 Git 历史。

