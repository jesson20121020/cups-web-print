---
name: cups-print
description: 当用户要把文档、图片、PDF 打印出来（家里 NAS 上的 Web 打印服务 cups-web-print / Canon TS3380），或要查看打印机与打印队列状态时使用；把文件传给本 Skill 的脚本即可直接上传并打印
---

# cups-print — 让 NAS 上的打印服务把文件打出来

家里 NAS（192.168.31.190）上跑着 `cups-web-print`（Flask + CUPS），打印机是 Canon TS3380（USB 接 NAS，经 ipp-usb 暴露成 `ipp://127.0.0.1:60000/ipp/print`，队列名 `TS3380-IPPS`）。
本 Skill 的脚本走的是**网页“打印”按钮同一条链路**（`/api/upload` → `/api/print` → `/api/jobs`），所以网页能打的东西脚本都能打，反之亦然。

## 什么时候用

- 用户发来图片 / PDF / Office 文档，并说“打印 / 打出来 / 给我打几份 / 打双面 / 只打第 2 页”之类
- 用户问“打印机现在能用吗 / 有没有卡任务 / 上次那个打完没有”
- 用户想清掉队列里卡住的任务

**只是分享文件、没说要打印时，先问一句**（打印要耗纸耗墨，每张都是真金白银）。

## 入口

从当前加载的 Skill 目录定位脚本，用绝对路径执行（下文用 `PRINT` 代指）：

```bash
PRINT="<skill 目录>/scripts/print-file.sh"
$PRINT --status                 # 服务 + 队列 + 打印机在线状态 + 待打任务
$PRINT <文件路径>                # 按默认参数直接打印，并等结果
```

脚本自带 `--help`。依赖：bash、curl、python3（NAS 上都有）。

## 常见场景

```bash
# 打印一张图（默认：彩色 / 双面长边 / A4 / 1 份 / 适应纸张）
$PRINT /path/to/作业.png

# 黑白省钱、单面、两份
$PRINT /path/to/资料.pdf --mono --simplex --copies 2

# 只打第 2-3、第 5 页（PDF 或 Office 转换后的文档）
$PRINT /path/to/report.pdf --pages "2-3 5"

# 6 寸照片纸、彩色、照片模式
$PRINT /path/to/照片.jpg --paper 4x6 --paper-type photo --simplex

# 先确认服务状态再打（推荐：每次打印前顺手跑一下）
$PRINT --status && $PRINT /path/to/file.docx

# 只上传不打印（用户想先在网页里看看效果）
$PRINT /path/to/file.pdf --no-print --json

# 只校验参数 / 看会提交什么，不联服务
$PRINT /path/to/file.pdf --dry-run
```

参数覆盖：`--printer` `--copies` `--mono` `--simplex` `--duplex-short` `--paper` `--paper-type` `--pages` `--scaling` `--orientation` `--no-print` `--dry-run` `--wait` `--json` `--service` `--force` `--no-composite-black`。

彩色模式下服务端默认给任务带 `-o ColorModel=RGB`（黑色文字由彩墨合成，避免落到「灰度 / K 单色」光栅路径把黑字打淡）。确实想用打印机默认行为时才加 `--no-composite-black`；黑白模式（`--mono`）恒走 K 黑墨，该选项无效。

## 默认值（与网页一致，别自己乱改）

| 项 | 默认 | 备注 |
| --- | --- | --- |
| 队列 | 自动挑第一台 IPP 队列（`TS3380-IPPS`） | 服务里可能还有 `TS3380-TEST` 等测试队列，别选错 |
| 色彩 | 彩色 | 文字资料想省钱用 `--mono` |
| 双面 | 双面长边 | 单页图片、照片建议 `--simplex` |
| 纸张 | A4 | 可选 A3/A5/A6/B4/B5/4x6/5x7/3.5x5/8x10 |
| 缩放 | fit（适应纸张） | 按原始尺寸打用 `--scaling none` |
| 份数 | 1 | 1-99 |

## 判定与流程（agent 按这个走）

1. 用户给的路径可能带 `@`，也可能是相对路径；先确认文件存在（`ls -l`）。
2. 打印前跑一次 `--status`：
   - `online_status=online` → 直接打印。
   - `offline` / `unknown` → **停下来，把 `原因：…` 原样告诉用户**，不要加 `--force`。打印机离线时把任务塞进队列只会卡住（虽然恢复后会自动打，但用户看到的是“没反应”）。
3. 文件类型：支持 `pdf txt doc docx ppt pptx xls xlsx rtf jpg jpeg png gif bmp svg`。
   - HEIC/HEVC 不在支持列表里：让用户重发成 JPG，或先用 `convert`/PIL 转一下再打。
   - 超过 100MB 服务端会拒收。
4. 图片和 Office 文档由**服务端**自动转成 PDF 再打（v1.85 起：图片走 img2pdf 无损、缺失的预览 PDF 会按需补），脚本不用本地转换，也不要用 `lp` 直连绕开服务。
5. 提交后脚本会轮询任务状态，把结果按下面的格式回报；`--wait 0` 表示只提交不等。
6. 同一份文件用户刚打过、又没说要再打 → 先问一句，别重复打。

## 结果怎么汇报

- 成功：`已打印：<文件名>（<色彩>/<单双面>/<纸张>×<份数>，job …）`
- 提交但没确认完：`已提交，任务在队列里（可用 --status 跟进）`
- 失败：把脚本的 `❌ …` 原文贴出来 + 给出的处置建议（多半是打印机离线/纸张/卡纸）
- 只上传：把服务端文件名告诉用户，并提示“可在网页 http://192.168.31.190:5000 预览/再打”

## 出问题怎么办

| 现象 | 处置 |
| --- | --- |
| 服务不可达（exit 2） | `systemctl is-active cups-web-print`、`curl -s localhost:5000/api/printers`；服务代码在 `/vol1/@appcenter/cups-web-print/server`，重启方式见项目 MEMORY |
| 打印机离线且原因是「USB 未被系统识别」 | 见下一条“USB 卡死”；NAS 上有 root 看门狗每 2 分钟自动复位，等 1-2 分钟再试 |
| 任务停在“打印中 90%” | `$PRINT --status` 看任务 message，其中会带 CUPS 的真实原因（v1.85 起） |
| 任务要清掉 | `cancel TS3380-IPPS-<cups_id>`（脚本 `--json` 会给出 `cups_job_id`） |
| 打印很淡/缺色 | 改用 `--mono` 看是否墨盒问题；用网页「打印机详情」看墨量 |

USB 卡死的表现：`lsusb` 看不到 `Canon TS3300`、`systemctl is-active ipp-usb` 非 active、`dmesg` 里有 `usb1-port5: Cannot enable / unable to enumerate`。
手动复位（要 root）：

```bash
P=/sys/bus/usb/devices/usb1/1-0:1.0/usb1-port5
echo 1 | sudo tee $P/disable; sleep 1; echo 0 | sudo tee $P/disable
```

复位后设备会立刻重新枚举，卡在队列里的任务会自动接着打（**不用重新提交**）。

## 维护信息

- 网页：`http://192.168.31.190:5000`（NAS 本机 `http://127.0.0.1:5000`）
- 服务日志：`/vol1/@appdata/cups-web-print/app.log`；CUPS：`/var/log/cups/error_log`
- 看门狗日志：`/var/log/cups-usb-watchdog.log`
- 上传件目录：`/vol1/@appcenter/cups-web-print/server/uploads`；预览 PDF：`…/previews`
- 脚本源码随项目仓库：`cups-web-print/skills/cups-print/scripts/print-file.sh`（本文件同目录）
