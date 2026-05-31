"""
GitHub API 集成模块

负责与 GitHub REST API 交互, 获取 PR 的元信息和代码变更。
支持公开仓库 (无需 token) 和私有仓库 (需要 GITHUB_TOKEN)。

核心功能:
- 解析 PR URL, 提取 owner / repo / pr_number
- 获取 PR 元信息 (标题、描述、作者、分支等)
- 获取 PR 的文件变更列表 (含 unified diff patch)
- 处理分页、限流、错误等边界情况
"""

import re
import time
import logging
from typing import Optional
from urllib.parse import urlparse

import httpx

from models.schemas import FileChange, PRData, PRMetadata

logger = logging.getLogger(__name__)

# GitHub PR URL 解析正则
# 支持格式: https://github.com/{owner}/{repo}/pull/{number}
_PR_URL_PATTERN = re.compile(
    r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
)


class GitHubAPIError(Exception):
    """GitHub API 调用异常"""

    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class GitHubService:
    """GitHub API 服务封装"""

    def __init__(
        self,
        token: str = "",
        base_url: str = "https://api.github.com",
        timeout: int = 30,
        max_retries: int = 3,
    ):
        """
        初始化 GitHub 服务

        Args:
            token: GitHub Personal Access Token
                   公开仓库可不传, 但有 token 可以提高 rate limit (60→5000/小时)
            base_url: API 基础地址
            timeout: 单次请求超时秒数
            max_retries: 失败重试次数
        """
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries

        self._pr_etag = ""

        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "AI-PR-Review/1.0",
        }
        if token:
            headers["Authorization"] = f"token {token}"

        self._client = httpx.Client(headers=headers, timeout=timeout)

    # ──────────────────────────────────────────────
    # 公开方法
    # ──────────────────────────────────────────────

    def parse_pr_url(self, pr_url: str) -> tuple[str, str, int]:
        """
        解析 GitHub PR URL, 提取 owner, repo, pr_number

        Args:
            pr_url: PR 完整 URL

        Returns:
            (owner, repo, pr_number) 三元组

        Raises:
            ValueError: URL 格式不合法
        """
        pr_url = pr_url.strip().rstrip("/")

        # 尝试正则匹配
        match = _PR_URL_PATTERN.search(pr_url)
        if not match:
            raise ValueError(
                f"无法解析 PR URL: {pr_url}\n"
                f"期望格式: https://github.com/{{owner}}/{{repo}}/pull/{{number}}"
            )

        owner = match.group("owner")
        repo = match.group("repo")
        pr_number = int(match.group("number"))

        logger.info(f"解析 PR URL 成功: {owner}/{repo}#{pr_number}")
        return owner, repo, pr_number

    def fetch_pr_data(self, pr_url: str) -> PRData:
        """
        获取 PR 完整数据: 元信息 + 文件变更

        Args:
            pr_url: PR 完整 URL

        Returns:
            PRData: 包含元信息和文件变更的完整数据

        Raises:
            GitHubAPIError: API 调用失败
            ValueError: URL 解析失败
        """
        owner, repo, pr_number = self.parse_pr_url(pr_url)

        # 并行获取 PR 元信息和文件列表
        metadata = self._fetch_pr_metadata(owner, repo, pr_number)
        files = self._fetch_pr_files(owner, repo, pr_number)

        total_additions = sum(f.additions for f in files)
        total_deletions = sum(f.deletions for f in files)

        logger.info(
            f"PR #{pr_number} 数据获取完成: "
            f"{len(files)} 个文件, "
            f"+{total_additions} -{total_deletions}"
        )

        return PRData(
            metadata=metadata,
            files=files,
            total_files=len(files),
            total_additions=total_additions,
            total_deletions=total_deletions,
        )

    # ──────────────────────────────────────────────
    # 私有方法: 具体 API 调用
    # ──────────────────────────────────────────────

    def _fetch_pr_metadata(
        self, owner: str, repo: str, pr_number: int
    ) -> PRMetadata:
        """
        获取 PR 元信息
        GET /repos/{owner}/{repo}/pulls/{pull_number}
        """
        url = f"{self.base_url}/repos/{owner}/{repo}/pulls/{pr_number}"
        data, response = self._request_with_retry("GET", url)

        etag = response.headers.get("ETag", "")
        if etag:
            self._pr_etag = etag

        return PRMetadata(
            title=data.get("title", ""),
            description=data.get("body", "") or "",
            author=data.get("user", {}).get("login", "unknown"),
            base_branch=data.get("base", {}).get("ref", ""),
            head_branch=data.get("head", {}).get("ref", ""),
            commits_count=data.get("commits", 0),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )

    def _fetch_pr_files(
        self, owner: str, repo: str, pr_number: int
    ) -> list[FileChange]:
        """
        获取 PR 文件变更列表
        GET /repos/{owner}/{repo}/pulls/{pull_number}/files

        GitHub 默认每页 30 条, 最多 100 条/页。
        对于大 PR (超过 3000 个文件), 需要考虑分页和截断。
        """
        url = f"{self.base_url}/repos/{owner}/{repo}/pulls/{pr_number}/files"
        all_files: list[FileChange] = []
        page = 1
        max_pages = 10  # 最多取 10 页 (最多 3000 个文件), 超大 PR 截断

        while page <= max_pages:
            params = {"per_page": 100, "page": page}
            data, _response = self._request_with_retry("GET", url, params=params)

            if not isinstance(data, list) or len(data) == 0:
                break

            for item in data:
                all_files.append(
                    FileChange(
                        filename=item.get("filename", ""),
                        status=item.get("status", ""),
                        additions=item.get("additions", 0),
                        deletions=item.get("deletions", 0),
                        changes=item.get("changes", 0),
                        patch=item.get("patch", ""),
                        raw_url=item.get("raw_url", ""),
                    )
                )

            # 检查是否还有更多页
            if len(data) < 100:
                break
            page += 1

        logger.info(f"获取到 {len(all_files)} 个文件变更 (共 {page} 页)")

        # 超大 PR 截断警告
        if page > max_pages or (page == max_pages and len(all_files) >= max_pages * 100):
            logger.warning(
                f"PR 包含超过 {max_pages * 100} 个文件变更, "
                f"仅获取前 {len(all_files)} 个。"
                f"完整分析可能不准确。"
            )
        return all_files

    def _request_with_retry(
        self,
        method: str,
        url: str,
        params: Optional[dict] = None,
        extra_headers: Optional[dict] = None,
    ) -> "tuple[dict | list, httpx.Response]":
        """
        带重试的 HTTP 请求

        Args:
            method: HTTP 方法
            url: 请求 URL
            params: 查询参数
            extra_headers: 额外请求头 (如 If-None-Match)

        Returns:
            (解析后的 JSON 数据, httpx.Response 对象)

        Raises:
            GitHubAPIError: 请求失败
        """
        last_error: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                if extra_headers:
                    response = self._client.request(
                        method, url, params=params, headers=extra_headers
                    )
                else:
                    response = self._client.request(method, url, params=params)

                # 处理 rate limit
                if response.status_code == 403:
                    rate_limit_remaining = response.headers.get(
                        "X-RateLimit-Remaining", "0"
                    )
                    if rate_limit_remaining == "0":
                        reset_time = int(
                            response.headers.get("X-RateLimit-Reset", "0")
                        )
                        wait_seconds = max(reset_time - int(time.time()), 0) + 1
                        raise GitHubAPIError(
                            f"GitHub API 速率限制已用完。"
                            f"请在 {wait_seconds} 秒后重试, "
                            f"或配置 GITHUB_TOKEN 环境变量以提升限制 (60→5000/小时)。",
                            status_code=403,
                        )

                if response.status_code == 404:
                    raise GitHubAPIError(
                        f"PR 不存在或无权限访问: {url}\n"
                        f"请检查: 1) PR 编号是否正确 2) 仓库是否为公开 3) 私有仓库需配置 Token",
                        status_code=404,
                    )

                if response.status_code >= 400:
                    raise GitHubAPIError(
                        f"GitHub API 错误 ({response.status_code}): "
                        f"{response.text[:500]}",
                        status_code=response.status_code,
                    )

                return response.json(), response

            except GitHubAPIError:
                raise
            except httpx.TimeoutException:
                last_error = Exception(f"请求超时 (第 {attempt}/{self.max_retries} 次尝试)")
                logger.warning(str(last_error))
            except Exception as e:
                last_error = e
                logger.warning(f"请求异常 (第 {attempt}/{self.max_retries} 次尝试): {e}")

            if attempt < self.max_retries:
                wait = 2 ** attempt  # 指数退避: 2s, 4s, 8s
                logger.info(f"等待 {wait} 秒后重试...")
                time.sleep(wait)

        raise GitHubAPIError(
            f"请求失败, 已重试 {self.max_retries} 次: {last_error}"
        )

    @property
    def last_etag(self) -> str:
        """最近一次 fetch_pr_data 获取到的 PR 元数据端点 ETag (用于缓存校验)"""
        return self._pr_etag

    def check_pr_updated(
        self, owner: str, repo: str, pr_number: int, etag: str
    ) -> "tuple[bool, str]":
        """
        用 If-None-Match 头检查 PR 是否自上次缓存后发生变更

        向 GitHub API 发送轻量校验请求:
        - 若 PR 未变更 → GitHub 返回 304 Not Modified (不计入 Rate Limit)
        - 若 PR 已变更 → GitHub 返回 200 + 新 ETag
        """
        url = f"{self.base_url}/repos/{owner}/{repo}/pulls/{pr_number}"

        try:
            response = self._client.request(
                "GET", url, headers={"If-None-Match": etag}
            )

            if response.status_code == 304:
                logger.info(f"PR #{pr_number} 缓存验证通过: 304 Not Modified")
                return True, ""

            if response.status_code == 200:
                new_etag = response.headers.get("ETag", "")
                logger.info(f"PR #{pr_number} 已更新, 新 ETag={new_etag}")
                return False, new_etag

            if response.status_code == 404:
                raise GitHubAPIError(
                    f"PR 不存在或无权限访问: {url}",
                    status_code=404,
                )

            logger.warning(f"PR #{pr_number} 缓存校验异常: HTTP {response.status_code}")
            return False, ""

        except httpx.TimeoutException:
            logger.warning(f"PR #{pr_number} 缓存校验超时, 降级为重新分析")
            return False, ""

    def close(self):
        """关闭 HTTP 客户端"""
        self._client.close()
