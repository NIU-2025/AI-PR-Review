"""
上下文构建模块

负责将 GitHub PR 的原始数据组装为 LLM 可消费的上下文文本。
这是整个系统的核心模块之一, 直接影响分析质量。

核心策略:
1. 根据变更量自动判定分析模式 (trivial / simple / normal / large)
2. 按优先级分层组装上下文 (diff → 文件信息 → 元信息)
3. Token 预算管理: 超出预算时按优先级裁剪
4. 大文件截断: 超过预算时只保留变更行 ±N 行上下文
5. [Day 2] 文件级上下文: 为每个文件独立构建分析上下文, 支持依赖感知
"""

import fnmatch
import logging
import re
from enum import Enum

from models.schemas import FileChange, PRData
from config import ContextConfig

logger = logging.getLogger(__name__)

# 粗略的 token 估算: 英文约 1 token ≈ 4 字符, 代码约 1 token ≈ 3 字符
_CHARS_PER_TOKEN_CODE = 3
_CHARS_PER_TOKEN_TEXT = 4

# 无需分析的文件匹配模式
# 这些文件通常不包含业务逻辑, 分析它们只会浪费 token 并产生无意义结果
_SKIP_FILE_PATTERNS = [
    # 压缩/构建产物
    "*.min.js",
    "*.min.css",
    "*.bundle.js",
    # 二进制/资源文件
    "*.svg",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.ico",
    "*.woff",
    "*.woff2",
    "*.ttf",
    "*.eot",
    # 锁文件 (纯依赖声明, 无业务逻辑)
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
    "Gemfile.lock",
    "go.sum",
    "Cargo.lock",
    "composer.lock",
    # Source map
    "*.map",
    # 编译产物
    "*.pyc",
    "*.class",
    "*.o",
    "*.so",
    "*.dll",
]


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
    # 文件级上下文构建 (Day 2 新增)
    # ──────────────────────────────────────────────

    def sort_files_by_importance(
        self, files: list[FileChange]
    ) -> list[FileChange]:
        """
        按分析重要性排序文件列表

        排序策略:
        1. 变更行数大的文件优先 (changes 降序)
        2. 核心业务文件 (源码) 优先于 配置/文档/测试 文件
        3. 新增文件和修改文件优先于删除文件

        这样在限额分析时, 重要文件不会被跳过。
        """
        # 文件类型权重: 测试文件需在源码检查前判定 (避免 .py/.js 误匹配)
        def _file_type_weight(filename: str) -> int:
            lower = filename.lower()
            # 测试文件 (必须最优先检查, 因为它也是 .py/.js 结尾)
            if "test" in lower or "spec" in lower or lower.endswith("_test.py") or lower.endswith(".test.js") or lower.endswith(".test.ts"):
                return 2
            # 源码文件 (最重要)
            if any(lower.endswith(ext) for ext in (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".rb", ".php")):
                return 0
            # 配置文件
            if any(lower.endswith(ext) for ext in (".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", "dockerfile", "makefile")):
                return 1
            # 文档
            if any(lower.endswith(ext) for ext in (".md", ".rst", ".txt", ".adoc")):
                return 3
            return 4

        def _sort_key(f: FileChange) -> tuple:
            return (
                _file_type_weight(f.filename),  # 文件类型权重 (越小越重要)
                -f.changes,  # 变更行数 (越大越重要)
                f.filename.lower(),  # 同条件按文件名字母序
            )

        return sorted(files, key=_sort_key)

    def should_skip_file(self, file_change: FileChange) -> bool:
        """
        判断文件是否应该跳过分析

        跳过的条件:
        1. 匹配 _SKIP_FILE_PATTERNS (压缩产物、锁文件、二进制等)
        2. 无 diff patch (无法分析, 通常是二进制文件)
        3. 纯文件删除 (status == 'removed', 无新增代码)
        4. 变更行数极少 (≤ 2 行) 且无实际代码内容 (可能在后续版本中恢复分析)

        Args:
            file_change: 文件变更信息

        Returns:
            True 表示跳过, False 表示需要分析
        """
        # 无 patch 内容, 无法分析
        if not file_change.patch:
            logger.debug(f"跳过 {file_change.filename}: 无 diff 内容")
            return True

        # 匹配跳过模式
        basename = file_change.filename.split("/")[-1]
        for pattern in _SKIP_FILE_PATTERNS:
            if fnmatch.fnmatch(basename, pattern) or fnmatch.fnmatch(file_change.filename, pattern):
                logger.debug(f"跳过 {file_change.filename}: 匹配跳过模式 {pattern}")
                return True

        # 纯删除文件 (status == 'removed' 且 additions == 0)
        if file_change.status == "removed" and file_change.additions == 0:
            logger.debug(f"跳过 {file_change.filename}: 纯删除文件")
            return True

        return False

    def build_file_context(
        self,
        file_change: FileChange,
        pr_data: PRData,
        cross_file_hints: dict[str, list[str]] | None = None,
    ) -> str:
        """
        为单个文件构建独立分析上下文

        与 build_context() 不同, 该方法只聚焦于一个文件的变更,
        同时注入 PR 整体信息和跨文件依赖提示, 帮助 LLM 做出更准确的分析。

        Args:
            file_change: 目标文件的变更信息
            pr_data: PR 整体数据 (用于获取元信息)
            cross_file_hints: 跨文件依赖提示 map, key=文件名, value=提示列表
                              如 {"src/auth.py": ["函数 authenticate() 签名已变更"]}

        Returns:
            该文件的独立分析上下文字符串
        """
        parts: list[str] = []

        # ── 文件标识 ──
        parts.append(
            f"## 分析文件: `{file_change.filename}` "
            f"({file_change.status}, +{file_change.additions} -{file_change.deletions})"
        )

        # ── PR 元信息 (精简版) ──
        meta = pr_data.metadata
        parts.append(
            f"### PR 上下文\n"
            f"- 标题: {meta.title}\n"
            f"- 作者: {meta.author}\n"
            f"- 分支: {meta.head_branch} → {meta.base_branch}\n"
            f"- 总文件数: {pr_data.total_files} (+{pr_data.total_additions} -{pr_data.total_deletions})"
        )

        # ── 跨文件依赖提示 ──
        if cross_file_hints and file_change.filename in cross_file_hints:
            hints = cross_file_hints[file_change.filename]
            hints_text = "\n".join(f"- {h}" for h in hints)
            parts.append(
                f"### ⚠️ 关联文件变更提示\n"
                f"以下与本文件相关的外部变更可能导致兼容性问题:\n"
                f"{hints_text}"
            )

        # ── 代码 diff ──
        patch = file_change.patch
        # 单文件上下文 token 预算: ~8K chars
        max_patch_chars = 8000
        if len(patch) > max_patch_chars:
            patch = patch[:max_patch_chars]
            patch += "\n...(文件过大, diff 已截断)"

        parts.append(
            f"### 代码变更 (unified diff)\n"
            f"```diff\n{patch}\n```"
        )

        context = "\n\n".join(parts)
        logger.debug(
            f"文件上下文构建完成: {file_change.filename} "
            f"({len(context)} chars, 约 {self._estimate_tokens(context)} tokens)"
        )

        return context

    def extract_cross_file_dependencies(
        self, files: list[FileChange]
    ) -> dict[str, list[str]]:
        """
        提取跨文件依赖关系, 生成提示信息

        通过分析每个文件的 import/reference 语句, 识别:
        - 文件 A import 了文件 B 的模块, 而文件 B 也在本次 PR 中被修改
        - 变更文件的导出函数签名是否发生变化

        这是简化的依赖分析, 不做 AST 解析, 仅依赖文本模式匹配。
        准确度有限, 但足以在大多数 PR 中提供有价值的上下文提示。

        Args:
            files: 变更文件列表

        Returns:
            dict: {文件名: [提示文本列表]}
        """
        if len(files) < 2:
            return {}

        # 构建文件名集合 (用于快速查找)
        file_names = set()
        # 提取文件名关键词 (去掉路径和扩展名)
        # e.g., src/utils/auth_helper.py → {"auth_helper", "auth", "helper"}
        file_keywords: dict[str, set[str]] = {}

        for f in files:
            basename = f.filename
            file_names.add(basename)
            # 提取文件名中的关键词
            stem = basename.split("/")[-1]  # auth_helper.py
            stem = stem.rsplit(".", 1)[0]  # auth_helper
            keywords = set(stem.lower().split("_"))
            keywords.add(stem.lower())
            file_keywords[basename] = keywords

        hints: dict[str, list[str]] = {}

        for f in files:
            if not f.patch:
                continue

            # 从 patch 中提取 import 引用
            added_lines = [line[1:].strip() for line in f.patch.split("\n") if line.startswith("+") and not line.startswith("+++")]

            for added_line in added_lines:
                # 匹配 import/require 模式
                # Python: from xxx import yyy  或  import xxx
                # JS/TS:  import ... from "xxx"  或  require("xxx")
                # Go:     import "xxx"
                match = re.search(r'''from\s+[`"']?(\S+)[`"']?\s+import|import\s+(\S+)|from\s+['"]([^'"]+)['"]|require\s*\(\s*['"]([^'"]+)['"]\s*\)|import\s+"([^"]+)"''', added_line)
                if not match:
                    continue

                # 提取模块名
                module = next((g for g in match.groups() if g), None)
                if not module:
                    continue

                # 检查引用的模块是否在本次 PR 中也被修改
                for other_file in file_names:
                    if other_file == f.filename:
                        continue
                    other_stem = other_file.split("/")[-1].rsplit(".", 1)[0]
                    # 简单匹配: 模块名是否包含在另一个文件名中
                    module_parts = module.lower().replace("/", ".").replace("..", ".").split(".")
                    other_parts = other_file.lower().replace("/", ".").split(".")
                    # 检查是否有重叠
                    common = set(module_parts) & set(other_parts)
                    has_overlap = bool(common) or other_stem.lower() in module.lower() or module.lower().split(".")[-1] in other_stem.lower()

                    if has_overlap:
                        hint = (
                            f"`{f.filename}` 引用了 `{module}`, "
                            f"而本次 PR 同时修改了 `{other_file}`。"
                            f"请检查接口是否兼容。"
                        )
                        if f.filename not in hints:
                            hints[f.filename] = []
                        hints[f.filename].append(hint)

        if hints:
            total = sum(len(v) for v in hints.values())
            logger.info(f"跨文件依赖分析完成, 共发现 {total} 条依赖提示")

        return hints

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
