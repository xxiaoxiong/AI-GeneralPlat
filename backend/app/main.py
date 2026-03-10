import os
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.database import init_db
from app.core.redis import init_redis, close_redis
from app.core.logging import setup_logging, get_logger
from app.api.v1 import auth, users, models, knowledge, workflows, app_market, audit, prompts, agents

logger = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── 初始化日志系统 ──
    setup_logging()
    logger.info("启动 AI-GeneralPlat v2.0 Agent 平台")

    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    os.makedirs(settings.CHROMA_PERSIST_DIR, exist_ok=True)
    os.makedirs(settings.LOG_DIR, exist_ok=True)
    os.makedirs(settings.MEMORY_DB_PATH, exist_ok=True)
    os.makedirs(settings.CHECKPOINT_DIR, exist_ok=True)

    # Import all models to ensure they are registered with Base.metadata before create_all
    import app.models  # noqa: F401
    await init_db()
    await init_redis()
    await _seed_initial_data()

    # ── 清理过期检查点 ──
    from app.services.agent.checkpoint import CheckpointManager
    CheckpointManager.cleanup_old_checkpoints(max_age_hours=24)

    logger.info("所有子系统初始化完成")
    yield

    # ── 关闭时清理 ──
    from app.services.inference import ModelManager
    await ModelManager.get_instance().cleanup_idle(max_idle_seconds=0)
    await close_redis()
    logger.info("服务已关闭")


async def _seed_initial_data():
    from sqlalchemy import select
    from app.core.database import AsyncSessionLocal
    from app.core.security import get_password_hash
    from app.models.user import User, Role, Permission, UserRole, RolePermission

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.username == settings.ADMIN_USERNAME))
        if result.scalar_one_or_none():
            return

        admin_role = Role(name="admin", display_name="管理员", is_system=True,
                          description="系统管理员，拥有所有权限")
        user_role = Role(name="user", display_name="普通用户", is_system=True,
                         description="普通用户")
        db.add_all([admin_role, user_role])
        await db.flush()

        admin = User(
            username=settings.ADMIN_USERNAME,
            email=settings.ADMIN_EMAIL,
            hashed_password=get_password_hash(settings.ADMIN_PASSWORD),
            full_name="系统管理员",
            is_active=True,
            is_superuser=True,
        )
        db.add(admin)
        await db.flush()

        db.add(UserRole(user_id=admin.id, role_id=admin_role.id))

        await _seed_app_templates(db)
        await db.commit()


async def _seed_app_templates(db):
    from app.models.app_market import AppTemplate

    templates = [
        {"name": "smart_contract_review", "display_name": "智能合同审查", "category": "legal",
         "app_type": "chat", "icon": "📄", "sort_order": 1, "tags": "合同,法务,风控",
         "description": "上传合同文件，AI自动识别风险条款、合规问题，输出风险评估报告",
         "system_prompt": "你是一位专业的法律顾问，擅长合同审查和风险识别。请仔细分析用户提供的合同内容，识别潜在风险条款、不合规内容，并给出专业建议。"},
        {"name": "financial_analysis", "display_name": "财税风控分析", "category": "finance",
         "app_type": "chat", "icon": "💰", "sort_order": 2, "tags": "财务,税务,风控",
         "description": "智能分析财务报表，识别财税风险，辅助财务决策",
         "system_prompt": "你是一位资深财税专家，擅长财务报表分析、税务筹划和风险识别。请帮助用户分析财务数据，识别潜在风险。"},
        {"name": "smart_recruitment", "display_name": "智能招聘助手", "category": "hr",
         "app_type": "chat", "icon": "👥", "sort_order": 3, "tags": "招聘,HR,简历",
         "description": "智能筛选简历，生成面试问题，辅助招聘决策",
         "system_prompt": "你是一位专业的HR顾问，擅长简历筛选、面试评估和人才管理。请帮助用户进行招聘相关工作。"},
        {"name": "bid_analysis", "display_name": "招投标分析", "category": "procurement",
         "app_type": "chat", "icon": "📊", "sort_order": 4, "tags": "招标,投标,采购",
         "description": "分析招标文件，提取关键要求，辅助投标决策",
         "system_prompt": "你是一位采购和招投标专家，请帮助用户分析招标文件，提取关键信息，评估投标可行性。"},
        {"name": "marketing_copywriter", "display_name": "营销文案生成", "category": "marketing",
         "app_type": "chat", "icon": "✍️", "sort_order": 5, "tags": "营销,文案,广告",
         "description": "根据产品信息快速生成高质量营销文案、广告语、推广内容",
         "system_prompt": "你是一位资深营销文案专家，擅长撰写吸引人的营销文案、广告语和推广内容。请根据用户需求创作高质量文案。"},
        {"name": "intelligent_customer_service", "display_name": "智能客服机器人", "category": "service",
         "app_type": "chat", "icon": "🤖", "sort_order": 6, "tags": "客服,问答,支持",
         "description": "基于企业知识库的智能客服，自动回答客户问题",
         "system_prompt": "你是一位专业的客服助手，请根据知识库内容准确回答客户问题，保持友好、专业的态度。"},
        {"name": "meeting_minutes", "display_name": "会议纪要生成", "category": "office",
         "app_type": "chat", "icon": "📝", "sort_order": 7, "tags": "会议,纪要,办公",
         "description": "输入会议内容，自动生成结构化会议纪要",
         "system_prompt": "你是一位专业的会议记录助手，请将用户提供的会议内容整理成结构清晰的会议纪要，包括会议主题、参与人员、讨论要点、决议事项和后续行动。"},
        {"name": "policy_interpreter", "display_name": "政策法规解读", "category": "compliance",
         "app_type": "chat", "icon": "⚖️", "sort_order": 8, "tags": "政策,法规,合规",
         "description": "快速解读政策法规，提供合规建议",
         "system_prompt": "你是一位政策法规专家，请帮助用户解读相关政策法规，提供专业的合规建议和操作指引。"},
        {"name": "code_assistant", "display_name": "代码开发助手", "category": "tech",
         "app_type": "chat", "icon": "💻", "sort_order": 9, "tags": "代码,开发,技术",
         "description": "智能代码补全、审查、重构，提升开发效率",
         "system_prompt": "你是一位资深软件工程师，擅长多种编程语言。请帮助用户编写、审查、优化代码，解答技术问题。"},
        {"name": "training_assistant", "display_name": "员工培训助手", "category": "hr",
         "app_type": "chat", "icon": "🎓", "sort_order": 10, "tags": "培训,学习,知识",
         "description": "基于企业知识库的智能培训助手，帮助员工快速学习",
         "system_prompt": "你是一位专业的培训讲师，请根据知识库内容帮助员工学习和理解相关知识，提供清晰易懂的解释和示例。"},
    ]

    for t in templates:
        db.add(AppTemplate(**t, is_builtin=True, is_active=True))


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="企业级 AI 通用能力大平台与业务落地平台",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"code": 500, "message": "服务器内部错误", "detail": str(exc)},
    )


API_PREFIX = "/api/v1"
app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(users.router, prefix=API_PREFIX)
app.include_router(models.router, prefix=API_PREFIX)
app.include_router(knowledge.router, prefix=API_PREFIX)
app.include_router(workflows.router, prefix=API_PREFIX)
app.include_router(app_market.router, prefix=API_PREFIX)
app.include_router(audit.router, prefix=API_PREFIX)
app.include_router(prompts.router, prefix=API_PREFIX)
app.include_router(agents.router, prefix=API_PREFIX)


@app.get("/api/health")
async def health_check():
    return {"status": "ok", "version": settings.APP_VERSION}
