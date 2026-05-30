/**
 * AI PR Review - 前端交互逻辑
 *
 * 核心功能:
 * - 提交 PR URL 到后端 /api/review/stream (SSE 流式分析)
 * - 渐进渲染: 先出总结 → 逐个出风险 → 最后出元信息
 * - 进度条 + 实时状态文字
 * - 代码语法高亮 (highlight.js)
 * - 风险分类筛选栏 (安全/性能/逻辑/稳定性/规范)
 * - P0 风险默认展开 + 入场动画
 * - 错误重试机制
 */

const API_BASE = window.location.origin;

// ── DOM 引用 ──

const prUrlInput = document.getElementById('prUrl');
const analyzeBtn = document.getElementById('analyzeBtn');
const btnText = analyzeBtn.querySelector('.btn-text');
const btnLoading = analyzeBtn.querySelector('.btn-loading');
const statusSection = document.getElementById('statusSection');
const statusContent = document.getElementById('statusContent');
const progressSection = document.getElementById('progressSection');
const progressText = document.getElementById('progressText');
const progressBarFill = document.getElementById('progressBarFill');
const progressSubtext = document.getElementById('progressSubtext');
const resultSection = document.getElementById('resultSection');
const riskFilterBar = document.getElementById('riskFilterBar');
const riskCountBadge = document.getElementById('riskCountBadge');

// 流式分析 AbortController, 用于取消分析
let currentAbortController = null;

// 收集流式到达的风险项
let riskItems = [];
let riskIndex = 0;

// 当前活跃的筛选 (all = 显示全部)
let currentFilter = 'all';

// ── 事件绑定 ──

analyzeBtn.addEventListener('click', handleAnalyze);
prUrlInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') handleAnalyze();
});

// ── 主流程: 流式分析 ──

async function handleAnalyze() {
    const prUrl = prUrlInput.value.trim();

    // ── 输入校验 ──
    if (!prUrl) {
        showStatus('请输入 GitHub PR URL', 'error');
        return;
    }
    if (!prUrl.includes('github.com') || !prUrl.includes('/pull/')) {
        showStatus('URL 格式不正确。请使用: https://github.com/{owner}/{repo}/pull/{number}', 'error');
        return;
    }

    // ── 重置状态 ──
    setLoading(true);
    hideStatus();
    hideResult();
    resetProgress();
    riskItems = [];
    riskIndex = 0;
    currentFilter = 'all';

    // 重置筛选栏
    resetFilterBar();

    // 清空结果区域
    document.getElementById('summaryContent').innerHTML = '';
    document.getElementById('riskLevelContent').innerHTML = '';
    document.getElementById('risksContent').innerHTML = '';
    document.getElementById('metaContent').innerHTML = '';

    // 显示结果卡片（内容为空, 等待流式填充）
    resultSection.style.display = '';

    // ── 创建 AbortController (支持取消) ──
    if (currentAbortController) {
        currentAbortController.abort();
    }
    currentAbortController = new AbortController();

    try {
        const response = await fetch(`${API_BASE}/api/review/stream`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pr_url: prUrl }),
            signal: currentAbortController.signal,
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.detail || `服务器错误 (${response.status})`);
        }

        // ── 读取 SSE 流 ──
        await parseSSEStream(response);

    } catch (error) {
        if (error.name === 'AbortError') {
            console.log('分析已被取消');
            return;
        }
        updateProgress(0, '', '分析失败', true);
        progressBarFill.classList.add('done');
        progressBarFill.style.background = 'var(--color-danger)';
        showStatus(`❌ ${error.message}`, 'error');
    } finally {
        setLoading(false);
        currentAbortController = null;
    }
}

// ──────────────────────────────────────────────
// SSE 流解析
// ──────────────────────────────────────────────

/**
 * 解析 SSE 响应流
 *
 * 使用 ReadableStream API 逐行读取 SSE 事件,
 * 解析 event/data 字段, 分发到对应的处理函数。
 *
 * SSE 格式 (每行以 \n 结尾, 事件间以 \n\n 分隔):
 *   event: progress
 *   data: 正在分析...
 *
 *   event: summary
 *   data: {"overview": "...", ...}
 *
 * @param {Response} response - fetch 返回的 Response 对象
 */
async function parseSSEStream(response) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();

    let buffer = '';      // 缓冲区: 积累未完整的事件行
    let currentEvent = ''; // 当前正在解析的事件类型
    let currentData = '';  // 当前正在解析的数据内容
    // 统计风险未分析的文件数量 (用于估算进度百分比)
    let totalFiles = 0;
    let analyzedFiles = 0;

    showProgress();

    try {
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });

            // 按行分割, 最后一行可能不完整, 留在 buffer 中
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';

            for (const line of lines) {
                if (line.startsWith('event: ')) {
                    currentEvent = line.slice(7).trim();
                } else if (line.startsWith('data: ')) {
                    currentData = line.slice(6).trim();
                } else if (line === '') {
                    // 空行 = 事件结束, 处理事件
                    if (currentEvent && currentData) {
                        handleSSEEvent(currentEvent, currentData, {
                            totalFiles,
                            analyzedFiles,
                            setAnalyzedFiles: (n) => { analyzedFiles = n; },
                            setTotalFiles: (n) => { totalFiles = n; },
                        });
                    }
                    currentEvent = '';
                    currentData = '';
                }
            }
        }
    } finally {
        reader.releaseLock();
    }
}

// ──────────────────────────────────────────────
// SSE 事件分发
// ──────────────────────────────────────────────

/**
 * 处理单个 SSE 事件
 *
 * 根据事件类型, 调用对应的渲染函数:
 * - meta:    显示 PR 元信息卡片 + 设置文件总数
 * - progress: 更新进度条文字和百分比
 * - summary:  渲染变更总结卡片
 * - risk:     追加风险项
 * - done:     完成进度条, 显示最终元信息
 * - error:    显示错误
 *
 * @param {string} event - 事件类型
 * @param {string} dataStr - JSON 字符串或纯文本
 * @param {object} ctx - 上下文 (文件计数)
 */
function handleSSEEvent(event, dataStr, ctx) {
    switch (event) {
        case 'meta': {
            const meta = JSON.parse(dataStr);
            // 设置文件总数供进度条使用
            ctx.setTotalFiles(meta.total_files);
            // 立即展示 PR 元信息
            renderMeta(meta, null);
            break;
        }

        case 'progress': {
            // 进度文字, 如 "正在分析 (3/15): src/auth.py"
            const text = dataStr;
            const match = text.match(/\((\d+)\/(\d+)\)/);
            if (match) {
                const current = parseInt(match[1], 10);
                const total = parseInt(match[2], 10);
                ctx.setAnalyzedFiles(current - 1); // 标注"正在分析N"时, 已完成N-1个
                ctx.setTotalFiles(total);
                if (total > 0) {
                    const pct = Math.round(((current - 1) / total) * 100);
                    updateProgress(pct, text, `已分析 ${current - 1}/${total} 个文件`);
                } else {
                    updateProgress(0, text, '');
                }
            } else if (text === '分析完成!') {
                updateProgress(100, text, '', true);
                progressBarFill.classList.add('done');
            } else {
                updateProgress(0, text, '');
            }
            break;
        }

        case 'summary': {
            const summary = JSON.parse(dataStr);
            renderSummary(summary);
            renderRiskLevel(summary.risk_level);
            break;
        }

        case 'risk': {
            const risk = JSON.parse(dataStr);
            riskItems.push(risk);
            // 追加渲染单个风险项 (扩展最后一个)
            appendRiskItem(risk, riskIndex);
            riskIndex++;
            if (ctx.totalFiles > 0 && ctx.analyzedFiles < ctx.totalFiles) {
                const current = ctx.analyzedFiles + 1;
                updateProgress(
                    Math.round((current / ctx.totalFiles) * 100),
                    `正在分析 (${current}/${ctx.totalFiles})...`,
                    `发现 ${riskItems.length} 个风险`
                );
            }
            break;
        }

        case 'done': {
            const info = JSON.parse(dataStr);
            // 标记进度条完成
            updateProgress(100, '分析完成', `耗时 ${(info.duration_ms / 1000).toFixed(1)}s · ${info.tokens.toLocaleString()} tokens · ${info.risk_count} 个风险`, true);
            progressBarFill.classList.add('done');
            // 更新完整元信息 (token / 耗时 / 模型)
            const metaElement = document.getElementById('metaContent');
            if (metaElement) {
                renderMeta(null, info);
            }
            // 显示风险筛选栏和计数
            if (riskItems.length > 0) {
                showFilterBar();
                updateRiskCountBadge(riskItems.length);
            }
            // 如果没有风险, 显示优化后的空状态
            if (info.risk_count === 0) {
                const risksContainer = document.getElementById('risksContent');
                if (risksContainer && risksContainer.innerHTML.trim() === '') {
                    risksContainer.innerHTML = '<div class="no-risks"><span class="no-risks-icon">✅</span><strong>未发现需要关注的风险代码</strong>本次变更的代码质量良好</div>';
                }
            }
            // 2秒后隐藏进度条
            setTimeout(() => {
                progressSection.style.display = 'none';
            }, 2000);
            break;
        }

        case 'error': {
            updateProgress(0, '分析出错', '', true);
            progressBarFill.classList.add('done');
            progressBarFill.style.background = 'var(--color-danger)';
            showStatus(`❌ ${dataStr}`, 'error');
            break;
        }

        default:
            console.log('未知 SSE 事件:', event, dataStr.substring(0, 100));
    }
}

// ──────────────────────────────────────────────
// 渲染函数
// ──────────────────────────────────────────────

function renderSummary(summary) {
    const container = document.getElementById('summaryContent');
    if (!summary) return;

    let html = '';

    html += `<div class="summary-overview">${escapeHtml(summary.overview)}</div>`;

    if (summary.key_changes && summary.key_changes.length > 0) {
        html += '<ul class="key-changes-list">';
        summary.key_changes.forEach(change => {
            html += `<li>${escapeHtml(change)}</li>`;
        });
        html += '</ul>';
    }

    if (summary.affected_modules && summary.affected_modules.length > 0) {
        html += '<div class="modules-tags">';
        summary.affected_modules.forEach(mod => {
            html += `<span class="module-tag">${escapeHtml(mod)}</span>`;
        });
        html += '</div>';
    }

    container.innerHTML = html;
}

function renderRiskLevel(level) {
    const container = document.getElementById('riskLevelContent');
    const labels = {
        trivial: '✅ 无风险',
        low: '🟢 低风险',
        medium: '🟡 中风险',
        high: '🟠 高风险',
        critical: '🔴 严重风险',
    };

    const className = `risk-${level || 'low'}`;
    const label = labels[level] || level || '未知';

    container.innerHTML = `
        <span class="risk-level-badge ${className}">${label}</span>
        <p style="margin-top:8px;font-size:13px;color:var(--color-text-secondary);">
            此评级由 AI 基于变更范围、代码模式和历史经验综合判定，仅供参考
        </p>
    `;
}

/**
 * 追加渲染单个风险项
 *
 * 每次 SSE 推送一个风险时, 不重新渲染整个列表,
 * 而是在 DOM 末尾追加一个新的风险项 (增量渲染)。
 *
 * @param {object} risk - 风险数据
 * @param {number} index - 全局序号
 */
function appendRiskItem(risk, index) {
    const container = document.getElementById('risksContent');

    const confidencePercent = Math.round((risk.confidence || 0) * 100);
    const categoryLabel = risk.category || '';
    const categoryHtml = categoryLabel
        ? `<span class="risk-category cat-${escapeHtml(categoryLabel)}">${escapeHtml(categoryLabel)}</span>`
        : '';

    const itemHtml = `
        <div class="risk-item severity-${risk.severity}" data-index="${index}">
            <div class="risk-header" onclick="toggleRisk(${index})">
                <span class="risk-severity">${risk.severity}</span>
                ${categoryHtml}
                <span class="risk-title">${escapeHtml(risk.title)}</span>
                <span class="risk-file">${escapeHtml(risk.file)} ${risk.line_range ? escapeHtml(risk.line_range) : ''}</span>
                <span class="risk-confidence">置信度 ${confidencePercent}%</span>
            </div>
            <div class="risk-body">
                <p><span class="label">问题描述</span>${escapeHtml(risk.description)}</p>
                ${risk.suggestion ? `
                    <div class="risk-suggestion">
                        <span class="label">改进建议</span>${escapeHtml(risk.suggestion)}
                    </div>
                ` : ''}
                ${risk.code_snippet ? `
                    <div class="risk-code">${escapeHtml(risk.code_snippet)}</div>
                ` : ''}
            </div>
        </div>
    `;

    container.insertAdjacentHTML('beforeend', itemHtml);

    // 高亮新插入的代码块
    const newItem = container.querySelector(`.risk-item[data-index="${index}"]`);
    if (newItem) {
        const codeBlock = newItem.querySelector('.risk-code');
        if (codeBlock && window.hljs) {
            hljs.highlightElement(codeBlock);
        }
    }
}

/**
 * 渲染/更新分析元信息
 *
 * 可以被调用两次:
 * 1. meta 事件时: 渲染 PR 标题/作者/分支 (info 为 null)
 * 2. done 事件时: 追加模型/token/耗时 (meta 为 null)
 *
 * @param {object|null} meta - PR 元信息 (meta 事件)
 * @param {object|null} info - 分析完成信息 (done 事件)
 */
function renderMeta(meta, info) {
    const container = document.getElementById('metaContent');

    // 如果容器已有内容, 追加分析信息而非覆盖
    if (container.dataset.loaded === 'true' && info) {
        const modelEl = container.querySelector('.meta-model');
        const tokensEl = container.querySelector('.meta-tokens');
        const durationEl = container.querySelector('.meta-duration');
        const riskCountEl = container.querySelector('.meta-risk-count');
        if (modelEl) modelEl.textContent = info.model || 'N/A';
        if (tokensEl) tokensEl.textContent = (info.tokens || 0).toLocaleString();
        if (durationEl) durationEl.textContent = `${((info.duration_ms || 0) / 1000).toFixed(1)}s`;
        if (riskCountEl) riskCountEl.textContent = (info.risk_count || 0).toString();
        return;
    }

    const title = meta ? meta.title : '';
    const author = meta ? meta.author : '';
    const headBranch = meta ? meta.head_branch : '';
    const baseBranch = meta ? meta.base_branch : '';
    const modelName = info ? info.model : '';
    const tokens = info ? (info.tokens || 0) : 0;
    const duration = info ? `${((info.duration_ms || 0) / 1000).toFixed(1)}s` : '—';
    const riskCount = info ? (info.risk_count || 0) : 0;

    container.dataset.loaded = meta ? 'true' : 'false';

    container.innerHTML = `
        <div class="meta-grid">
            <div class="meta-item">
                <div class="meta-label">PR 标题</div>
                <div class="meta-value" style="font-size:13px;">${escapeHtml(title)}</div>
            </div>
            <div class="meta-item">
                <div class="meta-label">作者</div>
                <div class="meta-value">${escapeHtml(author)}</div>
            </div>
            <div class="meta-item">
                <div class="meta-label">分支</div>
                <div class="meta-value">${escapeHtml(headBranch)} → ${escapeHtml(baseBranch)}</div>
            </div>
            <div class="meta-item">
                <div class="meta-label">分析模型</div>
                <div class="meta-value meta-model">${escapeHtml(modelName)}</div>
            </div>
            <div class="meta-item">
                <div class="meta-label">Token 消耗</div>
                <div class="meta-value meta-tokens">${tokens.toLocaleString()}</div>
            </div>
            <div class="meta-item">
                <div class="meta-label">风险数量</div>
                <div class="meta-value meta-risk-count">${riskCount}</div>
            </div>
            <div class="meta-item">
                <div class="meta-label">分析耗时</div>
                <div class="meta-value meta-duration">${duration}</div>
            </div>
        </div>
    `;
}

// ──────────────────────────────────────────────
// 进度条控制
// ──────────────────────────────────────────────

function showProgress() {
    progressSection.style.display = '';
}

function updateProgress(percent, text, subtext, isDone = false) {
    progressBarFill.style.width = `${percent}%`;
    if (text) progressText.textContent = text;
    if (subtext !== undefined) progressSubtext.textContent = subtext;
    if (isDone) {
        progressBarFill.classList.add('done');
    }
}

function resetProgress() {
    progressSection.style.display = 'none';
    progressBarFill.style.width = '0%';
    progressBarFill.classList.remove('done');
    progressBarFill.style.background = '';
    progressText.textContent = '正在连接...';
    progressSubtext.textContent = '';
}

// ──────────────────────────────────────────────
// UI 辅助
// ──────────────────────────────────────────────

function setLoading(isLoading) {
    analyzeBtn.disabled = isLoading;
    btnText.style.display = isLoading ? 'none' : '';
    btnLoading.style.display = isLoading ? '' : 'none';
}

function showStatus(message, type) {
    statusSection.style.display = '';
    statusContent.className = `status-card status-${type}`;

    if (type === 'error') {
        // 错误状态附带重试按钮
        statusContent.innerHTML = `
            <span>${escapeHtml(message)}</span>
            <button class="btn-retry" onclick="handleRetry()">🔄 重试</button>
        `;
    } else {
        statusContent.textContent = message;
    }
}

/**
 * 重试最后一次分析
 */
function handleRetry() {
    hideStatus();
    handleAnalyze();
}

function hideStatus() {
    statusSection.style.display = 'none';
    // 清理 retry 按钮的 innerHTML, 防止下次 textContent 不生效
    statusContent.innerHTML = '';
}

function hideResult() {
    resultSection.style.display = 'none';
}

function toggleRisk(index) {
    const item = document.querySelector(`.risk-item[data-index="${index}"]`);
    if (item) {
        item.classList.toggle('expanded');
    }
}

function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// ──────────────────────────────────────────────
// 风险筛选栏
// ──────────────────────────────────────────────

/**
 * 显示筛选栏并绑定事件
 */
function showFilterBar() {
    riskFilterBar.style.display = '';
    riskCountBadge.style.display = '';
    // 绑定筛选按钮点击事件 (只绑一次)
    if (!riskFilterBar.dataset.bound) {
        riskFilterBar.querySelectorAll('.filter-tag').forEach(btn => {
            btn.addEventListener('click', () => {
                const filter = btn.dataset.filter;
                currentFilter = filter;
                // 更新 active 样式
                riskFilterBar.querySelectorAll('.filter-tag').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                // 应用筛选
                applyFilter(filter);
            });
        });
        riskFilterBar.dataset.bound = '1';
    }
}

/**
 * 更新风险计数徽章
 * @param {number} count - 风险总数
 */
function updateRiskCountBadge(count) {
    riskCountBadge.textContent = `${count} 个风险`;
    riskCountBadge.style.display = '';
}

/**
 * 重置筛选栏 (新一轮分析时调用)
 */
function resetFilterBar() {
    riskFilterBar.style.display = 'none';
    riskCountBadge.style.display = 'none';
    currentFilter = 'all';
    // 重置按钮 active
    riskFilterBar.querySelectorAll('.filter-tag').forEach(b => b.classList.remove('active'));
    const allBtn = riskFilterBar.querySelector('[data-filter="all"]');
    if (allBtn) allBtn.classList.add('active');
    riskFilterBar.dataset.bound = '';
}

/**
 * 按分类筛选风险项
 *
 * 显示/隐藏 DOM 中的风险卡片, 统计匹配数量。
 * 同时更新筛选栏中每个按钮的计数。
 *
 * @param {string} filter - 'all' | '安全' | '性能' | '逻辑' | '稳定性' | '规范'
 */
function applyFilter(filter) {
    const items = document.querySelectorAll('.risk-item');
    let visibleCount = 0;
    let totalCount = items.length;

    items.forEach(item => {
        const categoryEl = item.querySelector('.risk-category');
        const category = categoryEl ? categoryEl.textContent.trim() : '';

        if (filter === 'all' || category === filter) {
            item.classList.remove('filtered-out');
            visibleCount++;
        } else {
            item.classList.add('filtered-out');
        }
    });

    // 更新帮助文字的统计
    if (filter === 'all') {
        updateRiskCountBadge(totalCount);
    } else {
        riskCountBadge.textContent = `${filter} · ${visibleCount}/${totalCount}`;
    }
}

// ── 初始化 ──

console.log('AI PR Review 前端已就绪 (流式版本)');
