# 钉钉 → 家里打印机（dingtalk-print-bridge）

在钉钉里把图片/文件**转发给机器人**，回一个「1」就打到家里 NAS 上的 Canon TS3380。

```
钉钉云 ──WSS 长连接（NAS 主动连出，无需公网/端口映射）──▶ dingtalk-print-bridge
                                                          │ ① downloadCode 下载附件
                                                          │ ② /api/upload 上传
                                                          │ ③ /api/print 提交
                                                          │ ④ /api/jobs 轮询结果
                                                          ▼
                                                    cups-web-print :5000 ──▶ TS3380
```

设计取舍：

- **不接公网**。钉钉 Stream 模式是长连接，NAS 只出站，路由器不用开端口，家里网络不暴露。
- **二次确认**。打印要花钱，收到文件只回一张待打印卡片，回 `1` 才真正提交。
- **多图合并**。连发 9 张作业照 → 合并成一个 PDF 打一次，不会 9 张纸挨个进纸。
- **失败说人话**。打印机离线、USB 卡死、卡纸，把 `/api/jobs` 里的真实原因原样回给你。

## 一、钉钉开放平台配置（一次性，约 10 分钟）

1. **登录开放平台**：<https://open-dev.dingtalk.com/>，钉钉扫码。没有组织就先「创建团队」（免费的，名字随便，比如"家里"）。
2. **建应用**：应用开发 → 企业内部应用 → 创建应用 → 名称「家里打印机」。
   - 创建完在「凭证与基础信息」里拿 **AppKey / AppSecret**（对应 `DINGTALK_CLIENT_ID` / `DINGTALK_CLIENT_SECRET`）。
3. **开机器人**：应用 → 「机器人」→ 开启。
   - 消息接收模式选 **Stream 模式**（这点最关键，选了才不用公网地址）。
   - 机器人名字就是你在钉钉里看到的名字（本项目的回复卡片标题写死成「家里打印机」，要改就改 `bridge/bot.py` 里 `_reply`）。
4. **加权限**：应用 → 「权限管理」→ 搜索「机器人」，把**消息接收**和**发送消息**相关权限加上（控制台里权限名以界面为准）。保存。
5. **发布版本**：应用 → 「版本管理与发布」→ 创建版本 → 发布。企业内部应用一般立即生效。
   - 不发布版本，机器人不会出现在钉钉里，消息也收不到。
6. **开始用**：
   - 单聊：钉钉里搜索应用名，或从「工作台」进入应用，直接发图片/文件。
   - 群聊：群设置 → 机器人 → 添加机器人 → 选「家里打印机」，之后 `@家里打印机` + 图片/文件。
   - 第一次发消息后看容器日志里的 `senderStaffId`，把它填进 `.env` 的 `ALLOW_USER_IDS`，避免别人也能往你家打印机发东西。

> ⚠️ 机器人**读不到群历史消息**，只能收到「发给它」或「群里 @它」的消息。所以流程是：长按消息 → 转发给「家里打印机」，或直接在它的单聊窗口里发。
>
> ⚠️ 单聊窗口能不能直接发**文件**（PDF/Word）取决于钉钉客户端版本；图片肯定可以。如果单聊发不了文件，就用「群 + @机器人」的方式，或者用网页版打印服务上传。

## 二、部署

```bash
cd dingtalk-bridge
cp .env.example .env
# 填 DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET，按需改 PRINT_SERVICE / 默认参数

docker compose up -d --build
docker compose logs -f
```

启动日志里出现 `Stream 机器人启动` 就成功了。

### 不上 Docker 直接跑

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m bridge.main
```

### 自检

```bash
# 打印服务 + 打印机 + 钉钉凭证（不出纸）
.venv/bin/python -m bridge.main --check

# 只上传不打印（安全，用来确认链路）
.venv/bin/python self_test.py --upload 某个.jpg

# 真打一张（会出纸）
.venv/bin/python self_test.py --print 某个.pdf --mono --copies 1
```

## 互动卡片（按钮 + 进度）

卡片模板在 **钉钉卡片平台**（开放能力 → 卡片平台 → 模板管理）里搭，当前已发布的模板：

```
CARD_TEMPLATE_ID=2b646958-9391-4e48-aff2-c96ddb276165.schema
```

模板结构：

- 基础文本，内容 `${content}`（机器人每次投放时传入这段 markdown）
- 按钮组（指定 2 个按钮）
  - 确认打印 → 点击后做什么 = **回传请求**，Action ID = `confirm`
  - 取消 → 回传请求，Action ID = `cancel`

投放卡片时 `callbackType` 必须是 `STREAM`，按钮点击才会通过 Stream 长连接回调到
`/v1.0/card/instances/callback`（`bridge/card_callback.py` 处理）。

⚠️ 模板**发布后不能再编辑**。要改样式/文案就新建一个模板，把新 ID 填进 `.env` 的
`CARD_TEMPLATE_ID` 再重启容器。

⚠️ 卡片交互接口需要应用权限 `Card.Instance.Write`（已开通）。创建模板的
`Card.Template.ReadWrite.All` 这个组织申请不到，所以模板走卡片平台手搭 + 把我们调好的
JSON 直接导入（`doc.importSchema`），不走 API 建模板。

### 测试卡片

```bash
python card_test.py 0344694131836   # 给指定 staffId 发一张测试卡片
```

## 三、怎么用

| 你发 | 机器人回 |
| --- | --- |
| 图片 / 文件 | 待打印卡片（文件清单 + 当前参数） |
| 连发多张图 | 等 8 秒静默后合并成一批，回**一张**卡片 |
| `1` | 按卡片上的参数打印 |
| `2` | 黑白打印 |
| `3` / `4` | 单面 / 双面打印 |
| `0` | 取消，不打印 |
| `/p 2份 黑白 单面 A4 1-3` | 自定义参数后打印 |
| `第2-3页 黑白` | 只打 2–3 页 |
| `/status` | 打印机状态 + 队列 |
| `/help` | 用法 |

默认参数（`.env` 可改）：**A4 · 彩色 · 双面长边 · 1 份 · 适应纸张**，与网页版和 `cups-print` skill 一致。

## 四、常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| 发消息机器人没反应 | 应用没发布版本；机器人消息接收没选 Stream；群里没 @ 它 |
| `下载失败 401/403` | 权限没加全，或 `DINGTALK_ROBOT_CODE` 不对（企业内部机器人一般 = AppKey） |
| `上传失败 HTTP 4xx` | `PRINT_SERVICE` 地址错；或文件类型不在白名单（HEIC 不支持，先转 JPG） |
| 卡片显示但不打印 | 打印机离线，机器人会直接说原因；USB 卡死等情况等 1–2 分钟看门狗复位后回 `1` 重试 |
| 打印一半失败 | 回 `/status` 看队列和真实原因 |
| staffId 在哪看 | 容器日志里 `收到消息 type=... from=昵称(staffId)` |

## 五、目录

```
bridge/config.py        环境变量
bridge/dingtalk_api.py  钉钉 access_token + 附件下载
bridge/print_client.py  cups-web-print 客户端（upload/print/jobs）
bridge/bot.py           机器人逻辑（批次、指令、卡片、结果汇报）
bridge/main.py          入口 + --check 自检
self_test.py            不依赖钉钉的链路自检
```

## 六、还可以继续做

- **真正的按钮卡片**：在钉钉卡片平台建模板 + 注册 `Card_Callback_Router_Topic` 回调，就能点「打印」按钮而不是回数字（SDK 里 `Card_Callback_Router_Topic` 已支持，需要额外建模板）。
- **打印历史**：落一份 SQLite，`/history` 查最近打了什么。
- **家人白名单分组**：不同 staffId 给不同默认参数（比如老人默认黑白省墨）。
- **取件提醒**：打完后如果有别的设备（小爱）可以顺手播报。
