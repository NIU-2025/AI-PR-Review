"""
上下文构建模块

负责将 GitHub PR 的原始数据组装为 LLM 可消费的上下文文本。
这是整个系统的核心模块之一, 直接影响分析质量。

核心策略:
1. 根据变更量自动判定分析模式 (trivial / simple / normal / large)
2. 按优先级分层组装上下文 (diff → 文件信息 → 元信息)
3. Token 预算管理: 超出预算时按优先级裁剪
4. 大文件截断: 超过预算时只保留变更行 ±N 行上下文
"""

import logging
from enum import Enum

from models.schemas import FileChange, PRData
from config import ContextConfig

logger = logging.getLogger(__name__)

# 粗略的 token 估算: 英文约 1 token ≈ 4 字符, 代码约 1 token ≈ 3 字符
_CHARS_PER_TOKEN_CODE = 3
_CHARS_PER_TOKEN_TEXT = 4


class AnalysisMode(Enum):
    """
    分析模式, 根据 PR 变更量自动判定

    - trivial: 极小变更 (≤1个文件, ≤10行), 只出总结
    - simple:   小变更 (≤3个文件, ≤50行), 只查 P0
    - normal:   常规变更, 完整分析 P0+P1
    - large:    大变更 (>30文件, >500行), 只出 Top 5 风险
    """
    TRIVIAL = "trivial"
    SIMPLE = "simple"
    NORMAL = "normal"
    LARGE = "large"


class ContextBuilder:
    """PR 上下文构建器"""

    def __init__(self, config: ContextConfig):
        """
        Args:
            config: 上下文构建相关配置
        """
        self.config = config
        self.context_lines = config.context_lines_around_hunk

    # ──────────────────────────────────────────────
    # 公开方法
    # ──────────────────────────────────────────────

    def determine_mode(self, pr_data: PRData) -> AnalysisMode:
        """
        根据 PR 变更量自动判定分析模式

        判定规则:
        - trivial: 文件数 ≤ 1 且 总行数 ≤ 10
        - simple:   文件数 ≤ 3 且 总行数 ≤ 50
        - large:    文件数 > 30 或 总行数 > 500
        - normal:   其余情况
        """
        total_files = pr_data.total_files
        total_lines = pr_data.total_additions + pr_data.total_deletions

        if total_files <= self.config.min_files_for_trivial_mode and total_lines <= self.config.max_lines_for_trivial_mode:
            mode = AnalysisMode.TRIVIAL
        elif total_files <= self.config.max_files_for_simple_mode and total_lines <= self.config.max_lines_for_simple_mode:
            mode = AnalysisMode.SIMPLE
        elif total_files > self.config.max_files_for_full_analysis or total_lines > 500:
            mode = AnalysisMode.LARGE
        else:
            mode = AnalysisMode.NORMAL

        logger.info(
            f"分析模式判定: {mode.value} "
            f"(文件数={total_files}, 行数={total_lines})"
        )
        return mode

    def build_context(self, pr_data: PRData) -> str:
        """
        构建 LLM 分析用的上下文文本

        Args:
            pr_data: 完整 PR 数据

        Returns:
            组装好的上下文字符串, 用于注入 LLM Prompt
        """
        mode = self.determine_mode(pr_data)

        # 按优先级分层构建上下文
        parts: list[str] = []

        # ── 第一优先级: PR 元信息 (2K tokens 预算) ──
        meta_text = self._build_metadata_section(pr_data)
        parts.append(meta_text)

        # ── 第二优先级: 代码 diff (16K tokens 预算) ──
        diff_text = self._build_diff_section(pr_data.files, mode)
        parts.append(diff_text)

        # ── 第三优先级: 文件总览 ──
        overview_text = self._build_file_overview_section(pr_data)
        parts.append(overview_text)

        context = "\n\n".join(parts)
        estimated_tokens = self._estimate_tokens(context)

        logger.info(
            f"上下文构建完成: 约 {estimated_tokens} tokens, "
            f"模式={mode.value}, "
            f"字符数={len(context)}"
        )

        return context

    def build_trivial_context(self, pr_data: PRData) -> str:
        """
        为 trivial 模式构建极简上下文

        只包含 PR 元信息 + 变更摘要, 不包含完整 diff,
        用于只有几句变更总结的场景。
        """
        meta = self._build_metadata_section(pr_data)
        files_summary = "\n".join(
            f"- {f.status}: `{f.filename}` (+{f.additions} -{f.deletions})"
            for f in pr_data.files
        )
        return f"{meta}\n\n## 变更文件\n{files_summary}"

    # ──────────────────────────────────────────────
    # 私有方法: 各分段构建
    # ──────────────────────────────────────────────

    def _build_metadata_section(self, pr_data: PRData) -> str:
        """构建 PR 元信息段"""
        meta = pr_data.metadata
        lines = [
            f"## PR 信息",
            f"- 标题: {meta.title}",
            f"- 作者: {meta.author}",
            f"- 分支: {meta.head_branch} → {meta.base_branch}",
            f"- Commit 数: {meta.commits_count}",
            f"- 文件数: {pr_data.total_files} (+{pr_data.total_additions} -{pr_data.total_deletions})",
        ]
        if meta.description:
            # PR 描述可能很长, 截断到 500 字符
            desc = meta.description[:500]
            if len(meta.description) > 500:
                desc += "...(已截断)"
            lines.append(f"\nPR 描述:\n{desc}")

        return "\n".join(lines)

    def _build_diff_section(
        self, files: list[FileChange], mode: AnalysisMode
    ) -> str:
        """
        构建代码 diff 段

        策略:
        - normal 模式: 所有文件的完整 patch
        - large 模式: 优先展示变更量大的文件, 其余截断
        - simple 模式: 全部 patch
        - trivial 模式: 全部 patch (文件很少)
        """
        if not files:
            return "## 代码变更\n(无文件变更)"

        # 按变更行数降序排列 (让大变更文件优先)
        sorted_files = sorted(files, key=lambda f: f.changes, reverse=True)

        # large 模式下限制文件数
        if mode == AnalysisMode.LARGE:
            max_files = self.config.max_files_for_full_analysis
            sorted_files = sorted_files[:max_files]
            logger.info(f"Large 模式: 仅分析前 {max_files} 个最大变更文件")

        diff_parts = []
        for f in sorted_files:
            file_diff = self._format_single_file_diff(f)
            diff_parts.append(file_diff)

        full_diff = "\n\n".join(diff_parts)

        # Token 预算控制: 代码 diff 部分控制在 ~16K tokens
        max_diff_chars = 16000 * _CHARS_PER_TOKEN_CODE
        if len(full_diff) > max_diff_chars:
            full_diff = full_diff[:max_diff_chars]
            full_diff += (
                f"\n\n...(diff 内容过长, 已截断。"
                f"剩余 {len(files) - len(sorted_files)} 个文件未显示)"
            )
            logger.warning(f"Diff 内容超预算, 已截断至 {max_diff_chars} 字符")

        return f"## 代码变更 (unified diff)\n\n{full_diff}"

    def _format_single_file_diff(self, file_change: FileChange) -> str:
        """格式化单个文件的 diff"""
        header = (
            f"### {file_change.status}: `{file_change.filename}` "
            f"(+{file_change.additions} -{file_change.deletions})"
        )

        patch = file_change.patch
        if not patch:
            return f"{header}\n(无 diff 内容, 可能是二进制文件)"

        # 简单截断: 单个文件 patch 不超过 ~8000 字符
        max_patch_chars = 8000
        if len(patch) > max_patch_chars:
            patch = patch[:max_patch_chars]
            patch += "\n...(文件 diff 过长, 已截断)"

        return f"{header}\n```diff\n{patch}\n```"

    def _build_file_overview_section(self, pr_data: PRData) -> str:
        """构建文件变更总览"""
        if pr_data.total_files == 0:
            return "## 文件总览\n(无文件变更)"

        lines = [f"## 文件变更总览 ({pr_data.total_files} 个文件)"]
        for f in pr_data.files[:50]:  # 最多显示 50 个文件名
            lines.append(
                f"- [{f.status}] `{f.filename}` "
                f"(+{f.additions} -{f.deletions})"
            )

        if pr_data.total_files > 50:
            lines.append(f"... 以及其余 {pr_data.total_files - 50} 个文件")

        return "\n".join(lines)

    # ──────────────────────────────────────────────
    # 工具方法
    # ──────────────────────────────────────────────

    def _estimate_tokens(self, text: str) -> int:
        """
        估算文本的 token 数量

        这是一个粗略估算, 实际 token 数取决于模型的分词器。
        代码 (含较多符号) 约 1 token / 3 字符,
        自然语言约 1 token / 4 字符。
        这里取折中值 3.5 字符/token。
        """
        return len(text) // 3
