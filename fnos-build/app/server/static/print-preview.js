/**
 * Print Preview Module
 * 移植自 hanxi/cups-web 的 PdfCanvas.vue + PrintPreview.vue 思路，
 * 适配 cups-web-print 的 Flask + 原生 JS + Tailwind CSS 环境。
 *
 * 用法：
 *   PrintPreview.init({ container, fileInput, paperSelect, orientSelect, ... })
 *
 * 功能：
 *   - 用户上传文件 → 推送到 /api/preview/file 拿到 token（或 /api/preview/by-name 复用缓存）
 *   - 调用 /api/preview/pdf/<token> 取 PDF 流 → 用 PDF.js 渲染
 *   - 多页横向滚动（左右滑动 / ← → 键 / 触屏滑动）查看
 *   - 纸张尺寸/方向/色彩/缩放/页范围变化时，预览实时响应
 *   - 依赖 PDF.js（通过 CDN 加载）
 */
(function (global) {
  'use strict';

  // PDF.js 通过 CDN 加载（legacy build，兼容更多浏览器）
  const PDFJS_URL = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js';
  const PDFJS_WORKER_URL = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';

  let pdfjsLib = null;
  let pdfjsLoading = null;

  function loadPdfJs() {
    if (pdfjsLib) return Promise.resolve(pdfjsLib);
    if (pdfjsLoading) return pdfjsLoading;
    pdfjsLoading = new Promise((resolve, reject) => {
      if (global.pdfjsLib) {
        pdfjsLib = global.pdfjsLib;
        pdfjsLib.GlobalWorkerOptions.workerSrc = PDFJS_WORKER_URL;
        resolve(pdfjsLib);
        return;
      }
      const script = document.createElement('script');
      script.src = PDFJS_URL;
      script.async = true;
      script.onload = () => {
        pdfjsLib = global.pdfjsLib;
        if (!pdfjsLib) {
          reject(new Error('PDF.js 加载失败（全局对象未挂载）'));
          return;
        }
        pdfjsLib.GlobalWorkerOptions.workerSrc = PDFJS_WORKER_URL;
        resolve(pdfjsLib);
      };
      script.onerror = () => reject(new Error('PDF.js 脚本加载失败，请检查网络'));
      document.head.appendChild(script);
    });
    return pdfjsLoading;
  }

  // ─── 纸张尺寸映射（单位 mm） ─────────────────────────────────────────────
  const PAPER_DIMENSIONS = {
    A5:      { w: 148, h: 210 },
    A4:      { w: 210, h: 297 },
    A3:      { w: 297, h: 420 },
    B5:      { w: 176, h: 250 },
    Letter:  { w: 216, h: 279 },
    Legal:   { w: 216, h: 356 },
    '5inch': { w: 89,  h: 127 },
    '6inch': { w: 102, h: 152 },
    '7inch': { w: 127, h: 178 },
    '8inch': { w: 152, h: 203 },
    '10inch':{ w: 203, h: 254 },
  };

  function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  // ─── 主对象 ──────────────────────────────────────────────────────────────
  function PrintPreview(opts) {
    this.containerId = opts.containerId;
    this.fileInput = opts.fileInput;
    this.paperSelect = opts.paperSelect;
    this.orientSelect = opts.orientSelect;
    this.watermarkInput = opts.watermarkInput; // 可选
    this.onCanPrintChange = opts.onCanPrintChange || function () {};

    this.currentToken = null;
    this.currentFilename = null;
    this.pdfDoc = null;
    this.currentPage = 1;
    this.totalPages = 0;
    this.renderTask = null;
    this.requestToken = 0;
    this.resizeObserver = null;
    this._keydownHandler = null;
  }

  // 初始化：注入 DOM 结构
  PrintPreview.prototype.init = function () {
    const container = document.getElementById(this.containerId);
    if (!container) {
      console.error(`PrintPreview: 容器 #${this.containerId} 不存在`);
      return;
    }
    this.container = container;

    container.innerHTML = `
      <div class="bg-gray-100 rounded-lg p-3 sm:p-4">
        <div class="preview-wrap relative">
          <div class="preview-viewport relative overflow-hidden" style="touch-action: pan-y;">
            <!-- 页 strip：每页一个 panel，transform 平移切换 -->
            <div class="preview-strip flex transition-transform duration-300 ease-out" style="will-change: transform;"></div>

            <!-- 空状态 / 加载中 浮层（不受 strip 重建影响） -->
            <div class="preview-empty absolute inset-0 flex items-center justify-center text-gray-400 text-sm bg-gray-100 z-30">
              上传文件后显示预览
            </div>
            <div class="preview-loading absolute inset-0 hidden items-center justify-center bg-white/80 z-40">
              <svg class="animate-spin h-6 w-6 text-blue-500" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
              </svg>
            </div>

            <!-- 打印设置徽章（当前页右上角） -->
            <div class="preview-badges absolute top-2 left-2 flex flex-col gap-1 z-20 pointer-events-none"></div>

            <!-- 左右翻页按钮 -->
            <button type="button" class="preview-nav prev absolute left-0 top-1/2 -translate-y-1/2 bg-white/85 hover:bg-white text-gray-700 hover:text-blue-600 rounded-r-lg p-2 shadow-md disabled:opacity-25 disabled:cursor-not-allowed z-20 transition-colors" title="上一页 (←)">
              <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5">
                <path stroke-linecap="round" stroke-linejoin="round" d="M15 19l-7-7 7-7"/>
              </svg>
            </button>
            <button type="button" class="preview-nav next absolute right-0 top-1/2 -translate-y-1/2 bg-white/85 hover:bg-white text-gray-700 hover:text-blue-600 rounded-l-lg p-2 shadow-md disabled:opacity-25 disabled:cursor-not-allowed z-20 transition-colors" title="下一页 (→)">
              <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5">
                <path stroke-linecap="round" stroke-linejoin="round" d="M9 5l7 7-7 7"/>
              </svg>
            </button>
          </div>

          <p class="preview-error mt-2 text-center text-xs text-red-500 hidden"></p>
        </div>
        <!-- 页码指示器 -->
        <div class="preview-pager mt-2 flex items-center justify-center gap-2 text-xs text-gray-600">
          <span class="preview-page-indicator">上传文件后显示</span>
        </div>
      </div>
    `;

    this.viewportEl = container.querySelector('.preview-viewport');
    this.stripEl = container.querySelector('.preview-strip');
    this.emptyEl = container.querySelector('.preview-empty');
    this.loadingEl = container.querySelector('.preview-loading');
    this.badgesEl = container.querySelector('.preview-badges');
    this.navPrevBtn = container.querySelector('.preview-nav.prev');
    this.navNextBtn = container.querySelector('.preview-nav.next');
    this.errorEl = container.querySelector('.preview-error');
    this.pageIndicatorEl = container.querySelector('.preview-page-indicator');

    // 空状态默认显示
    this.showEmpty();

    // 事件
    if (this.navPrevBtn) this.navPrevBtn.addEventListener('click', () => this.prevPage());
    if (this.navNextBtn) this.navNextBtn.addEventListener('click', () => this.nextPage());

    // 键盘 ← →
    this._keydownHandler = (e) => {
      if (!this.pdfDoc) return;
      const tag = e.target && e.target.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || e.target && e.target.isContentEditable) return;
      if (e.key === 'ArrowLeft') { e.preventDefault(); this.prevPage(); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); this.nextPage(); }
    };
    document.addEventListener('keydown', this._keydownHandler);

    // 触屏滑动
    this._touchStartX = null;
    if (this.viewportEl) {
      this.viewportEl.addEventListener('touchstart', (e) => {
        this._touchStartX = e.changedTouches[0].screenX;
      }, { passive: true });
      this.viewportEl.addEventListener('touchend', (e) => {
        if (this._touchStartX === null) return;
        const dx = e.changedTouches[0].screenX - this._touchStartX;
        this._touchStartX = null;
        if (Math.abs(dx) > 45) {
          if (dx < 0) this.nextPage();
          else this.prevPage();
        }
      }, { passive: true });
    }

    // 上传 input
    if (this.fileInput) {
      this.fileInput.addEventListener('change', (e) => this.handleFile(e.target.files[0]));
    }

    // 打印设置联动
    const bindChange = (els, fn) => {
      (els || []).forEach(r => r.addEventListener('change', fn));
    };
    bindChange(document.querySelectorAll('input[name="colorMode"]'), () => this.applyPrintEffects());
    bindChange(document.querySelectorAll('input[name="duplex"]'), () => this.applyPrintEffects());
    bindChange(document.querySelectorAll('input[name="orientation"]'), () => this.applyPrintEffects());
    const paperEl = this.paperSelect;
    if (paperEl) paperEl.addEventListener('change', () => this.updatePaperStyle());
    if (this.orientSelect && this.orientSelect.addEventListener) {
      // orientSelectShim 的 addEventListener 是空函数，方向按钮自行调用 updatePaperStyle
    }
    const copiesEl = document.getElementById('copies');
    if (copiesEl) copiesEl.addEventListener('input', () => this.applyPrintEffects());
    const scalingEl = document.getElementById('printScaling');
    if (scalingEl) scalingEl.addEventListener('change', () => this.applyPrintEffects());
    const mirrorEl = document.getElementById('mirrorPrint');
    if (mirrorEl) mirrorEl.addEventListener('change', () => this.applyPrintEffects());
    const pageRangeEl = document.getElementById('pageRange');
    if (pageRangeEl) pageRangeEl.addEventListener('input', () => this.applyPageRange());

    // 容器尺寸变化时重渲染当前页
    // 但要避开 renderPage 内部改 paper.style.width/height 触发的 resize（否则循环触发）
    if (global.ResizeObserver) {
      this._suppressResize = false;
      this.resizeObserver = new ResizeObserver(() => {
        if (this._suppressResize) return;
        if (this.pdfDoc) this.renderPage(this.currentPage);
      });
      this.resizeObserver.observe(this.viewportEl);
    }

    // 应用初始纸张比例
    this.applyPaperAspect();
    this.applyPrintEffects();
  };

  // ── DOM 辅助 ─────────────────────────────────────────────────────────────
  PrintPreview.prototype.currentPanel = function () {
    if (!this.stripEl) return null;
    return this.stripEl.querySelector(`.preview-page-panel[data-page="${this.currentPage}"]`);
  };

  // ── 纸张比例 ─────────────────────────────────────────────────────────────
  // 返回 { ratio, cssW, cssH, isLandscape }：
  //   - ratio = 容器宽高比（aspect-ratio CSS 值）
  //   - cssW/cssH = 容器期望的 CSS 像素尺寸（用于 PDF.js 算 scale）
  //   - isLandscape 仅供打印徽章显示
  // 优先使用当前页面的真实尺寸（不同 PDF 页面横纵可能不同，按页面动态调整容器）；
  // 渲染前还没拿到页面时，回退到用户选择的纸张+方向。
  PrintPreview.prototype.getPaperAspect = function (page) {
    let ratio, isLandscape = false;
    if (page && typeof page.getViewport === 'function') {
      try {
        const v = page.getViewport({ scale: 1 });
        ratio = v.width / v.height;
        isLandscape = v.width > v.height;
      } catch (_) {}
    }
    if (!ratio) {
      const paper = this.paperSelect ? this.paperSelect.value : 'A4';
      const orient = this.orientSelect ? this.orientSelect.value : 'portrait';
      const dim = PAPER_DIMENSIONS[paper] || PAPER_DIMENSIONS.A4;
      const w = orient === 'landscape' ? dim.h : dim.w;
      const h = orient === 'landscape' ? dim.w : dim.h;
      ratio = w / h;
      isLandscape = orient === 'landscape';
    }
    return { ratio, isLandscape };
  };

  // 估算 paper 容器当前的 CSS 尺寸（用于 PDF.js 算 scale，避免被 max-w-full/max-h-full 截断）
  //
  // 关键：必须用 **viewport** 的 clientWidth 作为可用宽度（不是 strip 的 clientWidth——
  // strip 是 display:flex，所有 panel 横向并排，clientWidth = 所有面板宽之和，
  // 远大于视口，会导致横向页面被严重压缩）。
  PrintPreview.prototype.measurePaperCssSize = function (paperEl, ratio) {
    if (!paperEl || !ratio) return { w: 0, h: 0 };
    // 1) 可用宽度 = viewport 可见宽（不是 strip）
    const viewport = this.viewportEl || (paperEl.closest('.preview-viewport'));
    const viewportW = viewport ? viewport.clientWidth : (paperEl.parentElement ? paperEl.parentElement.clientWidth : 480);
    const padding = 8; // panel 的 px-1 + 容器边框
    const usableW = Math.max(120, viewportW - padding);
    const maxW = 480;
    const cssW = Math.max(120, Math.min(maxW, usableW));
    const cssH = cssW / ratio;
    return { w: cssW, h: cssH };
  };

  PrintPreview.prototype.applyPaperAspect = function (ratio) {
    if (typeof ratio !== 'number' || !isFinite(ratio) || ratio <= 0) {
      // 回退到默认 A4 portrait
      const dim = PAPER_DIMENSIONS.A4;
      ratio = dim.w / dim.h;
    }
    if (this.stripEl) {
      this.stripEl.querySelectorAll('.preview-paper').forEach(p => {
        p.style.aspectRatio = `${ratio}`;
      });
    }
  };

  PrintPreview.prototype.updatePaperStyle = function () {
    // 用户切换纸张/方向时清掉旧页面的尺寸标记，重新按默认 A4 比例呈现
    if (this._pageAspectByPage) {
      this._pageAspectByPage = null;
    }
    this.applyPaperAspect();
    if (this.pdfDoc) this.renderPage(this.currentPage);
    this.applyPrintEffects();
  };

  // ── 打印设置视觉反馈 ─────────────────────────────────────────────────────
  PrintPreview.prototype.applyPrintEffects = function () {
    if (!this.stripEl) return;

    const colorMode = ((document.querySelector('input[name="colorMode"]:checked') || {}).value) || 'color';
    const filter = colorMode === 'mono' ? 'grayscale(1)' : '';
    this.stripEl.querySelectorAll('.preview-canvas').forEach(c => { c.style.filter = filter; });

    // 徽章
    if (this.badgesEl) {
      const badges = [];
      const copiesEl = document.getElementById('copies');
      const copies = copiesEl ? parseInt(copiesEl.value, 10) || 1 : 1;
      if (copies > 1) badges.push(`<span class="px-2 py-0.5 bg-blue-600/80 text-white text-xs rounded shadow">× ${copies} 份</span>`);

      const duplex = ((document.querySelector('input[name="duplex"]:checked') || {}).value);
      if (duplex && duplex !== 'one-sided') {
        const label = duplex === 'two-sided-long-edge' ? '双面(长边)' : '双面(短边)';
        badges.push(`<span class="px-2 py-0.5 bg-purple-600/80 text-white text-xs rounded shadow">${label}</span>`);
      }

      const orient = ((document.querySelector('input[name="orientation"]:checked') || {}).value);
      if (orient === 'landscape') badges.push(`<span class="px-2 py-0.5 bg-orange-600/80 text-white text-xs rounded shadow">横向</span>`);
      if (colorMode === 'mono') badges.push(`<span class="px-2 py-0.5 bg-gray-700/80 text-white text-xs rounded shadow">黑白</span>`);

      const mirrorEl = document.getElementById('mirrorPrint');
      if (mirrorEl && mirrorEl.checked) badges.push(`<span class="px-2 py-0.5 bg-pink-600/80 text-white text-xs rounded shadow">镜像</span>`);

      const scalingEl = document.getElementById('printScaling');
      const scaling = scalingEl ? scalingEl.value : 'fit';
      if (scaling === 'fill') badges.push(`<span class="px-2 py-0.5 bg-cyan-600/80 text-white text-xs rounded shadow">填满纸张</span>`);
      else if (scaling === 'none') badges.push(`<span class="px-2 py-0.5 bg-gray-500/80 text-white text-xs rounded shadow">原始大小</span>`);

      const pageRangeEl = document.getElementById('pageRange');
      if (pageRangeEl && pageRangeEl.value.trim()) {
        badges.push(`<span class="px-2 py-0.5 bg-amber-600/80 text-white text-xs rounded shadow">范围: ${escapeHtml(pageRangeEl.value.trim())}</span>`);
      }

      this.badgesEl.innerHTML = badges.join('');
    }
  };

  // ── 页面范围 ─────────────────────────────────────────────────────────────
  PrintPreview.prototype.parsePageRange = function (rangeStr, totalPages) {
    if (!rangeStr || !rangeStr.trim()) return null;
    const pages = new Set();
    const parts = rangeStr.trim().split(/\s+/);
    for (const part of parts) {
      if (part.includes('-')) {
        const [a, b] = part.split('-').map(n => parseInt(n, 10));
        if (isNaN(a) || isNaN(b)) continue;
        const lo = Math.min(a, b);
        const hi = Math.max(a, b);
        for (let i = lo; i <= Math.min(hi, totalPages); i++) if (i >= 1) pages.add(i);
      } else {
        const n = parseInt(part, 10);
        if (!isNaN(n) && n >= 1 && n <= totalPages) pages.add(n);
      }
    }
    return Array.from(pages).sort((a, b) => a - b);
  };

  PrintPreview.prototype.applyPageRange = function () {
    if (!this.pdfDoc) return;
    const rangeEl = document.getElementById('pageRange');
    if (!rangeEl) return;
    const raw = rangeEl.value.trim();
    if (!raw) { this.updatePager(); this.applyPrintEffects(); return; }
    const pages = this.parsePageRange(raw, this.totalPages);
    if (!pages || pages.length === 0) { this.updatePager(); this.applyPrintEffects(); return; }
    if (!pages.includes(this.currentPage)) {
      this.scrollToPage(pages[0]);
    }
    this.updatePager();
    this.applyPrintEffects();
  };

  // ── 状态显示 ─────────────────────────────────────────────────────────────
  PrintPreview.prototype.showEmpty = function () {
    if (this.emptyEl) this.emptyEl.classList.remove('hidden');
    if (this.loadingEl) this.loadingEl.classList.add('hidden');
    if (this.stripEl) this.stripEl.innerHTML = '';
    if (this.pageIndicatorEl) this.pageIndicatorEl.textContent = '上传文件后显示';
    if (this.navPrevBtn) this.navPrevBtn.disabled = true;
    if (this.navNextBtn) this.navNextBtn.disabled = true;
  };

  PrintPreview.prototype.showLoading = function (show) {
    if (this.loadingEl) {
      this.loadingEl.classList.toggle('hidden', !show);
      this.loadingEl.classList.toggle('flex', show);
    }
  };

  PrintPreview.prototype.showError = function (msg) {
    if (!this.errorEl) return;
    if (!msg) { this.errorEl.classList.add('hidden'); this.errorEl.textContent = ''; return; }
    this.errorEl.textContent = msg;
    this.errorEl.classList.remove('hidden');
  };

  PrintPreview.prototype.cleanupPdfDoc = function () {
    if (this.renderTask) { try { this.renderTask.cancel(); } catch (_) {} this.renderTask = null; }
    if (this.pdfDoc) { try { this.pdfDoc.destroy(); } catch (_) {} this.pdfDoc = null; }
    this.totalPages = 0;
    this.currentPage = 1;
    this.showEmpty();
    this.applyPrintEffects();
  };

  // ── 加载文件 ─────────────────────────────────────────────────────────────
  // 从 File 对象（本地选择/拖拽 → 上传）
  PrintPreview.prototype.handleFile = async function (file) {
    if (!file) {
      this.cleanupPdfDoc();
      this.onCanPrintChange(false);
      return;
    }
    this.cleanupPdfDoc();
    if (this.emptyEl) this.emptyEl.classList.add('hidden');
    this.showLoading(true);
    this.showError(null);
    try {
      const form = new FormData();
      form.append('file', file);
      const resp = await fetch('/api/preview/file', { method: 'POST', body: form });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.error || `上传预览失败 (${resp.status})`);
      }
      const data = await resp.json();
      this.currentToken = data.token;
      this.currentFilename = data.name;
      this.onCanPrintChange(true);

      // 图片走 /api/preview/raw/<token> 直送原图（无需 PDF.js、pdftoppm）
      if (file.type && file.type.indexOf('image/') === 0) {
        await this.renderImage(`/api/preview/raw/${this.currentToken}`, file.name);
        return;
      }
      await this.renderPdf(`/api/preview/pdf/${this.currentToken}`);
    } catch (e) {
      console.error('预览加载失败：', e);
      this.showError(`预览加载失败：${e.message}（不影响打印，可直接提交）`);
      this.showLoading(false);
      this.showEmpty();
    }
  };

  // 根据已上传文件名（复用 previews 缓存 PDF，不重新 multipart 上传）
  PrintPreview.prototype.handleFileByName = async function (filename) {
    if (!filename) {
      this.cleanupPdfDoc();
      this.onCanPrintChange(false);
      return;
    }
    this.cleanupPdfDoc();
    if (this.emptyEl) this.emptyEl.classList.add('hidden');
    this.showLoading(true);
    this.showError(null);
    try {
      const resp = await fetch(`/api/preview/by-name?filename=${encodeURIComponent(filename)}`);
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.error || `获取预览失败 (${resp.status})`);
      }
      const data = await resp.json();
      this.currentToken = data.token;
      this.currentFilename = data.name;
      this.onCanPrintChange(true);

      // 图片：根据扩展名判定走 raw 路径
      const lc = (data.name || '').toLowerCase();
      const isImage = /\.(jpe?g|png|gif|bmp|svg)$/.test(lc);
      if (isImage) {
        await this.renderImage(`/api/preview/raw/${this.currentToken}`, data.name);
        return;
      }
      await this.renderPdf(`/api/preview/pdf/${this.currentToken}`);
    } catch (e) {
      console.error('预览加载失败：', e);
      this.showError(`预览加载失败：${e.message}（不影响打印，可直接提交）`);
      this.showLoading(false);
      this.showEmpty();
    }
  };

  // ── 图片渲染（单页直送原图，跳过 PDF.js 与 pdftoppm） ───────────────────────
  PrintPreview.prototype.renderImage = async function (url, name) {
    const myToken = ++this.requestToken;
    try {
      // 先取 HEAD/Range 一下确认资源可访问（同时触发浏览器缓存）
      // 然后用 Image 对象拿到原始宽高（保留 EXIF orientation 由浏览器负责）
      const img = new Image();
      img.crossOrigin = 'anonymous';
      img.decoding = 'async';

      const loaded = new Promise((resolve, reject) => {
        img.onload = () => resolve(img);
        img.onerror = () => reject(new Error('图片加载失败'));
      });
      img.src = url;
      await loaded;
      if (myToken !== this.requestToken) return;

      // 单页"虚拟 PDF"
      this.pdfDoc = null;
      this.totalPages = 1;
      this.currentPage = 1;
      this.currentImage = img;

      // 按图片真实宽高比建一个 panel（复用 paper 容器，避免引入新 DOM）
      if (this.stripEl) {
        this.stripEl.innerHTML = '';
        const ratio = (img.naturalWidth || 1) / (img.naturalHeight || 1);
        const panel = document.createElement('div');
        panel.className = 'preview-page-panel flex-shrink-0 w-full px-1';
        panel.setAttribute('data-page', 1);
        panel.innerHTML = `
          <div class="preview-paper bg-white shadow-lg border border-gray-200 mx-auto relative"
               style="aspect-ratio: ${ratio}; width: 100%; max-width: 480px;">
            <img class="preview-image absolute inset-0 m-auto max-w-full max-h-full" alt="${(name || '').replace(/"/g, '&quot;')}" />
          </div>
        `;
        this.stripEl.appendChild(panel);
        const imgEl = panel.querySelector('.preview-image');
        if (imgEl) imgEl.src = url;
        this.stripEl.style.transform = 'translateX(0%)';
      }

      this.updatePager();
      this.applyPrintEffects();
      this.showLoading(false);
    } catch (e) {
      if (myToken !== this.requestToken) return;
      console.error('图片预览失败：', e);
      this.showError(`图片预览失败：${e.message}（不影响打印，可直接提交）`);
      this.showLoading(false);
      this.showEmpty();
    }
  };

  // ── PDF 渲染 ─────────────────────────────────────────────────────────────
  PrintPreview.prototype.renderPdf = async function (url) {
    const myToken = ++this.requestToken;
    let pdfjs;
    try {
      pdfjs = await loadPdfJs();
    } catch (e) {
      this.showError(`PDF.js 加载失败：${e.message}`);
      this.showLoading(false);
      return;
    }
    if (myToken !== this.requestToken) return;

    try {
      const doc = await pdfjs.getDocument({
        url,
        cMapUrl: 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/cmaps/',
        cMapPacked: true,
        isEvalSupported: false,
      }).promise;

      if (myToken !== this.requestToken) { try { doc.destroy(); } catch (_) {} return; }

      this.pdfDoc = doc;
      this.totalPages = doc.numPages;
      this.currentPage = 1;
      this.buildPagePanels();
      this.updatePager();
      // 首次加载：少页 PDF（<=10）直接渲染所有页，多页只懒加载前后各一页（避免卡顿）
      if (this.totalPages <= 10) {
        for (let i = 1; i <= this.totalPages; i++) {
          this.renderPage(i, i !== 1);  // 第 1 页同步显示，其余 preload
        }
        // 应用初始纸张比例（之前 renderPage 已设过 paper aspect-ratio，这里统一徽章）
        this.applyPrintEffects();
      } else {
        // 多页懒加载：scrollToPage 会渲染当前页 + 前后各一页
        this.scrollToPage(1);
      }
      if (myToken === this.requestToken) {
        this.showLoading(false);
        this.applyPrintEffects();
      }
    } catch (e) {
      if (myToken !== this.requestToken) return;
      console.error('PDF 渲染失败：', e);
      this.showError(`PDF 渲染失败：${e.message}（不影响打印，可直接提交）`);
      this.showLoading(false);
    }
  };

  // 根据 totalPages 构建横向 strip 中的每一页 panel
  // 设计：
  //   - panel: inline-block（让 width 由 paper 内容撑开，避免 flex stretch）
  //   - strip: text-align: center 居中单个 panel + white-space: nowrap 让多 panel 横向排列
  //   - paper: display: inline-block（让 strip 的 text-align:center 居中它）
  //   - paper 宽高由 renderPage 直接 style.width/height 设（不依赖 aspect-ratio，
  //     避免 flex 嵌套下父容器宽度被干扰）
  PrintPreview.prototype.buildPagePanels = function () {
    if (!this.stripEl) return;
    this.stripEl.innerHTML = '';
    // strip 用 white-space:nowrap 让所有 panel 横向排列；
    // text-align:center 让只有一页时 paper 居中；多页时 transform 滑动不被居中影响
    this.stripEl.style.whiteSpace = 'nowrap';
    this.stripEl.style.textAlign = 'center';

    const defaultDim = PAPER_DIMENSIONS.A4;
    const defaultRatio = defaultDim.w / defaultDim.h;
    for (let i = 1; i <= this.totalPages; i++) {
      const panel = document.createElement('div');
      panel.className = 'preview-page-panel';
      // panel 用 inline-block（不 flex）；外边距控制 panel 间距离
      panel.style.cssText = 'display:inline-block;vertical-align:top;padding:0 4px;white-space:normal;';
      panel.setAttribute('data-page', i);
      // paper 用 inline-block + display 让 strip 的 text-align:center 生效（自动居中）
      panel.innerHTML = `
        <div class="preview-paper bg-white shadow-lg border border-gray-200 relative"
             style="display:inline-block;width:240px;height:${240/defaultRatio}px;position:relative;overflow:hidden;">
          <canvas class="preview-canvas" style="position:absolute;left:0;top:0;width:100%;height:100%;display:block;"></canvas>
          <canvas class="preview-watermark" style="position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none;display:block;"></canvas>
        </div>
      `;
      this.stripEl.appendChild(panel);
    }
    this.stripEl.style.transform = 'translateX(0%)';
    this.applyPrintEffects();
  };

  // 切换页（transform 平移 + 懒加载当前页）
  // strip 用 white-space:nowrap 排列所有 panel，translateX 用 px 而非 %（避免 % 相对
  // strip 自身宽度的总宽移动，与单页 panel 宽不匹配）。
  PrintPreview.prototype.scrollToPage = function (pageNum) {
    if (!this.stripEl) return;
    if (pageNum < 1 || pageNum > this.totalPages) return;
    // 累加前 (pageNum - 1) 个 panel 的实际宽度（含 padding），作为左移距离
    let offset = 0;
    for (let i = 1; i < pageNum; i++) {
      const p = this.stripEl.querySelector(`.preview-page-panel[data-page="${i}"]`);
      if (p) offset -= p.offsetWidth;
    }
    this.stripEl.style.transform = `translateX(${offset}px)`;
    this.currentPage = pageNum;
    this.renderPage(pageNum);
    if (pageNum > 1) this.renderPage(pageNum - 1, true);
    if (pageNum < this.totalPages) this.renderPage(pageNum + 1, true);
    this.updatePager();
  };

  PrintPreview.prototype.renderPage = async function (pageNum, isPreload) {
    if (!this.pdfDoc || !this.stripEl) return;
    const myToken = this.requestToken;
    const panel = this.stripEl.querySelector(`.preview-page-panel[data-page="${pageNum}"]`);
    if (!panel) return;
    const canvasEl = panel.querySelector('.preview-canvas');
    const paperEl = panel.querySelector('.preview-paper');
    if (!canvasEl || !paperEl) return;

    // 取消该 canvas 上正在跑的旧 render task（避免 PDF.js "multiple render()" 错误）
    if (canvasEl._pdfRenderTask) {
      try { canvasEl._pdfRenderTask.cancel(); } catch (_) {}
      canvasEl._pdfRenderTask = null;
    }

    // 抑制 ResizeObserver 在 renderPage 改 paper 尺寸时回调（避免循环触发）
    this._suppressResize = true;

    try {
      const page = await this.pdfDoc.getPage(pageNum);
      if (myToken !== this.requestToken) return;

      const dpr = global.devicePixelRatio || 1;

      // 1) 用页面真实宽高比。aspect-ratio 只是占位 fallback，**最终宽高由 JS 直接设**。
      const aspect = this.getPaperAspect(page);
      const ratio = aspect.ratio;

      // 2) 计算 paper CSS 像素（用 viewport clientWidth 作为可用宽度）
      const viewport = this.viewportEl || (paperEl.closest('.preview-viewport'));
      const viewportW = viewport ? viewport.clientWidth : 480;
      const padding = 8;
      const usableW = Math.max(120, viewportW - padding);
      const maxW = 480;
      const cssW = Math.max(120, Math.min(maxW, usableW));
      const cssH = cssW / ratio;

      if (cssW <= 0 || cssH <= 0) return;

      // 3) 直接设 paper 宽高，**不依赖 aspect-ratio**
      paperEl.style.width = cssW + 'px';
      paperEl.style.height = cssH + 'px';
      paperEl.style.aspectRatio = '';

      // 4) PDF.js scale
      const baseViewport = page.getViewport({ scale: 1 });
      const baseScale = cssW / baseViewport.width;
      const scaledViewport = page.getViewport({ scale: baseScale * dpr });

      // 5) canvas 物理像素 = scaledViewport；CSS 像素 = cssW × cssH，与 paper 严格一致
      const ctx = canvasEl.getContext('2d');
      canvasEl.width = scaledViewport.width;
      canvasEl.height = scaledViewport.height;
      canvasEl.style.width = cssW + 'px';
      canvasEl.style.height = cssH + 'px';

      const colorMode = ((document.querySelector('input[name="colorMode"]:checked') || {}).value);
      canvasEl.style.filter = colorMode === 'mono' ? 'grayscale(1)' : '';

      const task = page.render({ canvasContext: ctx, viewport: scaledViewport });
      canvasEl._pdfRenderTask = task;
      await task.promise;
      if (myToken !== this.requestToken) return;
      this.drawWatermarkOnPanel(panel, canvasEl);
    } catch (e) {
      if (e && e.name === 'RenderingCancelledException') return;
      if (!isPreload) console.error('渲染页面失败：', e);
    } finally {
      // 解抑制：等下一帧再放开，避免 ResizeObserver 同步回调
      requestAnimationFrame(() => { this._suppressResize = false; });
    }
  };

  // 给单个 panel 画水印（可选功能；无 watermarkInput 时为空操作）
  PrintPreview.prototype.drawWatermarkOnPanel = function (panel, canvasEl) {
    const text = this.watermarkInput ? this.watermarkInput.value : '';
    if (!text) return;
    const wc = panel.querySelector('.preview-watermark');
    if (!wc) return;
    const cssW = parseFloat(canvasEl.style.width);
    const cssH = parseFloat(canvasEl.style.height);
    if (!cssW || !cssH) return;
    const dpr = global.devicePixelRatio || 1;
    wc.width = cssW * dpr;
    wc.height = cssH * dpr;
    wc.style.width = cssW + 'px';
    wc.style.height = cssH + 'px';
    const ctx = wc.getContext('2d');
    ctx.clearRect(0, 0, wc.width, wc.height);
    ctx.save();
    ctx.scale(dpr, dpr);
    ctx.globalAlpha = 0.15;
    ctx.fillStyle = '#888';
    const fontSize = Math.max(12, cssW * 0.05);
    ctx.font = `${fontSize}px sans-serif`;
    const textWidth = ctx.measureText(text).width;
    const stepX = textWidth + 30;
    const stepY = fontSize * 2.5;
    const diag = Math.sqrt(cssW * cssW + cssH * cssH);
    ctx.translate(cssW / 2, cssH / 2);
    ctx.rotate(-45 * Math.PI / 180);
    for (let y = -diag; y < diag; y += stepY) {
      for (let x = -diag; x < diag; x += stepX) {
        ctx.fillText(text, x, y);
      }
    }
    ctx.restore();
  };

  // 兼容旧 API：drawWatermark（整体调用时画当前页）
  PrintPreview.prototype.drawWatermark = function () {
    if (this.pdfDoc) this.renderPage(this.currentPage);
  };

  // ── 翻页 ─────────────────────────────────────────────────────────────────
  PrintPreview.prototype.prevPage = function () {
    if (this.currentPage <= 1) return;
    this.scrollToPage(this.currentPage - 1);
  };

  PrintPreview.prototype.nextPage = function () {
    if (this.currentPage >= this.totalPages) return;
    this.scrollToPage(this.currentPage + 1);
  };

  PrintPreview.prototype.updatePager = function () {
    if (this.pageIndicatorEl) {
      this.pageIndicatorEl.textContent = this.totalPages > 0 ? `第 ${this.currentPage} / ${this.totalPages} 页` : '上传文件后显示';
    }
    if (this.navPrevBtn) this.navPrevBtn.disabled = this.currentPage <= 1 || !this.pdfDoc;
    if (this.navNextBtn) this.navNextBtn.disabled = this.currentPage >= this.totalPages || !this.pdfDoc;
  };

  PrintPreview.prototype.updateNavButtons = function () {
    this.updatePager();
  };

  // ── 销毁 ─────────────────────────────────────────────────────────────────
  PrintPreview.prototype.destroy = function () {
    this.cleanupPdfDoc();
    if (this.resizeObserver) this.resizeObserver.disconnect();
    if (this._keydownHandler) document.removeEventListener('keydown', this._keydownHandler);
    if (this.currentToken) {
      fetch(`/api/preview/file?token=${this.currentToken}`, { method: 'DELETE' }).catch(() => {});
    }
  };

  global.PrintPreview = PrintPreview;
})(window);
