"""
Pydantic 数据模型定义

定义了整个 Review 流程中的数据结构:
- 请求/响应模型
- PR 文件变更模型
- 风险项模型
- 分析结果模型
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


# ──────────────────────────────────────────────
# 请求模型
# ──────────────────────────────────────────────

class PRReviewRequest(BaseModel):
    """PR Review 请求"""
    pr_url: str = Field(
        ...,
        description="GitHub Pull Request 的完整 URL",
        examples=["https://github.com/owner/repo/pull/123"]
    )


# ──────────────────────────────────────────────
# PR 数据模型
# ──────────────────────────────────────────────

class FileChange(BaseModel):
    """PR 中单个文件的变更信息"""
    filename: str = Field(..., description="文件路径")
    status: str = Field(..., description="变更状态: added / modified / removed / renamed")
    additions: int = Field(default=0, description="新增行数")
    deletions: int = Field(default=0, description="删除行数")
    changes: int = Field(default=0, description="总变更行数")
    patch: str = Field(default="", description="unified diff 格式的补丁内容")
    raw_url: str = Field(default="", description="文件原始内容 URL")


class PRMetadata(BaseModel):
    """PR 元信息"""
    title: str = Field(default="", description="PR 标题")
    description: str = Field(default="", description="PR 描述 / body")
    author: str = Field(default="", description="PR 作者")
    base_branch: str = Field(default="", description="目标分支")
    head_branch: str = Field(default="", description="源分支")
    commits_count: int = Field(default=0, description="commit 数量")
    created_at: Optional[datetime] = Field(default=None, description="创建时间")
    updated_at: Optional[datetime] = Field(default=None, description="更新时间")


class PRData(BaseModel):
    """完整的 PR 数据"""
    metadata: PRMetadata = Field(default_factory=PRMetadata, description="PR 元信息")
    files: list[FileChange] = Field(default_factory=list, description="变更文件列表")
    total_files: int = Field(default=0, description="变更文件总数")
    total_additions: int = Field(default=0, description="总新增行数")
    total_deletions: int = Field(default=0, description="总删除行数")


# ──────────────────────────────────────────────
# 分析结果模型
# ──────────────────────────────────────────────

class RiskItem(BaseModel):
    """单个风险项"""
    severity: str = Field(
        ...,
        description="风险等级: P0(关键)/P1(高)/P2(中)/P3(低)"
    )
    category: str = Field(
        default="",
        description="风险分类标签: 安全/性能/逻辑/稳定性/规范"
    )
    file: str = Field(..., description="所在文件路径")
    line_range: str = Field(default="", description="涉及行范围, 如 'L42-L58'")
    title: str = Field(..., description="风险标题摘要")
    description: str = Field(..., description="风险详细描述, 说明为什么这是问题")
    suggestion: str = Field(default="", description="具体改进建议, 含可应用的代码示例")
    code_snippet: str = Field(default="", description="相关代码片段")
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="置信度 (0-1), 低于0.65的风险不展示给用户"
    )


class ChangeSummary(BaseModel):
    """PR 变更总结"""
    overview: str = Field(..., description="变更整体概述 (3-5句话)")
    key_changes: list[str] = Field(default_factory=list, description="关键变更点列表")
    affected_modules: list[str] = Field(default_factory=list, description="受影响的模块/领域")
    risk_level: str = Field(
        default="low",
        description="整体风险评估: trivial / low / medium / high / critical"
    )


class AnalysisResult(BaseModel):
    """阶段1分析结果: 变更总结"""
    summary: ChangeSummary = Field(default_factory=ChangeSummary, description="变更总结")
    risks: list[RiskItem] = Field(default_factory=list, description="风险项列表")
    llm_model: str = Field(default="", description="使用的模型名称")
    token_used: int = Field(default=0, description="消耗的 token 数 (估算)")
    analysis_duration_ms: int = Field(default=0, description="分析耗时 (毫秒)")


# ──────────────────────────────────────────────
# 响应模型
# ──────────────────────────────────────────────

class PRReviewResponse(BaseModel):
    """PR Review 完整响应"""
    success: bool = Field(..., description="请求是否成功")
    pr_url: str = Field(..., description="原始 PR URL")
    pr_metadata: PRMetadata = Field(default_factory=PRMetadata, description="PR 元信息")
    analysis: Optional[AnalysisResult] = Field(default=None, description="分析结果")
    error: Optional[str] = Field(default=None, description="错误信息")
    from_cache: bool = Field(default=False, description="是否来自本地缓存 (未重新调用 LLM)")
    cached_at: Optional[str] = Field(default=None, description="缓存保存时间 (ISO 8601)")


class CacheCheckResponse(BaseModel):
    """缓存检查响应 (用于前端预检是否有缓存)"""
    cached: bool = Field(..., description="是否有已缓存的 Review 结果")
    from_cache: bool = Field(default=False, description="本次响应是否来自缓存")
    cached_at: Optional[str] = Field(default=None, description="缓存保存时间")
    review_id: Optional[str] = Field(default=None, description="本次 Review 的唯一标识")
    pr_metadata: Optional[dict] = Field(default=None, description="PR 元信息 (dict)")
    analysis: Optional[dict] = Field(default=None, description="分析结果 (dict)")
