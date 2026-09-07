# cups-web-print 二次开发指南（含侧边栏预览）

本指南说明如何配合 DeepSeek Harness（DSH）和 `dsh-web-preview-panel` 插件进行 cups-web-print 的二次开发。

---

## 🎯 已部署环境

| 组件 | 状态 | 路径 |
| --- | --- | --- |
| **DeepSeek Harness** | ✅ 运行中 | `http://127.0.0.1:2299/` |
| **dsh-web-preview-panel** | ✅ 已安装 v0.2.4 | 插件市场搜索安装 |
| **cups-web-print Flask** | ✅ 运行中 | `http://127.0.0.1:5000/zh` |
| **dev-server.sh** | ✅ 已创建 | `cups-web-print/dev-server.sh` |

---

## 🚀 完整开发流程

### 1. 启动 cups-web-print 开发服务器

```bash
cd /vol1/@appdata/deepseek.harness/home/main/cups-web-print

# 三种命令
./dev-server.sh start        # 启动（默认 5000 端口）
./dev-server.sh stop         # 停止
./dev-server.sh restart      # 重启（修改代码后用）
./dev-server.sh status       # 查看状态
./dev-server.sh logs         # 实时日志（Ctrl+C 退出）
```

### 2. 在 DSH 中打开预览面板

1. 浏览器访问 `http://127.0.0.1:2299/`
2. 选择一个 Workspace（可以是任意已挂载的目录）
3. 在右上角找到 ▶ 按钮（**dsh-web-preview-panel** 注入的）
4. 点击后在地址栏输入：`http://localhost:5000/zh`
5. **侧边栏 iframe** 立即显示 cups-web-print 页面

### 3. 修改代码并预览

```bash
# 编辑代码
vim app.py
vim templates/index.html

# 重启 Flask（让代码生效）
./dev-server.sh restart

# 浏览器中刷新 iframe（或按 Ctrl+Shift+R 强刷）
```

**注意**：Flask 没有热重载，每次改代码都要 `restart`。

### 4. 调试 cups-web-print API

```bash
# 查看新增的预览 API 状态
curl http://localhost:5000/api/preview/info

# 完整流程测试
TOKEN=$(curl -sS -X POST http://localhost:5000/api/preview/file \
  -F "file=@yourfile.pdf" | python3 -c "import json,sys; print(json.load(sys.stdin)['token'])")
echo "Token: $TOKEN"

# 在浏览器访问（PDF.js 会渲染）
# http://localhost:5000/api/preview/pdf/$TOKEN

# 清理
curl -X DELETE "http://localhost:5000/api/preview/file?token=$TOKEN"
```

---

## 🛠️ 移植内容回顾

### 后端（Flask）

| 文件 | 来源 | 功能 |
| --- | --- | --- |
| `cups-web-print/preview_routes.py` | 新增 | Flask Blueprint（端口 5000 的 `/api/preview/*`） |
| `cups-web-print/app.py` | 修改 | 注册 `preview_bp`（向后兼容） |
| `cups-web-print/templates/index.html` | 修改 | 右侧栏预览卡片 + JS 集成 |

**新增 API 端点**：
- `POST /api/preview/file` — 上传文件，返回 token
- `GET /api/preview/pdf/<token>` — 取 PDF 流
- `DELETE /api/preview/file` — 清理缓存
- `GET /api/preview/info` — 诊断信息

### 前端（原生 JS）

`cups-web-print/static/print-preview.js` 是 **移植自 cups-web 的 `PdfCanvas.vue` + `PrintPreview.vue`**：

| cups-web（Vue）） | cups-web-print（原生 JS） |
| --- | --- |
| `frontend/src/components/print/PdfCanvas.vue` | `static/print-preview.js` 中 `renderPage`/`renderPdf` |
| `frontend/src/components/print/PrintPreview.vue` | `static/print-preview.js` 中 `PrintPreview` 类 |
| `cmd/server/watermark.go` | `static/print-preview.js` 中 `drawWatermark` |
| `cmd/server/convert_handler.go` | `preview_routes.py` 中 `api_preview_pdf` |

---

## 🧩 dsh-web-preview-panel 详解

### 安装过程回顾

```bash
# 1. 安装插件到 DSH profile
cd /vol1/@appdata/deepseek.harness/dsh-data/profiles/web/
npm install dsh-web-preview-panel --save

# 2. 配置 cordis.patch.yml
cat > cordis.patch.yml <<'EOF'
- insert:
    - name: dsh-web-preview-panel
EOF

# 3. DSH HMR 自动重新加载配置（无需重启）
```

### 插件工作原理

```
┌─ DSH Web (port 2299) ─────────────────────────────────┐
│                                                        │
│  ┌──────────────────┐   ┌─────────────────────────┐  │
│  │ 对话界面          │   │ ▶ 预览面板（iframe）    │  │
│  │ （与 AI 交互）    │   │  http://localhost:5000 │  │
│  └──────────────────┘   └─────────────────────────┘  │
│         │                          ▲                  │
│         │ AI 修改代码               │ cups-web-print  │
│         ▼                          │ 提供 iframe 内容 │
└─────────┼──────────────────────────┼──────────────────┘
          │                          │
   ┌──────▼──────────┐       ┌──────┴──────────────┐
   │ 编辑器 / 终端   │       │  Flask (5000)       │
   │ （IDE）         │       │  - 原有打印 API     │
   └─────────────────┘       │  - 新增 /api/preview│
                             └─────────────────────┘
```

### 关键路径

- 插件客户端 bundle：`/vol1/@appdata/deepseek.harness/dsh-data/profiles/web/node_modules/dsh-web-preview-panel/`
- DSH 通过 `/plugins/<name>/client.js` 加载
- 配置：`/vol1/@appdata/deepseek.harness/dsh-data/profiles/web/cordis.patch.yml`

---

## 📝 常见任务速查

### 启动 / 停止一切

```bash
# 启动 cups-web-print
./dev-server.sh start

# 查看状态
./dev-server.sh status

# 停止 cups-web-print
./dev-server.sh stop

# DSH 由系统管理（不需要手动启动）
# 访问： http://127.0.0.1:2299/
```

### 修改代码后

```bash
# 修改 Python 文件
vim preview_routes.py
./dev-server.sh restart

# 修改前端 JS
vim static/print-preview.js
./dev-server.sh restart  # 即使是前端也需要，因为 Flask 重启会清缓存

# 修改 HTML 模板
vim templates/index.html
./dev-server.sh restart
```

### 查看运行日志

```bash
# cups-web-print 日志
./dev-server.sh logs

# 或
tail -f /tmp/cups-web-print-dev.log

# DSH 日志
tail -f /vol1/@appdata/deepseek.harness/harness.log
```

### 调试 API

```bash
# 诊断信息
curl http://localhost:5000/api/preview/info

# 测试 PDF 流
curl -o test.pdf http://localhost:5000/api/preview/pdf/<token>

# 测试图片转换（如果装 LibreOffice）
curl -X POST http://localhost:5000/api/preview/file -F "file=@test.png"
```

---

## ⚠️ 注意事项

1. **DSH 拦截 `/api/*`**：cups-web-print 的 `/api/*` 端点通过 DSH 反代会 404。必须**直接访问** `http://localhost:5000` 才能用 cups-web-print 的 API。
2. **PDF.js CDN**：预览功能依赖 `cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/`。如果离线部署需要下载到 `static/`。
3. **LibreOffice 未装**：图片/Office 转 PDF 会失败，但 PDF 直传不影响。
4. **端口冲突**：5000 端口被占用时用 `./dev-server.sh start 8080` 自定义。

---

## 🎁 推荐开发插件（可选）

如果想要更完整的开发体验，可以加装：

- **dsh-side-panel**（`ccq1/dsh-side-panel`）— 通用侧边栏（文件浏览+终端+Git review）
- **dsh-better-sidebar + tsonglew/dsh-media-preview** — 媒体预览
- **BrambleXu/dsh-annotate** — 浏览器元素选择 → 反馈给 AI

详细列表：<https://github.com/awesome-dsh-plugin/awesome-dsh-plugin>

---

**祝开发愉快！** 🚀