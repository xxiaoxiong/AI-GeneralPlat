from pydantic_settings import BaseSettings
from typing import List, Optional
import json


class Settings(BaseSettings):
    APP_NAME: str = "AI-GeneralPlat"
    APP_VERSION: str = "2.0.0"
    DEBUG: bool = False
    SECRET_KEY: str = "change-me-in-production-min-32-chars-long!!"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    DATABASE_URL: str = "mysql+aiomysql://root:password@localhost:3308/ai_plat"
    DATABASE_SYNC_URL: str = "mysql+pymysql://root:password@localhost:3308/ai_plat"

    REDIS_URL: str = "redis://localhost:6379/0"

    CHROMA_PERSIST_DIR: str = "./data/chroma"

    UPLOAD_DIR: str = "./data/uploads"
    MAX_UPLOAD_SIZE_MB: int = 100

    ADMIN_EMAIL: str = "admin@example.com"
    ADMIN_PASSWORD: str = "Admin@123456"
    ADMIN_USERNAME: str = "admin"

    CORS_ORIGINS: str = '["http://localhost:3000","http://localhost:5173"]'

    # ── Agent 执行引擎配置 ────────────────────────────────────────────────────
    AGENT_MAX_ITERATIONS: int = 15              # 全局默认最大 ReAct 迭代
    AGENT_MAX_TOOL_RETRIES: int = 2             # 工具调用失败重试次数
    AGENT_TOOL_TIMEOUT_SECONDS: int = 30        # 单次工具执行超时
    AGENT_MAX_SAME_TOOL_CALLS: int = 3          # 同一工具最大连续调用次数
    AGENT_TEMPERATURE: float = 0.3              # 30B 模型建议低温度
    AGENT_ENABLE_SELF_VERIFY: bool = True       # 启用自我校验
    AGENT_ENABLE_SOURCE_CITATION: bool = True   # 启用来源引用

    # ── 推理层配置（30B 模型优化）──────────────────────────────────────────────
    INFERENCE_MAX_CONTEXT_TOKENS: int = 8192    # 最大上下文窗口 token 数
    INFERENCE_MAX_OUTPUT_TOKENS: int = 4096     # 最大输出 token 数
    INFERENCE_KV_CACHE_MAX_MB: int = 4096       # KV Cache 最大显存占用 MB
    INFERENCE_CONCURRENT_REQUESTS: int = 2      # 最大并发推理请求
    INFERENCE_REQUEST_QUEUE_SIZE: int = 20      # 请求队列大小
    INFERENCE_REQUEST_TIMEOUT: int = 120        # 单次推理请求超时秒数
    INFERENCE_BATCH_SIZE: int = 1               # 批处理大小

    # ── 上下文管理配置 ────────────────────────────────────────────────────────
    CONTEXT_SLIDING_WINDOW_TURNS: int = 6       # 滑动窗口保留最近 N 轮完整对话
    CONTEXT_MAX_HISTORY_TOKENS: int = 2048      # 历史信息蒸馏后最大 token 数
    CONTEXT_SUMMARY_THRESHOLD: int = 4096       # 超过此 token 数触发历史摘要
    CONTEXT_PIN_SYSTEM_PROMPT: bool = True       # 系统提示词永远置顶
    CONTEXT_PIN_ORIGINAL_TASK: bool = True       # 原始任务永不丢失

    # ── 记忆系统配置 ──────────────────────────────────────────────────────────
    MEMORY_SHORT_TERM_MAX_TURNS: int = 6        # 短期记忆保留完整轮数
    MEMORY_LONG_TERM_MAX_ITEMS: int = 200       # 长期记忆最大条目
    MEMORY_IMPORTANCE_THRESHOLD: float = 0.5    # 记忆重要性阈值
    MEMORY_AUTO_DISTILL: bool = True            # 自动蒸馏旧历史
    MEMORY_STRUCTURED_STORE: bool = True        # 启用结构化记忆（知识/规则/技能）
    MEMORY_CONFLICT_STRATEGY: str = "newer"     # 记忆冲突策略：newer / higher_importance / merge
    MEMORY_DB_PATH: str = "./data/memory"       # 记忆数据库路径

    # ── 幻觉抑制配置 ──────────────────────────────────────────────────────────
    HALLUCINATION_FACT_CHECK: bool = True       # 启用事实校验
    HALLUCINATION_MAX_VERIFY_ROUNDS: int = 2    # 重要结果验证轮数
    HALLUCINATION_FORCE_SOURCE: bool = True     # 强制来源引用
    HALLUCINATION_UNCERTAINTY_EXPR: bool = True  # 允许不确定性表达

    # ── 监控与日志配置 ────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "./data/logs"
    LOG_AGENT_TRACE: bool = True                # 记录 Agent 执行轨迹
    LOG_TOOL_CALLS: bool = True                 # 记录工具调用详情
    LOG_PERFORMANCE: bool = True                # 记录性能指标
    MONITOR_ENABLE: bool = True                 # 启用性能监控
    MONITOR_SLOW_THRESHOLD_MS: int = 5000       # 慢请求阈值

    # ── 稳定性配置 ────────────────────────────────────────────────────────────
    HOT_RELOAD_TOOLS: bool = True               # 工具热重载
    HOT_RELOAD_PROMPTS: bool = True             # 提示词热重载
    SESSION_ISOLATION: bool = True              # 多会话内存隔离
    DETERMINISTIC_MODE: bool = False            # 确定性模式（固定 seed）
    DETERMINISTIC_SEED: int = 42                # 确定性模式种子
    CHECKPOINT_ENABLED: bool = True             # 启用断点续跑
    CHECKPOINT_DIR: str = "./data/checkpoints"  # 断点存储目录

    @property
    def cors_origins_list(self) -> List[str]:
        return json.loads(self.CORS_ORIGINS)

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
