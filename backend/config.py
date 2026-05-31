"""
全局配置管理

支持从环境变量和 .env 文件加载配置
复制 .env.example 为 .env 并填入真实值
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

# 自动加载项目根目录下的 .env 文件
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parent.parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except ImportError:
    pass


@dataclass
class GitHubConfig:
    """GitHub API 相关配置"""
    api_token: str = ""
    api_base_url: str = "https://api.github.com"
    request_timeout: int = 30
    max_retries: int = 3


@dataclass
class LLMConfig:
    """LLM 调用相关配置"""
    api_key: str = ""
    api_base_url: str = "https://api.deepseek.com"
    model_name: str = "deepseek-v4-flash"
    max_tokens: int = 8192
    temperature: float = 0.1
    request_timeout: int = 120


@dataclass
class ContextConfig:
    """上下文构建相关配置"""
    max_context_tokens: int = 64000
    context_lines_around_hunk: int = 25
    max_files_for_full_analysis: int = 50
    min_files_for_trivial_mode: int = 1
    max_lines_for_trivial_mode: int = 10
    max_lines_for_simple_mode: int = 50
    max_files_for_simple_mode: int = 3
    max_risks_per_file: int = 3
    max_risks_per_pr: int = 10


@dataclass
class StorageConfig:
    """结果持久化相关配置"""
    results_dir: str = "reviews"
    auto_save_enabled: bool = True


@dataclass
class AppConfig:
    """应用全局配置"""
    github: GitHubConfig = field(default_factory=GitHubConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    cors_origins: list = field(default_factory=lambda: ["*"])
    host: str = "0.0.0.0"
    port: int = 8000


def load_config() -> AppConfig:
    """
    从环境变量加载配置

    优先级: 环境变量 > .env 文件 > 默认值
    """
    config = AppConfig()

    config.github.api_token = os.getenv("GITHUB_TOKEN", "")
    config.github.api_base_url = os.getenv("GITHUB_API_BASE", "https://api.github.com")

    config.llm.api_key = os.getenv("LLM_API_KEY", "")
    config.llm.api_base_url = os.getenv("LLM_API_BASE", "https://api.deepseek.com/v1")
    config.llm.model_name = os.getenv("LLM_MODEL", "deepseek-chat")
    config.llm.max_tokens = int(os.getenv("LLM_MAX_TOKENS", "4096"))
    config.llm.temperature = float(os.getenv("LLM_TEMPERATURE", "0.1"))

    config.host = os.getenv("HOST", "0.0.0.0")
    config.port = int(os.getenv("PORT", "8000"))

    return config
