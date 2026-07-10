# 郑州大学成绩监控

由 GitHub Actions 每 10 分钟启动一次。电脑关机后也可以继续运行。

## 1. 上传文件

将本项目中的全部文件上传到 GitHub 私有仓库，必须保留：

```text
.github/workflows/grade_monitor.yml
grade_monitor.py
requirements.txt
last_grades.json
failure_count.json
```

## 2. 设置 GitHub Secrets

进入仓库：

`Settings → Secrets and variables → Actions → New repository secret`

依次创建：

| 名称 | 内容 |
|---|---|
| `ZZU_ACCOUNT` | 郑州大学统一身份认证学号 |
| `ZZU_PASSWORD` | 统一身份认证密码 |
| `SEND_KEY` | Server酱 SendKey |

不要把账号、密码或 SendKey 直接提交到仓库。

## 3. 开启 Actions

进入仓库的 `Actions` 页面，选择“郑大成绩监控”，点击：

`Run workflow`

第一次运行只建立成绩基准，不推送已有成绩。以后发现新成绩或成绩变化时才推送。

## 4. 查看运行结果

在 `Actions` 页面打开某次任务即可查看日志。

正常日志大致为：

```text
✓ 登录成功
✓ 共读取到 48 门课程
未发现成绩变化
```

## 注意

- GitHub 的定时任务不是严格准点，繁忙时可能延迟几分钟。
- 如果统一认证出现验证码、滑块或动态认证，纯云端脚本无法人工处理。
- 登录状态和成绩缓存会由 Actions 自动提交回私有仓库。
- 仓库务必设为 Private。
