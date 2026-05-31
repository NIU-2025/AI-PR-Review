"""
AI PR Review 助手 - 服务入口

FastAPI 应用主文件, 负责:
- 创建 FastAPI 实例
- 配置 CORS 和日志
- 挂载路由
- 提供启动入口
"""

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import load_config
from routers.review import router as review_router

# ── 日志配置 ──

LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "logger")
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FORMAT = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(LOG_FORMAT)

file_handler = TimedRotatingFileHandler(
    os.path.join(LOG_DIR, "app.log"),
    when="midnight",
    interval=1,
    backupCount=7,
    encoding="utf-8",
)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(LOG_FORMAT)

logging.basicConfig(
    level=logging.INFO,
    handlers=[console_handler, file_handler],
)

logger = logging.getLogger(__name__)

# ── 应用实例 ──

config = load_config()

app = FastAPI(
    title="AI PR Review",
    description="基于大语言模型的 GitHub Pull Request 代码评审助手",
    version="0.1.0",
)

# ── CORS 配置 ──

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 路由注册 ──

app.include_router(review_router)


# ── 静态文件托管 ──

import os
from fastapi.responses import FileResponse

frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")

if os.path.isdir(frontend_dir):
    app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

    @app.get("/")
    async def serve_index():
        """托管前端首页"""
        return FileResponse(os.path.join(frontend_dir, "index.html"))


# ── 启动事件 ──

@app.on_event("startup")
async def startup_event():
    logger.info("=" * 50)
    logger.info("AI PR Review 助手启动")
    logger.info(f"LLM Model: {config.llm.model_name}")
    logger.info(f"GitHub API: {config.github.api_base_url}")
    if not config.llm.api_key:
        logger.warning("⚠️ LLM_API_KEY 未配置！请在 .env 文件中填入您的 API Key，否则分析功能将不可用。")
    logger.info("=" * 50)


# ── 直接运行入口 ──

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=config.host,
        port=config.port,
        reload=True,
        log_level="info",
    )
