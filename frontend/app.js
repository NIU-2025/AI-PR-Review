/**
 * AI PR Review - 前端交互逻辑
 *
 * 核心功能:
 * - 提交 PR URL 到后端 /api/review
 * - 轮询或等待分析结果
 * - 渲染结构化的 Review 报告
 * - 错误处理与 loading 状态
 */

const API_BASE = window.location.origin;

// ── DOM 引用 ──

const prUrlInput = document.getElementById('prUrl');
const analyzeBtn = document.getElementById('analyzeBtn');
const btnText = analyzeBtn.querySelector('.btn-text');
const btnLoading = analyzeBtn.querySelector('.btn-loading');
const statusSection = document.getElementById('statusSection');
const statusContent = document.getElementById('statusContent');
const resultSection = document.getElementById('resultSection');

// ── 事件绑定 ──

analyzeBtn.addEventListener('click', handleAnalyze);
prUrlInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') handleAnalyze();
});

// ── 主流程 ──

async function handleAnalyze() {
    const prUrl = prUrlInput.value.trim();

    if (!prUrl) {
        showStatus('请输入 GitHub PR URL', 'error');
        return;
    }

    if (!prUrl.includes('github.com') || !prUrl.includes('/pull/')) {
        showStatus('URL 格式不正确。请使用格式: https://github.com/{owner}/{repo}/pull/{number}', 'error');
        return;
    }

    // 进入 loading 状态
    setLoading(true);
    hideResult();
    showStatus('正在从 GitHub 获取 PR 数据...', 'loading');

    try {
        const response = await fetch(`${API_BASE}/api/review`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pr_url: prUrl }),
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            const detail = errorData.detail || `服务器错误 (${response.status})`;
            throw new Error(detail);
        }

        const data = await response.json();

        if (!data.success) {
            throw new Error(data.error || '分析失败');
        }

        // 渲染结果
        hideStatus();
        renderResult(data);
        resultSection.style.display = '';

    } catch (error) {
        showStatus(`❌ ${error.message}`, 'error');
    } finally {
        setLoading(false);
    }
}

// ── 渲染函数 ──

function renderResult(data) {
    const { pr_metadata, analysis } = data;

    renderSummary(analysis.summary);
    renderRiskLevel(analysis.summary.risk_level);
    renderRisks(analysis.risks);
    renderMeta(pr_metadata, analysis);
}

function renderSummary(summary) {
    const container = document.getElementById('summaryContent');
    if (!summary) {
        container.innerHTML = '<p>无法获取总结</p>';
        return;
    }

    let html = '';

    // 概述
    html += `<div class="summary-overview">${escapeHtml(summary.overview)}</div>`;

    // 关键变更
    if (summary.key_changes && summary.key_changes.length > 0) {
        html += '<ul class="key-changes-list">';
        summary.key_changes.forEach(change => {
            html += `<li>${escapeHtml(change)}</li>`;
        });
        html += '</ul>';
    }

    // 受影响模块
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

function renderRisks(risks) {
    const container = document.getElementById('risksContent');

    if (!risks || risks.length === 0) {
        container.innerHTML = '<div class="no-risks">✅ 未发现需要关注的风险代码</div>';
        return;
    }

    let html = '';
    risks.forEach((risk, index) => {
        const confidencePercent = Math.round((risk.confidence || 0) * 100);
        html += `
            <div class="risk-item severity-${risk.severity}" data-index="${index}">
                <div class="risk-header" onclick="toggleRisk(${index})">
                    <span class="risk-severity">${risk.severity}</span>
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
    });

    container.innerHTML = html;
}

function renderMeta(metadata, analysis) {
    const container = document.getElementById('metaContent');

    const modelName = analysis ? analysis.llm_model : 'N/A';
    const tokenUsed = analysis ? analysis.token_used : 0;
    const duration = analysis ? `${(analysis.analysis_duration_ms / 1000).toFixed(1)}s` : 'N/A';

    container.innerHTML = `
        <div class="meta-grid">
            <div class="meta-item">
                <div class="meta-label">PR 标题</div>
                <div class="meta-value" style="font-size:13px;">${escapeHtml(metadata.title || 'N/A')}</div>
            </div>
            <div class="meta-item">
                <div class="meta-label">作者</div>
                <div class="meta-value">${escapeHtml(metadata.author || 'N/A')}</div>
            </div>
            <div class="meta-item">
                <div class="meta-label">分支</div>
                <div class="meta-value">${escapeHtml(metadata.head_branch || '')} → ${escapeHtml(metadata.base_branch || '')}</div>
            </div>
            <div class="meta-item">
                <div class="meta-label">分析模型</div>
                <div class="meta-value">${escapeHtml(modelName)}</div>
            </div>
            <div class="meta-item">
                <div class="meta-label">Token 消耗</div>
                <div class="meta-value">${tokenUsed.toLocaleString()}</div>
            </div>
            <div class="meta-item">
                <div class="meta-label">分析耗时</div>
                <div class="meta-value">${duration}</div>
            </div>
        </div>
    `;
}

// ── UI 辅助 ──

function setLoading(isLoading) {
    analyzeBtn.disabled = isLoading;
    btnText.style.display = isLoading ? 'none' : '';
    btnLoading.style.display = isLoading ? '' : 'none';
}

function showStatus(message, type) {
    statusSection.style.display = '';
    statusContent.className = `status-card status-${type}`;
    statusContent.textContent = message;
}

function hideStatus() {
    statusSection.style.display = 'none';
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

// ── 初始化 ──

console.log('AI PR Review 前端已就绪');
