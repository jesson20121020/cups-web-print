# 打印预览功能移植说明

本文件记录从 [`hanxi/cups-web`](https://github.com/hanxi/cups-web) 移植打印预览功能到 [`wishday/cups-web-print`](https://github.com/wishday/cups-web-print) 的设计与实施。

> 📅 移植版本：cups-web v0.2.6 → cups-web-print (2024)
>
> 🎯 目标：保留 cups-web-print 的 Flask 后端与简单前端，同时获得 cups-web 的现代化 PDF 实时预览体验。

---

## 🆕 改动一览

| 文件 | 类型 | 说明 |
| --- | --- | --- |
| `preview_routes.py` | **新增** | Flask Blueprint：临时文件缓存 + 文档转 PDF + 流式回传 |
| `static/print-preview.js` | **新增** | PDF.js 集成：上传 → 预览 → 纸张比例自适应 → 水印 |
| `app.py` | **修改** | 在启动早期注册 `preview_bp`，向后兼容，不影响现有 API |
| `templates/index.html` | **修改** | 右侧栏新增"实时打印预览"卡片 + JS 初始化 |

**总新增代码量**：约 800 行（含注释与日志）。

---

## 🏗️ 架构设计

```
用户选择文件 file
   │
   ▼
┌─────────────────┐    POST /api/preview/file     ┌──────────────────────┐
│  static/        │ ──────────────────────────▶ │  preview_routes.py   │
│  print-preview  │  返回 {token, expires_in}     │   _store_temp_file() │
│  .js            │ ◀────────────────────────── │   (previews/_tmp_*) │
└─────────────────┘                              └──────────────────────┘
   │
   ▼  GET /api/preview/pdf/<token>
┌─────────────────┐    流式 application/pdf       ┌──────────────────────┐
│  PDF.js         │ ◀────────────────────────── │  preview_routes.py   │
│  renderPage()   │                              │   ├─ PDF: send_file │
└─────────────────┘                              │   ├─ 图片: img2pdf  │
   │                                              │   └─ Office: LibreOffice
   ▼                                              └──────────────────────┘
canvas + aspect-ratio
纸张真实比例显示
```

### 关键设计决策

1. **token 临时缓存**（而不是直接复用 `uploads/` 文件夹）：
   - 避免污染现有上传/删除逻辑
   - TTL 30 分钟自动过期，无需清理 cron
   - LRU 淘汰上限 64 条，防止磁盘撑爆

2. **流式回传**（`Response(data, mimetype='application/pdf')`）：
   - 不写盘到 `previews/`，避免与原 `convert_pdf_to_images` 流程冲突
   - PDF.js 客户端直接读流渲染

3. **复用现有转换函数**：
   - `convert_image_to_pdf`：图片 → PDF（img2pdf 优先，LibreOffice 兜底）
   - `convert_to_pdf`：Office/文本 → PDF（LibreOffice headless）
   - 保持 cups-web-print 现有的 PDF 标准化管线

4. **完全兼容现有 API**：
   - `/api/preview/<filename>` 保留不动
   - `/api/upload` 保留不动
   - 仅在右侧栏**新增**一个独立预览卡片，不破坏左侧上传流程

---

## 🔌 新增 API 端点

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/preview/file` | 上传文件，返回 `token` |
| `GET` | `/api/preview/pdf/<token>` | 通过 token 取 PDF 流 |
| `DELETE` | `/api/preview/file` | 通过 token 清除缓存 |
| `GET` | `/api/preview/info` | 诊断信息（缓存大小等） |

### 示例：上传 → 预览流程

```bash
# 1. 上传图片
curl -X POST http://localhost:5000/api/preview/file \
  -F "file=@photo.jpg"
# → {"token": "abc123…", "expires_in": 1800, "size": 524288, "name": "photo.jpg"}

# 2. 取 PDF 流（浏览器/PDF.js 使用）
curl http://localhost:5000/api/preview/pdf/abc123... -o preview.pdf

# 3. 主动清理
curl -X DELETE "http://localhost:5000/api/preview/file?token=abc123..."
```

---

## 🖼️ 前端体验

移植自 cups-web 的 `PrintPreview.vue` + `PdfCanvas.vue`，但用原生 JS 重写以适配 cups-web-print 的 Tailwind + 无构建步骤前端。

**核心特性**（与 cups-web 对齐）：
- ✅ 上传后自动 PDF.js 实时渲染
- ✅ 纸张尺寸/方向变化时，预览容器按 **真实宽高比** 重排
- ✅ 多页 PDF 支持页码导航（`< 1/N >`）
- ✅ 高 DPI 适配（`devicePixelRatio` × 2 渲染）
- ✅ 水印叠加（`<canvas>` 斜 45° 平铺，alpha 0.15）
- ✅ 失败降级：预览失败时仍可打印，仅显示提示

**与现有 UI 的联动**：
- 上传文件 → 实时预览同步加载
- 切换纸张 (A4/A3/...) → 预览容器比例更新
- 切换方向 (纵向/横向) → 预览容器比例更新 + 单选按钮高亮同步

---

## ⚠️ 安全注意事项

| 关注点 | 处理方式 |
| --- | --- |
| 路径遍历 | `os.path.basename()` + token 隔离 |
| 任意文件删除 | `_safe_remove()` 仅允许删除 `uploads/` `previews/` `/tmp/` 下的文件 |
| 大文件攻击 | `MAX_CONVERT_BYTES = 100MB`（与 `app.config['MAX_CONTENT_LENGTH']` 对齐） |
| 内存爆炸 | token 缓存上限 64 条 + LRU 淘汰 |
| LibreOffice 命令注入 | 通过 `subprocess.run(list, ...)` 而非 shell 字符串 |

---

## 📦 依赖变化

**无需新增依赖**：复用 cups-web-print 现有的 `Flask>=3.0.0`、`img2pdf>=0.1.0`。

**前端**：`print-preview.js` 通过 CDN 加载 PDF.js（v3.11.174，legacy build，浏览器兼容性最佳）：
```html
<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
```

如需离线部署，可下载 `pdf.min.js` + `pdf.worker.min.js` 到 `static/` 目录并修改 `print-preview.js` 中的 CDN URL。

---

## 🧪 验证方法

启动 cups-web-print，浏览器打开 http://localhost:5000/zh：

1. **空状态**：右侧"实时打印预览"卡片显示"上传文件后显示预览"
2. **上传 PDF**：上传一个多页 PDF → 右侧预览自动渲染第一页，可翻页
3. **上传图片**：上传 JPG → 后端调用 `convert_image_to_pdf` → 转 PDF → 渲染预览
4. **切换纸张**：选择 A3 → 预览容器变扁（aspect-ratio 更新）
5. **切换方向**：点"横向"按钮 → 预览容器旋转，且左侧单选按钮同步
6. **失败降级**：断网后上传 → 显示"PDF 渲染失败，不影响打印"提示

---

## 📚 移植参考对照表

| cups-web (Vue + Go) | cups-web-print (原生 JS + Flask) |
| --- | --- |
| `frontend/src/components/print/PdfCanvas.vue` | `static/print-preview.js`（renderPage/renderPdf） |
| `frontend/src/components/print/PrintPreview.vue` | `static/print-preview.js`（PrintPreview 类） |
| `frontend/src/views/PrintView.vue` | `templates/index.html`（右侧预览卡片 + JS init） |
| `cmd/server/watermark.go` | `static/print-preview.js`（drawWatermark canvas API） |
| `cmd/server/convert_handler.go` | `preview_routes.py`（api_preview_pdf） |
| `cmd/server/pdf_utils.go` | 复用 cups-web-print 现有的 `convert_image_to_pdf` / `convert_to_pdf` |
| Vue `pdfjs-dist` import | CDN `<script>` 全局挂载 |

---

**维护者**：本移植保留了 cups-web-print 的简洁部署体验（单 Flask 应用，无 Node 构建步骤），同时获得现代化 PDF 预览能力。