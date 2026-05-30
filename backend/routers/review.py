"""
Review API 路由

提供 PR Review 的核心 API 端点:
- POST /api/review  - 提交 PR URL 进行 Review
- GET  /api/health   - 健康检查
"""

import json
import logging
import sys
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from config import AppConfig, load_config
from models.schemas import PRReviewRequest, PRReviewResponse, ChangeSummary, RiskItem, AnalysisResult
from services.github_service import GitHubAPIError, GitHubService
from services.llm_service import LLMService
from utils.context_builder import ContextBuilder
from utils.file_storage import ResultStorage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["review"])


# ──────────────────────────────────────────────
# 依赖注入: 全局服务实例 (简单单例)
# ──────────────────────────────────────────────

_app_config: AppConfig | None = None
_github_service: GitHubService | None = None
_llm_service: LLMService | None = None
_context_builder: ContextBuilder | None = None
_result_storage: ResultStorage | None = None


def _init_services():
    """延迟初始化服务实例"""
    global _app_config, _github_service, _llm_service, _context_builder, _result_storage
    if _app_config is None:
        _app_config = load_config()
        _github_service = GitHubService(
            token=_app_config.github.api_token,
            base_url=_app_config.github.api_base_url,
            timeout=_app_config.github.request_timeout,
            max_retries=_app_config.github.max_retries,
        )
        _llm_service = LLMService(config=_app_config.llm)
        _context_builder = ContextBuilder(config=_app_config.context)
        _result_storage = ResultStorage(config=_app_config.storage)
        logger.info("服务实例初始化完成")


def get_github_service() -> GitHubService:
    _init_services()
    return _github_service  # type: ignore


def get_llm_service() -> LLMService:
    _init_services()
    return _llm_service  # type: ignore


def get_context_builder() -> ContextBuilder:
    _init_services()
    return _context_builder  # type: ignore


def get_result_storage() -> ResultStorage:
    _init_services()
    return _result_storage  # type: ignore


# ──────────────────────────────────────────────
# API 端点
# ──────────────────────────────────────────────

@router.get("/health")
async def health_check():
    """健康检查端点"""
    return {"status": "ok", "timestamp": time.time()}


@router.post("/review", response_model=PRReviewResponse)
async def review_pr(
    request: PRReviewRequest,
    github: GitHubService = Depends(get_github_service),
    llm: LLMService = Depends(get_llm_service),
    context_builder: ContextBuilder = Depends(get_context_builder),
    storage: ResultStorage = Depends(get_result_storage),
):
    """
    PR Review 核心端点
    """
    print(f"[review_pr] >>> 入口, URL={request.pr_url}", flush=True)
    logger.info(f"[review_pr] >>> 入口, URL={request.pr_url}")
    start_time = time.time()

    # ── Step 1: 拉取 PR 数据 ──
    try:
        pr_data = github.fetch_pr_data(request.pr_url)
        print(f"[review_pr] Step1 OK: {pr_data.total_files} files", flush=True)
        logger.info(f"[review_pr] Step1 OK: {pr_data.total_files} files")
    except ValueError as e:
        print(f"[review_pr] Step1 ValueError: {e}", flush=True)
        raise HTTPException(status_code=400, detail=str(e))
    except GitHubAPIError as e:
        print(f"[review_pr] Step1 GitHubAPIError: {e}", flush=True)
        raise HTTPException(
            status_code=e.status_code or 502,
            detail=str(e),
        )

    # ── Step 1.5: 空 PR 拦截 ──
    has_changes = context_builder.has_meaningful_changes(pr_data)
    print(f"[review_pr] has_meaningful_changes={has_changes}", flush=True)
    if not has_changes:
        logger.info(f"PR 无有效代码变更, 跳过分析")
        return PRReviewResponse(
            success=True,
            pr_url=request.pr_url,
            pr_metadata=pr_data.metadata,
            analysis=None,
            error=(
                "该 PR 不包含有效的代码变更（可能仅包含二进制文件、图片或空文件变更），无需分析。"
            ),
        )

    # ── Step 2: 判定分析模式 & 构建上下文 ──
    mode = context_builder.determine_mode(pr_data)
    print(f"[review_pr] Step2 mode={mode.value}", flush=True)

    if mode.value == "trivial":
        context = context_builder.build_trivial_context(pr_data)
    else:
        context = context_builder.build_context(pr_data)

    # ── Step 3: LLM 分析 ──
    print(f"[review_pr] Step3 开始 LLM 分析...", flush=True)
    try:
        if mode.value == "trivial":
            analysis = llm.analyze(pr_data, context, mode)
        else:
            analysis = llm.analyze_per_file(pr_data, context, mode, context_builder)
        print(f"[review_pr] Step3 OK: risks={len(analysis.risks)}, tokens={analysis.token_used}", flush=True)
    except RuntimeError as e:
        print(f"[review_pr] Step3 RuntimeError: {e}", flush=True)
        error_msg = _friendly_error(str(e))
        raise HTTPException(status_code=502, detail=error_msg)
    except Exception as e:
        print(f"[review_pr] Step3 未预期异常: {type(e).__name__}: {e}", flush=True)
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"LLM 分析异常: {e}")

    total_ms = int((time.time() - start_time) * 1000)
    logger.info(f"PR Review 完成, 总耗时: {total_ms}ms")

    # ── 保存分析结果到本地文件 ──
    print(f"[review_pr] Step4 准备保存, storage.enabled={storage.enabled}, dir={storage.results_dir}", flush=True)
    logger.info(f"[review_pr] Step4 准备保存: enabled={storage.enabled}, dir={storage.results_dir}")
    try:
        saved_path = storage.save_result(
            pr_url=request.pr_url,
            analysis=analysis,
            pr_metadata=pr_data.metadata,
            llm_model=llm.model,
            token_used=analysis.token_used,
            duration_ms=total_ms,
        )
        print(f"[review_pr] Step4 保存完成: {saved_path}", flush=True)
        logger.info(f"[review_pr] Step4 保存完成: {saved_path}")
    except Exception as e:
        import traceback
        print(f"[review_pr] Step4 保存异常: {e}", flush=True)
        traceback.print_exc()
        logger.error(f"[review_pr] Step4 保存异常: {e}", exc_info=True)

    return PRReviewResponse(
        success=True,
        pr_url=request.pr_url,
        pr_metadata=pr_data.metadata,
        analysis=analysis,
    )


# ──────────────────────────────────────────────
# Day 2 新增: SSE 流式端点
# ──────────────────────────────────────────────

@router.post("/review/stream")
async def review_pr_stream(
    request: PRReviewRequest,
    github: GitHubService = Depends(get_github_service),
    llm: LLMService = Depends(get_llm_service),
    context_builder: ContextBuilder = Depends(get_context_builder),
    storage: ResultStorage = Depends(get_result_storage),
):
    """
    PR Review 流式端点 (SSE)

    与 /api/review 分析逻辑相同, 但通过 Server-Sent Events 逐事件推送,
    前端可以渐进渲染分析进度, 大幅改善用户体验。

    事件流格式:
      event: progress
      data: 进度文字

      event: summary
      data: {"overview": "...", "key_changes": [...], ...}

      event: risk
      data: {"severity": "P1", "title": "...", "file": "...", ...}

      event: done
      data: {"model": "...", "tokens": 12345, "duration_ms": 8000, "risk_count": 3}

      event: error
      data: 错误信息
    """

    # ── Step 1: 拉取 PR 数据 (非流式, 因为这一步本身就很快) ──
    try:
        pr_data = github.fetch_pr_data(request.pr_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except GitHubAPIError as e:
        raise HTTPException(
            status_code=e.status_code or 502,
            detail=str(e),
        )

    # ── Step 1.5: 空 PR 拦截 ──
    if not context_builder.has_meaningful_changes(pr_data):
        logger.info(f"流式端点: PR 无有效代码变更, 跳过分析")

        async def empty_pr_generator():
            yield _format_sse("meta", {
                "title": pr_data.metadata.title,
                "author": pr_data.metadata.author,
                "base_branch": pr_data.metadata.base_branch,
                "head_branch": pr_data.metadata.head_branch,
                "total_files": pr_data.total_files,
                "total_additions": pr_data.total_additions,
                "total_deletions": pr_data.total_deletions,
            })
            yield _format_sse("progress", "该 PR 不包含有效的代码变更（可能仅包含二进制文件、图片或空文件变更），无需分析。")
            yield _format_sse("done", {
                "model": "",
                "tokens": 0,
                "duration_ms": 0,
                "risk_count": 0,
                "risk_level": "trivial",
            })

        return StreamingResponse(
            empty_pr_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    # ── Step 2: 判定分析模式 & 构建上下文 ──
    mode = context_builder.determine_mode(pr_data)

    if mode.value == "trivial":
        context = context_builder.build_trivial_context(pr_data)
    else:
        context = context_builder.build_context(pr_data)

    # ── Step 3: SSE 生成器 ──
    async def sse_event_generator():
        """
        SSE 事件流生成器

        包装 LLM 分析生成器, 将每个 yield 转换为符合 SSE 协议格式的字符串。
        同时在流开始时发送 PR 元信息, 在发生异常时发送 error 事件。
        """
        try:
            # ── 首先发送 PR 元信息 (前端可以立即展示 PR 标题/作者) ──
            yield _format_sse(
                "meta",
                {
                    "title": pr_data.metadata.title,
                    "author": pr_data.metadata.author,
                    "base_branch": pr_data.metadata.base_branch,
                    "head_branch": pr_data.metadata.head_branch,
                    "total_files": pr_data.total_files,
                    "total_additions": pr_data.total_additions,
                    "total_deletions": pr_data.total_deletions,
                },
            )

            # ── 流式分析 ──
            if mode.value == "trivial":
                # trivial 模式不使用逐文件分析, 用旧的非流式方法
                yield _format_sse("progress", "正在分析变更总结...")
                analysis = llm.analyze(pr_data, context, mode)
                yield _format_sse("summary", analysis.summary.model_dump())
                yield _format_sse(
                    "done",
                    {
                        "model": llm.model,
                        "tokens": analysis.token_used,
                        "duration_ms": analysis.analysis_duration_ms,
                        "risk_count": len(analysis.risks),
                        "risk_level": analysis.summary.risk_level,
                    },
                )

                # ── 保存分析结果到本地文件 ──
                storage.save_result(
                    pr_url=request.pr_url,
                    analysis=analysis,
                    pr_metadata=pr_data.metadata,
                    llm_model=llm.model,
                    token_used=analysis.token_used,
                    duration_ms=analysis.analysis_duration_ms,
                )
            else:
                # 逐文件分析 + 流式推送 → 流结束后统一保存
                captured_summary = None
                captured_risks = []
                captured_done = None

                for event_dict in llm.analyze_per_file_stream(
                    pr_data, context, mode, context_builder
                ):
                    if event_dict["event"] == "summary":
                        captured_summary = event_dict["data"]
                    elif event_dict["event"] == "risk":
                        captured_risks.append(event_dict["data"])
                    elif event_dict["event"] == "done":
                        captured_done = event_dict["data"]
                    yield _format_sse(event_dict["event"], event_dict["data"])

                # 流全部推送完毕后, 重建 AnalysisResult 并保存
                if captured_summary is not None and captured_done is not None:
                    try:
                        summary_obj = ChangeSummary(**captured_summary)
                        risk_objs = [RiskItem(**r) for r in captured_risks]
                        analysis_result = AnalysisResult(
                            summary=summary_obj,
                            risks=risk_objs,
                            llm_model=captured_done.get("model", llm.model),
                            token_used=captured_done.get("tokens", 0),
                            analysis_duration_ms=captured_done.get("duration_ms", 0),
                        )
                        print(f"[sse_stream] 流式分析完成, 正在保存结果...", flush=True)
                        logger.info(f"[sse_stream] 流式分析完成, 正在保存结果: risks={len(risk_objs)}, tokens={analysis_result.token_used}")
                        saved = storage.save_result(
                            pr_url=request.pr_url,
                            analysis=analysis_result,
                            pr_metadata=pr_data.metadata,
                            llm_model=analysis_result.llm_model,
                            token_used=analysis_result.token_used,
                            duration_ms=analysis_result.analysis_duration_ms,
                        )
                        print(f"[sse_stream] 保存完成: {saved}", flush=True)
                        logger.info(f"[sse_stream] 保存完成: {saved}")
                    except Exception as e:
                        import traceback
                        print(f"[sse_stream] 保存异常: {e}", flush=True)
                        traceback.print_exc()
                        logger.error(f"[sse_stream] 保存异常: {e}", exc_info=True)

        except RuntimeError as e:
            logger.error(f"SSE 流分析失败: {e}")
            yield _format_sse("error", _friendly_error(str(e)))
        except Exception as e:
            logger.error(f"SSE 流分析异常: {e}", exc_info=True)
            yield _format_sse("error", f"系统内部异常: {e}")

    return StreamingResponse(
        sse_event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用 Nginx 缓冲
        },
    )


def _friendly_error(raw_error: str) -> str:
    """
    将底层技术错误转换为用户友好的中文提示

    处理常见的 LLM API 错误:
    - Connection error → 网络/API 地址配置问题
    - Authentication error → API Key 错误
    - Rate limit → 速率限制
    - Timeout → 超时

    Args:
        raw_error: 原始错误字符串

    Returns:
        友好的中文错误提示
    """
    error_lower = raw_error.lower()

    if "connection" in error_lower or "connect" in error_lower:
        return (
            "无法连接到 LLM API 服务。"
            "请检查: 1) 网络连接是否正常 "
            "2) .env 中的 LLM_API_BASE 地址是否正确 "
            "3) API 服务是否可用"
        )
    if "auth" in error_lower or "401" in raw_error or "unauthorized" in error_lower:
        return (
            "LLM API 认证失败。"
            "请检查 .env 中的 LLM_API_KEY 是否正确且未过期"
        )
    if "rate" in error_lower or "429" in raw_error or "limit" in error_lower:
        return (
            "LLM API 调用次数已达上限。"
            "请稍后重试或更换 API Key"
        )
    if "timeout" in error_lower or "timed out" in error_lower:
        return (
            "LLM API 响应超时。"
            "PR 文件较多或网络较慢时可能出现，请稍后重试"
        )
    if "404" in raw_error:
        return (
            "LLM API 端点不存在。"
            "请检查 .env 中的 LLM_API_BASE 和 LLM_MODEL 是否正确"
        )

    return f"LLM 分析失败: {raw_error}"


def _format_sse(event: str, data) -> str:
    """
    将事件和数据格式化为 SSE 协议字符串

    SSE 格式:
      event: {事件类型}\n
      data: {JSON数据}\n
      \n

    Args:
        event: 事件类型 (progress / summary / risk / done / meta / error)
        data: 数据内容, str 或 dict

    Returns:
        SSE 格式的字符串
    """
    if isinstance(data, str):
        data_str = data
    else:
        data_str = json.dumps(data, ensure_ascii=False)

    return f"event: {event}\ndata: {data_str}\n\n"
