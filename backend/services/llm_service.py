"""
LLM 分析服务模块

负责调用大语言模型进行 PR 代码评审。
采用多阶段分析流程, 每个阶段有独立的 Prompt 和输出约束。

分析流程:
  Stage 1 - 变更总结: 理解 PR 做了什么
  Stage 2 - 风险识别: 逐文件/逐段识别风险代码 (带自我反驳)
  Stage 3 - 结果整合: 合并多阶段结果, 去重, 格式化

支持的模型: 任何兼容 OpenAI API 的模型 (GPT-4o, DeepSeek-V3, Claude 等)
"""

import asyncio
import json
import logging
import time
from typing import Optional

from openai import OpenAI

from config import LLMConfig
from models.schemas import (
    AnalysisResult,
    ChangeSummary,
    FileChange,
    PRData,
    RiskItem,
)
from utils.context_builder import AnalysisMode, ContextBuilder

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Prompt 模板
# ──────────────────────────────────────────────

# === Stage 1: 变更总结 ===

SYSTEM_PROMPT_SUMMARY = """你是一位资深代码评审专家, 拥有 10 年以上软件开发经验。
你的任务是对 GitHub Pull Request 的变更进行客观、准确的总结。

## 输出要求
请以 JSON 格式输出, 包含以下字段:
{
  "overview": "用 3-5 句话概括这次 PR 的整体变更内容、目的和影响范围",
  "key_changes": ["变更点1", "变更点2", ...],    // 3-7 个关键变更点
  "affected_modules": ["模块1", "模块2", ...],    // 受影响的模块/领域
  "risk_level": "trivial|low|medium|high|critical"  // 整体风险等级
}

## 风险等级判定参考
- trivial: 仅修改配置、文档、依赖版本号, 无业务逻辑变更
- low: 小范围代码调整, 如修复 typo、调整日志、增加非关键校验
- medium: 涉及核心业务逻辑修改、新增功能模块、API 变更
- high: 涉及认证授权、数据持久化、并发控制、外部服务调用
- critical: 涉及安全漏洞修复、数据迁移、破坏性 API 变更

## 注意事项
- 客观描述, 不添加主观评价
- 基于 diff 内容总结, 不要猜测变更意图
- 如果 PR 描述中有变更目的, 优先参考但不要照搬
- 仅输出 JSON, 不要包含其他文字"""


# === Stage 2: 风险代码识别 ===

SYSTEM_PROMPT_RISK = """你是一位资深代码安全与质量审计专家。
你的任务是审查代码变更, 识别潜在的风险和问题。

## 风险等级定义
- P0 (关键): 确定有问题的代码, 可能导致安全漏洞、数据丢失、服务崩溃
- P1 (高): 很可能有问题, 如逻辑矛盾、明显性能问题、空指针/未定义行为
- P2 (中): 可能有隐患, 如不符合最佳实践、边界条件未处理、资源泄露风险
- P3 (低): 建议改进, 如命名不规范、注释缺失、代码风格

## 风险分类标签
每个风险必须归类到以下标签之一:
- [安全]: SQL注入、XSS、CSRF、敏感信息泄露、权限绕过、不安全的反序列化、硬编码密钥
- [性能]: N+1查询、大循环内重复IO、未使用缓存、内存泄露、不必要的对象创建、同步阻塞
- [逻辑]: 空指针、数组越界、类型错误、死循环、竞态条件、错误的条件判断、不完整的错误处理
- [稳定性]: 未处理异常、资源未释放(fd/连接)、超时未设置、缺少重试机制、单点故障
- [规范]: 命名不规范、Magic Number、代码重复、缺少注释、函数过长、圈复杂度过高

## Few-Shot 示例

### 示例 1: 应该标记为 [安全] P0
```diff
+ sql = f"SELECT * FROM users WHERE id = {user_id}"
```
正确: P0 [安全] SQL注入 - 使用字符串拼接构造SQL, 攻击者可通过user_id注入恶意代码。
错误: 如果该行在注释中或是在测试用例中模拟SQL注入检测, 则不应标记。

### 示例 2: 不应该标记 (误报示范)
```diff
+ if user is None:
+     return None
```
正确: 不标记。这是空值检查的惯用写法, 属于防御性编程, 代码意图清晰。

### 示例 3: 应该标记为 [性能] P2
```diff
+ for item in items:
+     result = db.query(f"SELECT * FROM detail WHERE id = {item.id}")
```
正确: P2 [性能] N+1查询 - 循环内执行数据库查询, items为100个时会产生101次查询。

### 示例 4: 不应该标记 (白名单)
```diff
+ console.log("debug:", data)
```
正确: 不标记。虽然调试日志不雅观, 但不导致功能问题。如果日志中包含敏感信息(token/密码)则标记为P0 [安全]。

## 自我反驳机制 (重要!)
对每一个你标记的风险, 你必须在输出前完成自我反驳:
1. 这段代码在什么情况下实际上是安全的?
2. 文件中是否有其他地方做了一样的模式?
3. 如果这是测试文件, 该模式是否是故意为之?
只有当三个反驳都**不成立**时, 才输出该风险。

## 白名单 (以下模式不应报告为风险)
- 非空检查的惯用写法: if x is None, if not x, x ?? default
- 日志级别/内容的调整 (除非包含敏感信息泄露)
- 注释增删、格式化调整、import 排序、变量重命名
- 测试文件中的 "硬编码" 数据和 mock 对象
- 配置文件中新增的普通配置项
- 依赖版本号更新 (Dependabot/renovate 自动 PR)
- 文档文件的变更

## 输出要求
每个风险必须包含 what(什么问题) / why(为什么是问题) / fix(怎么改):

```json
{
  "risks": [
    {
      "severity": "P0|P1|P2|P3",
      "category": "安全|性能|逻辑|稳定性|规范",
      "file": "文件路径",
      "line_range": "涉及行范围, 如 L42-L58",
      "title": "风险标题 (≤15字, 如 'SQL注入风险')",
      "description": "详细描述, 必须说明: 什么代码有什么问题、为什么这是问题、可能导致的后果",
      "suggestion": "具体的修改方案, 给出可直接应用的代码示例",
      "code_snippet": "触发风险的代码片段",
      "confidence": 0.85
    }
  ]
}
```

关键要求:
- confidence 必须 ≥ 0.65 才输出, 不确定的风险宁可漏报不要误报
- suggestion 必须给出具体的修改代码, 不能只说"建议优化"
- 每个风险必须带 category 标签

如果 PR 变更中没有发现任何风险, 请输出 {"risks": []}。
仅输出 JSON, 不要包含其他文字。"""


# === Stage 1 (trivial模式) 简化版 ===

SYSTEM_PROMPT_TRIVIAL = """你是一位资深代码评审专家。
请对以下 PR 变更进行简要总结。

## 风险等级判定
- trivial: 仅修改配置、文档、依赖版本号, 无业务逻辑变更 (如 Dependabot PR)

## 输出要求
请以 JSON 格式输出:
{
  "overview": "用 2-3 句话概括这次变更内容和影响",
  "key_changes": ["变更点1", "变更点2"],
  "affected_modules": [],
  "risk_level": "trivial"
}
仅输出 JSON, 不要包含其他文字。"""


# === Stage 2 (逐文件模式): 单文件风险分析 ===

SYSTEM_PROMPT_PER_FILE_RISK = """你是一位资深代码安全与质量审计专家。
你的任务是审查**单个文件**的代码变更, 识别潜在的风险和问题。

## 重要: 你只分析下面提供的一个文件, 不要分析其他文件!

## 风险等级定义
- P0 (关键): 确定有问题的代码, 可能导致安全漏洞、数据丢失、服务崩溃
- P1 (高): 很可能有问题, 如逻辑矛盾、明显性能问题、空指针/未定义行为
- P2 (中): 可能有隐患, 如不符合最佳实践、边界条件未处理、资源泄露风险
- P3 (低): 建议改进, 如命名不规范、注释缺失、代码风格

## 风险分类标签
每个风险必须归类到以下标签之一:
- [安全]: SQL注入、XSS、敏感信息泄露、权限绕过、硬编码密钥、不安全的加密算法
- [性能]: N+1查询、循环内IO、内存泄露、不必要的对象创建、同步阻塞
- [逻辑]: 空指针、数组越界、类型错误、竞态条件、错误的条件判断
- [稳定性]: 未处理异常、资源未释放、超时未设置、缺少重试、单点故障
- [规范]: 命名不规范、Magic Number、代码重复、函数过长、缺少注释

## Few-Shot 参考 (常见模式速查)

应标记:
- 字符串拼接构造SQL → P0 [安全] SQL注入
- 循环内执行数据库查询或HTTP请求 → P1/P2 [性能] N+1查询
- 打开文件/连接后无对应的close/release → P1 [稳定性] 资源泄露
- 异常捕获后仅print不处理 → P1 [稳定性] 异常吞没
- 硬编码的密码/token/密钥 → P0 [安全] 敏感信息泄露
- 使用 eval()/exec() 处理用户输入 → P0 [安全] 代码注入

不应标记:
- if x is None: return / if not x: continue → 防御性编程惯用写法
- console.log() / print() 调试语句 → 仅 [规范] 级别的提醒, 非功能问题
- 依赖版本号变更 → 不标记
- 测试文件中的 mock/hardcode → 不标记
- 配置文件新增普通配置项 → 不标记

## 自我反驳 (必须执行)
对每个风险, 依次检查:
1. 这段代码在测试文件/开发环境中是否无害?
2. 同级文件(同目录下其他文件)是否有相同模式?
3. 该模式是否在旧代码中已经存在(仅检查新增代码)?
三项中任意一项为"是"→ 不标记。

## 输出要求
输出 JSON, 每个风险必须含 what/why/fix:

```json
{
  "risks": [
    {
      "severity": "P0|P1|P2|P3",
      "category": "安全|性能|逻辑|稳定性|规范",
      "file": "文件路径",
      "line_range": "L起始-结束",
      "title": "≤15字的标题",
      "description": "说明: 什么代码、有什么问题、为什么是问题",
      "suggestion": "给出可直接应用的修改代码",
      "code_snippet": "触发风险的代码",
      "confidence": 0.85
    }
  ]
}
```

要求:
- confidence < 0.65 的风险不要输出
- suggestion 必须能直接应用, 不说空话
- 每个风险必带 category

如果无风险, 输出 {"risks": []}。
仅输出 JSON。"""


# ──────────────────────────────────────────────
# LLM 服务类
# ──────────────────────────────────────────────

class LLMService:
    """LLM 分析服务"""

    def __init__(self, config: LLMConfig):
        """
        Args:
            config: LLM 相关配置
        """
        self.config = config
        self.model = config.model_name

        self._total_tokens_used = 0

        self._client = OpenAI(
            api_key=config.api_key,
            base_url=config.api_base_url,
            timeout=config.request_timeout,
        )

    # ──────────────────────────────────────────────
    # 公开方法: 主分析入口
    # ──────────────────────────────────────────────

    def analyze(
        self,
        pr_data: PRData,
        context: str,
        mode: AnalysisMode,
    ) -> AnalysisResult:
        """
        主分析入口: 根据分析模式执行不同深度的分析

        Args:
            pr_data: PR 完整数据
            context: 组装好的上下文字符串
            mode: 分析模式

        Returns:
            AnalysisResult: 分析结果
        """
        start_time = time.time()
        self._total_tokens_used = 0

        # ── Stage 1: 变更总结 (所有模式都执行) ──
        logger.info(f"[Stage 1] 开始变更总结 (模式={mode.value})")
        summary = self._run_summary_stage(context, mode)
        logger.info(f"[Stage 1] 变更总结完成, 风险等级={summary.risk_level}")

        # ── Stage 2: 风险识别 (trivial 模式跳过) ──
        risks: list[RiskItem] = []
        if mode != AnalysisMode.TRIVIAL:
            logger.info(f"[Stage 2] 开始风险识别 (模式={mode.value})")
            risks = self._run_risk_stage(context, mode, pr_data)
            logger.info(f"[Stage 2] 风险识别完成, 发现 {len(risks)} 个风险")

        elapsed_ms = int((time.time() - start_time) * 1000)

        return AnalysisResult(
            summary=summary,
            risks=risks,
            llm_model=self.model,
            token_used=self._total_tokens_used,
            analysis_duration_ms=elapsed_ms,
        )

    # ──────────────────────────────────────────────
    # Day 2 新增: 逐文件分析入口
    # ──────────────────────────────────────────────

    def analyze_per_file(
        self,
        pr_data: PRData,
        overall_context: str,
        mode: AnalysisMode,
        context_builder: ContextBuilder,
    ) -> AnalysisResult:
        """
        逐文件深度分析入口

        流程:
        1. 先用整体上下文跑 Stage 1 变更总结
        2. 用 context_builder 进行文件排序、跳过、上下文构建
        3. 逐文件调用 LLM 进行风险识别 (顺序执行, 控制 API 压力)
        4. 汇总所有文件的风险 + 整体总结

        Args:
            pr_data: PR 完整数据
            overall_context: 整体上下文 (用于变更总结)
            mode: 分析模式
            context_builder: 上下文构建器 (用于文件级上下文)

        Returns:
            AnalysisResult: 包含总结和逐文件风险的分析结果
        """
        start_time = time.time()
        self._total_tokens_used = 0

        # ── Stage 1: 变更总结 (同上) ──
        logger.info(f"[Stage 1] 开始变更总结 (模式={mode.value})")
        summary = self._run_summary_stage(overall_context, mode)
        logger.info(f"[Stage 1] 变更总结完成, 风险等级={summary.risk_level}")

        # ── Stage 2: 逐文件风险识别 ──
        all_risks: list[RiskItem] = []
        if mode != AnalysisMode.TRIVIAL:
            logger.info(f"[Stage 2] 开始逐文件风险识别 (模式={mode.value})")
            all_risks = self._run_per_file_risk_stage(
                pr_data, mode, context_builder
            )
            logger.info(
                f"[Stage 2] 逐文件风险识别完成, "
                f"共分析 {len([f for f in pr_data.files if not context_builder.should_skip_file(f)])} 个文件, "
                f"发现 {len(all_risks)} 个风险"
            )

        elapsed_ms = int((time.time() - start_time) * 1000)

        return AnalysisResult(
            summary=summary,
            risks=all_risks,
            llm_model=self.model,
            token_used=self._total_tokens_used,
            analysis_duration_ms=elapsed_ms,
        )

    # ──────────────────────────────────────────────
    # Day 2 新增: 流式逐文件分析 (SSE Generator)
    # ──────────────────────────────────────────────

    def analyze_per_file_stream(
        self,
        pr_data: PRData,
        overall_context: str,
        mode: AnalysisMode,
        context_builder: ContextBuilder,
    ):
        """
        逐文件深度分析的流式版本 — Python Generator

        与 analyze_per_file() 逻辑相同, 但通过 yield 逐事件推送结果,
        供 SSE (Server-Sent Events) 端点消费, 实现前端渐进渲染。

        每个 yield 项是一个 dict: {"event": "事件类型", "data": 数据}

        事件类型:
        - progress: 进度提示文字 (str)
        - summary:  变更总结结果 (dict, ChangeSummary.model_dump())
        - risk:     单个风险项 (dict, RiskItem.model_dump())
        - done:     分析完成, 含元信息

        Args:
            pr_data: PR 完整数据
            overall_context: 整体上下文 (用于变更总结)
            mode: 分析模式
            context_builder: 上下文构建器
        """
        start_time = time.time()
        self._total_tokens_used = 0

        # ── Stage 1: 变更总结 ──
        logger.info(f"[Stream] Stage 1 开始变更总结")
        yield {"event": "progress", "data": "正在分析 PR 变更总结..."}
        summary = self._run_summary_stage(overall_context, mode)
        yield {"event": "summary", "data": summary.model_dump()}
        logger.info(f"[Stream] Stage 1 完成, 风险等级={summary.risk_level}")

        # ── Stage 2: 逐文件风险识别 ──
        all_risks: list[RiskItem] = []
        if mode != AnalysisMode.TRIVIAL:
            logger.info(f"[Stream] Stage 2 开始逐文件风险识别")

            cross_hints = context_builder.extract_cross_file_dependencies(pr_data.files)
            sorted_files = context_builder.sort_files_by_importance(pr_data.files)
            files_to_analyze = [
                f for f in sorted_files
                if not context_builder.should_skip_file(f)
            ]

            # ── 模式限额 ──
            if mode == AnalysisMode.LARGE:
                files_to_analyze = files_to_analyze[:30]
            elif mode == AnalysisMode.SIMPLE:
                files_to_analyze = files_to_analyze[:5]

            file_count = len(files_to_analyze)

            yield {
                "event": "progress",
                "data": (
                    f"待分析 {file_count} 个文件 "
                    f"(跳过 {len(pr_data.files) - file_count} 个)"
                ),
            }

            # ── 逐文件分析, 每个文件分析完立即 yield 风险 ──
            for idx, file_change in enumerate(files_to_analyze):
                yield {
                    "event": "progress",
                    "data": f"正在分析 ({idx + 1}/{file_count}): {file_change.filename}",
                }

                logger.info(
                    f"[Stream] 分析文件 [{idx + 1}/{file_count}]: "
                    f"{file_change.filename}"
                )

                file_context = context_builder.build_file_context(
                    file_change, pr_data, cross_hints
                )

                try:
                    file_risks = self._analyze_single_file(file_change, file_context)
                    for risk in file_risks:
                        # 单个风险立即推送, 前端可以逐个展示
                        yield {"event": "risk", "data": risk.model_dump()}
                    all_risks.extend(file_risks)
                    if file_risks:
                        logger.info(
                            f"  {file_change.filename}: +{len(file_risks)} 个风险"
                        )
                except Exception as e:
                    logger.warning(
                        f"文件 {file_change.filename} 分析失败, 跳过: {e}"
                    )

            # ── 最终过滤 ──
            all_risks = self._filter_by_mode(all_risks, mode)
            all_risks = self._deduplicate_risks(all_risks)
            all_risks = all_risks[:10]

        elapsed_ms = int((time.time() - start_time) * 1000)

        yield {
            "event": "progress",
            "data": "分析完成!",
        }

        # ── done 事件携带元信息 ──
        yield {
            "event": "done",
            "data": {
                "model": self.model,
                "tokens": self._total_tokens_used,
                "duration_ms": elapsed_ms,
                "risk_count": len(all_risks),
                "risk_level": summary.risk_level,
            },
        }

        logger.info(
            f"[Stream] 流式分析完成: "
            f"耗时={elapsed_ms}ms, "
            f"tokens={self._total_tokens_used}, "
            f"风险数={len(all_risks)}"
        )

    # ──────────────────────────────────────────────
    # Day 2 新增: 逐文件风险识别实现
    # ──────────────────────────────────────────────

    def _run_per_file_risk_stage(
        self,
        pr_data: PRData,
        mode: AnalysisMode,
        context_builder: ContextBuilder,
    ) -> list[RiskItem]:
        """
        逐文件执行风险识别

        步骤:
        1. 提取跨文件依赖提示
        2. 按重要性排序文件
        3. 跳过无分析价值的文件
        4. 根据模式限制分析文件数量
        5. 为每个文件构建独立上下文 → 调用 LLM → 收集风险
        6. 汇总、过滤、去重

        Args:
            pr_data: PR 数据
            mode: 分析模式
            context_builder: 上下文构建器

        Returns:
            聚合后的风险列表
        """
        # ── Step 1: 提取跨文件依赖提示 ──
        cross_hints = context_builder.extract_cross_file_dependencies(pr_data.files)

        # ── Step 2: 按重要性排序 ──
        sorted_files = context_builder.sort_files_by_importance(pr_data.files)

        # ── Step 3: 过滤掉无需分析的文件 ──
        files_to_analyze = [
            f for f in sorted_files
            if not context_builder.should_skip_file(f)
        ]
        logger.info(
            f"待分析文件: {len(files_to_analyze)}/{len(pr_data.files)} "
            f"(跳过 {len(pr_data.files) - len(files_to_analyze)} 个)"
        )

        # ── Step 4: 根据模式限制分析文件数 ──
        # large 模式只分析前 30 个重要文件, 防止耗时爆炸
        if mode == AnalysisMode.LARGE:
            files_to_analyze = files_to_analyze[:30]
        # simple 模式只分析前 5 个
        elif mode == AnalysisMode.SIMPLE:
            files_to_analyze = files_to_analyze[:5]

        # ── Step 5: 逐文件分析 ──
        all_risks: list[RiskItem] = []
        analyzed_count = 0
        file_count = len(files_to_analyze)

        for idx, file_change in enumerate(files_to_analyze):
            logger.info(
                f"分析文件 [{idx + 1}/{file_count}]: {file_change.filename}"
            )

            # 构建文件级上下文
            file_context = context_builder.build_file_context(
                file_change, pr_data, cross_hints
            )

            # 调用 LLM 分析
            try:
                file_risks = self._analyze_single_file(file_change, file_context)
                all_risks.extend(file_risks)
                analyzed_count += 1
                if file_risks:
                    logger.info(
                        f"  {file_change.filename}: 发现 {len(file_risks)} 个风险"
                    )
            except Exception as e:
                # 单个文件分析失败不应阻断整体流程
                logger.warning(
                    f"文件 {file_change.filename} 分析失败, 跳过: {e}"
                )
                continue

        logger.info(
            f"逐文件分析完成: {analyzed_count}/{file_count} 个文件分析成功, "
            f"累计风险 {len(all_risks)} 个"
        )

        # ── Step 6: 汇总、过滤、去重 ──
        all_risks = self._filter_by_mode(all_risks, mode)
        all_risks = self._deduplicate_risks(all_risks)
        all_risks = all_risks[:10]  # 总量上限

        return all_risks

    def _analyze_single_file(
        self, file_change: FileChange, file_context: str
    ) -> list[RiskItem]:
        """
        分析单个文件的变更风险

        调用 LLM 对单个文件的上下文进行分析, 并解析返回的风险列表。
        此方法被 _run_per_file_risk_stage 循环调用。

        Args:
            file_change: 文件变更信息
            file_context: 该文件的独立分析上下文

        Returns:
            该文件的风险项列表
        """
        user_prompt = (
            f"请审查以下文件中新增/修改的代码是否存在风险:\n\n"
            f"{file_context}"
        )

        raw_output = self._call_llm(SYSTEM_PROMPT_PER_FILE_RISK, user_prompt)
        parsed = self._safe_json_parse(raw_output)
        raw_risks = parsed.get("risks", [])

        if not isinstance(raw_risks, list):
            logger.warning(
                f"{file_change.filename}: 风险识别返回格式异常, 已忽略"
            )
            return []

        risks = []
        for item in raw_risks:
            try:
                risk = RiskItem(
                    severity=item.get("severity", "P3"),
                    category=item.get("category", ""),
                    # 强制使用当前文件的路径, 避免 LLM 返回错误的文件路径
                    file=file_change.filename,
                    line_range=item.get("line_range", ""),
                    title=item.get("title", ""),
                    description=item.get("description", ""),
                    suggestion=item.get("suggestion", ""),
                    code_snippet=item.get("code_snippet", ""),
                    confidence=float(item.get("confidence", 0.5)),
                )
                risks.append(risk)
            except Exception as e:
                logger.warning(
                    f"解析风险项失败: {e}, "
                    f"文件={file_change.filename}, "
                    f"原始数据: {str(item)[:200]}"
                )

        return risks

    # ──────────────────────────────────────────────
    # Stage 1: 变更总结
    # ──────────────────────────────────────────────

    def _run_summary_stage(
        self, context: str, mode: AnalysisMode
    ) -> ChangeSummary:
        """
        执行变更总结分析

        Args:
            context: 组装好的上下文
            mode: 分析模式

        Returns:
            ChangeSummary: 变更总结
        """
        if mode == AnalysisMode.TRIVIAL:
            system_prompt = SYSTEM_PROMPT_TRIVIAL
        else:
            system_prompt = SYSTEM_PROMPT_SUMMARY

        user_prompt = f"请分析以下 PR 变更:\n\n{context}"

        raw_output = self._call_llm(system_prompt, user_prompt)
        parsed = self._safe_json_parse(raw_output)

        return ChangeSummary(
            overview=parsed.get("overview", "无法生成总结"),
            key_changes=parsed.get("key_changes", []),
            affected_modules=parsed.get("affected_modules", []),
            risk_level=parsed.get("risk_level", "low"),
        )

    # ──────────────────────────────────────────────
    # Stage 2: 风险识别
    # ──────────────────────────────────────────────

    def _run_risk_stage(
        self,
        context: str,
        mode: AnalysisMode,
        pr_data: PRData,
    ) -> list[RiskItem]:
        """
        执行风险识别分析

        Args:
            context: 组装好的上下文
            mode: 分析模式
            pr_data: PR 数据 (用于上下文裁剪)

        Returns:
            风险项列表
        """
        user_prompt = f"请审查以下 PR 变更中的风险代码:\n\n{context}"

        raw_output = self._call_llm(SYSTEM_PROMPT_RISK, user_prompt)
        parsed = self._safe_json_parse(raw_output)
        raw_risks = parsed.get("risks", [])

        if not isinstance(raw_risks, list):
            logger.warning("风险识别返回格式异常, 已忽略")
            return []

        risks = []
        for item in raw_risks:
            try:
                risk = RiskItem(
                    severity=item.get("severity", "P3"),
                    category=item.get("category", ""),
                    file=item.get("file", ""),
                    line_range=item.get("line_range", ""),
                    title=item.get("title", ""),
                    description=item.get("description", ""),
                    suggestion=item.get("suggestion", ""),
                    code_snippet=item.get("code_snippet", ""),
                    confidence=float(item.get("confidence", 0.5)),
                )
                risks.append(risk)
            except Exception as e:
                logger.warning(f"解析风险项失败: {e}, 原始数据: {item}")

        # ── 模式过滤 ──
        risks = self._filter_by_mode(risks, mode)

        # ── 去重与合并 ──
        risks = self._deduplicate_risks(risks)

        # ── 数量上限 ──
        risks = risks[:10]

        return risks

    # ──────────────────────────────────────────────
    # 结果过滤与后处理
    # ──────────────────────────────────────────────

    def _filter_by_mode(
        self, risks: list[RiskItem], mode: AnalysisMode
    ) -> list[RiskItem]:
        """
        根据分析模式过滤风险

        - simple: 只保留 P0
        - normal: 保留 P0 + P1 (且 confidence ≥ 0.6)
        - large:  只保留 Top 5 (P0 优先)
        """
        if mode == AnalysisMode.SIMPLE:
            risks = [r for r in risks if r.severity == "P0"]
        elif mode in (AnalysisMode.NORMAL, AnalysisMode.LARGE):
            risks = [
                r for r in risks
                if r.severity in ("P0", "P1") and r.confidence >= 0.65
            ]

        if mode == AnalysisMode.LARGE:
            risks.sort(
                key=lambda r: (
                    0 if r.severity == "P0" else 1,
                    -r.confidence,
                )
            )
            risks = risks[:5]

        return risks

    def _deduplicate_risks(self, risks: list[RiskItem]) -> list[RiskItem]:
        """
        风险去重: 相同标题 + 相同文件 + 相同分类的合并

        简单策略: 完全相同的 (title.lower(), file, category) 视为重复, 保留第一个。
        """
        seen = set()
        unique = []
        for risk in risks:
            key = (risk.title.lower(), risk.file, risk.category)
            if key not in seen:
                seen.add(key)
                unique.append(risk)
        return unique

    # ──────────────────────────────────────────────
    # LLM 调用封装
    # ──────────────────────────────────────────────

    def _call_llm(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """
        调用 LLM 并返回原始文本

        Args:
            system_prompt: 系统提示
            user_prompt: 用户提示

        Returns:
            LLM 原始输出文本

        Raises:
            RuntimeError: LLM 调用失败
        """
        try:
            logger.info(
                f"调用 LLM (model={self.model}), "
                f"system_prompt={len(system_prompt)} chars, "
                f"user_prompt={len(user_prompt)} chars"
            )

            response = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )

            content = response.choices[0].message.content or ""

            usage = response.usage
            if usage:
                self._total_tokens_used += usage.total_tokens
                logger.info(
                    f"LLM 调用完成: "
                    f"prompt_tokens={usage.prompt_tokens}, "
                    f"completion_tokens={usage.completion_tokens}, "
                    f"total_tokens={usage.total_tokens}"
                )

            return content

        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            raise RuntimeError(f"LLM 调用失败: {e}") from e

    # ──────────────────────────────────────────────
    # 工具方法
    # ──────────────────────────────────────────────

    @staticmethod
    def _safe_json_parse(raw: str) -> dict:
        """
        安全 JSON 解析

        处理 LLM 输出中常见的格式问题:
        - ```json ... ``` 包裹
        - 首尾多余空白/换行
        - 尾随逗号
        """
        if not raw:
            return {}

        text = raw.strip()

        # 去除 markdown 代码块标记
        if text.startswith("```"):
            lines = text.split("\n")
            if len(lines) > 1:
                # 去掉第一行 (```json 或 ```) 和最后一行 (```)
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
            text = "\n".join(lines)

        text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试提取第一个 {...} 或 [...]
        for start_char, end_char in [("{", "}"), ("[", "]")]:
            start = text.find(start_char)
            end = text.rfind(end_char)
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    continue

        logger.warning(f"无法解析 LLM 输出为 JSON: {raw[:200]}...")
        return {}
