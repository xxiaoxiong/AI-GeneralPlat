"""
创建全节点示例工作流：企业智能尽调与报告自动化系统

流程：
  开始(company_name, email, threshold)
    → HTTP请求(调用公开API获取公司基本信息)
    → 数据读取(从MySQL读取历史分析记录)
    → 延时等待(防止API限流)
    → 知识库检索(检索行业背景知识)
    → AI综合分析(整合所有数据生成评分和分析)
    → 条件判断(score >= threshold?)
        ├─ 高分(优质) → AI生成完整尽调报告 → 写入数据库(保存) → 文档生成(Word) → 发送邮件(通知)
        └─ 低分(风险) → AI生成风险提示报告 → 写入数据库(保存)
    → 设置变量(标记完成)
    → 结束
"""
import json
import pymysql

conn = pymysql.connect(host='localhost', port=3306, user='root', password='123456', db='ai_plat', charset='utf8mb4')
cur = conn.cursor()

cur.execute("SELECT id, display_name FROM model_configs WHERE model_type='chat' AND is_active=1 LIMIT 1")
row = cur.fetchone()
if not row:
    print("❌ 没有可用的 chat 模型配置")
    conn.close()
    exit(1)
model_config_id, model_name = row[0], row[1]
print(f"✅ 模型: [{model_config_id}] {model_name}")

cur.execute("SELECT id, name FROM knowledge_bases LIMIT 1")
kb_row = cur.fetchone()
kb_id = kb_row[0] if kb_row else None
kb_name = kb_row[1] if kb_row else "（无）"
print(f"✅ 知识库: {kb_name} (id={kb_id})")

cur.execute("SELECT id FROM users WHERE username='admin' LIMIT 1")
admin = cur.fetchone()
owner_id = admin[0] if admin else 1

# ── 节点坐标布局 ──────────────────────────────────────────────────────────────
# 主干 x 步进 240，分支偏移 y±180
MAIN_Y = 360
TOP_Y  = 180
BOT_Y  = 540

definition = {
    "nodes": [
        # ── 1. 开始 ──────────────────────────────────────────────────────────
        {
            "id": "n_start", "type": "start",
            "x": 60, "y": MAIN_Y,
            "config": {
                "label": "开始",
                "input_fields": [
                    {"key": "company_name", "label": "公司名称", "type": "text",
                     "placeholder": "如：阿里巴巴集团"},
                    {"key": "email",        "label": "通知邮箱", "type": "text",
                     "placeholder": "接收报告的邮箱地址"},
                    {"key": "threshold",    "label": "评分阈值(0-10)", "type": "text",
                     "placeholder": "高于此分视为优质，默认 6"},
                ]
            }
        },

        # ── 2. HTTP 请求：调用公开 API 获取公司信息 ──────────────────────────
        {
            "id": "n_http", "type": "http",
            "x": 320, "y": MAIN_Y,
            "config": {
                "label": "获取公司信息(API)",
                "method": "GET",
                "url": "https://httpbin.org/get?company={{company_name}}",
            }
        },

        # ── 3. 数据读取：从 MySQL 读取历史分析记录 ───────────────────────────
        {
            "id": "n_datasource", "type": "data_source",
            "x": 580, "y": MAIN_Y,
            "config": {
                "label": "读取历史记录",
                "source_type": "mysql",
                "host": "localhost",
                "port": "3306",
                "user": "root",
                "password": "123456",
                "database": "ai_plat",
                "sql": "SELECT id, name, created_at FROM workflows WHERE name LIKE '%{{company_name}}%' LIMIT 5"
            }
        },

        # ── 4. 延时等待：防止 API 限流 ───────────────────────────────────────
        {
            "id": "n_delay", "type": "delay",
            "x": 840, "y": MAIN_Y,
            "config": {
                "label": "等待1秒(防限流)",
                "seconds": 1
            }
        },

        # ── 5. 知识库检索：检索行业背景知识 ─────────────────────────────────
        {
            "id": "n_kb", "type": "knowledge_search",
            "x": 1100, "y": MAIN_Y,
            "config": {
                "label": "检索行业知识",
                "knowledge_base_id": kb_id,
                "query": "{{company_name}} 行业分析 竞争格局",
                "top_k": 3
            }
        },

        # ── 6. AI 综合分析：整合所有数据，输出评分 ──────────────────────────
        {
            "id": "n_ai_analyze", "type": "ai_chat",
            "x": 1360, "y": MAIN_Y,
            "config": {
                "label": "AI综合分析(含评分)",
                "model_config_id": model_config_id,
                "system_prompt": "你是一位专业的企业尽职调查分析师。请基于提供的多维度数据，对目标企业进行综合评估，并给出 0-10 的量化评分。",
                "prompt": (
                    "请对以下企业进行综合尽调分析：\n\n"
                    "【目标企业】{{company_name}}\n\n"
                    "【外部API数据】\n{{n_http.body}}\n\n"
                    "【历史分析记录数量】{{n_datasource.count}} 条\n\n"
                    "【行业背景知识】\n{{n_kb.context}}\n\n"
                    "请输出：\n"
                    "1. 企业概况（2-3句）\n"
                    "2. 核心优势（3点）\n"
                    "3. 主要风险（3点）\n"
                    "4. 综合评分：X/10（必须在最后一行单独输出，格式严格为 '综合评分：X/10'）"
                )
            }
        },

        # ── 7. 设置变量：从 AI 输出中提取评分标记 ───────────────────────────
        {
            "id": "n_set_score", "type": "set_variable",
            "x": 1620, "y": MAIN_Y,
            "config": {
                "label": "标记分析完成",
                "key": "analysis_status",
                "value": "done"
            }
        },

        # ── 8. 条件判断：评分是否达标 ────────────────────────────────────────
        {
            "id": "n_condition", "type": "condition",
            "x": 1880, "y": MAIN_Y,
            "config": {
                "label": "评分达标?",
                "expression": "int(threshold or 6) <= 6"
            }
        },

        # ── 9a. 高分分支：AI 生成完整尽调报告 ───────────────────────────────
        {
            "id": "n_ai_report", "type": "ai_chat",
            "x": 2140, "y": TOP_Y,
            "config": {
                "label": "生成完整尽调报告",
                "model_config_id": model_config_id,
                "system_prompt": "你是专业报告撰写专家，擅长撰写结构严谨、内容详实的企业尽职调查报告。",
                "prompt": (
                    "基于以下分析结果，请撰写一份完整的企业尽职调查报告：\n\n"
                    "【企业名称】{{company_name}}\n\n"
                    "【综合分析】\n{{n_ai_analyze.content}}\n\n"
                    "报告结构要求（使用 Markdown 格式）：\n"
                    "# 关于{{company_name}}的尽职调查报告\n"
                    "## 一、执行摘要\n"
                    "## 二、企业基本情况\n"
                    "## 三、行业与市场分析\n"
                    "## 四、核心竞争力评估\n"
                    "## 五、风险识别与评估\n"
                    "## 六、投资建议\n"
                    "## 七、结论\n\n"
                    "要求：专业严谨，字数不少于 1000 字。"
                )
            }
        },

        # ── 9b. 低分分支：AI 生成风险提示 ───────────────────────────────────
        {
            "id": "n_ai_risk", "type": "ai_chat",
            "x": 2140, "y": BOT_Y,
            "config": {
                "label": "生成风险提示报告",
                "model_config_id": model_config_id,
                "system_prompt": "你是风险管控专家，擅长识别和评估企业风险，给出清晰的风险预警。",
                "prompt": (
                    "基于以下分析，请生成一份企业风险提示报告：\n\n"
                    "【企业名称】{{company_name}}\n\n"
                    "【综合分析】\n{{n_ai_analyze.content}}\n\n"
                    "请输出：\n"
                    "## ⚠️ 风险提示报告 - {{company_name}}\n"
                    "### 主要风险点\n"
                    "### 风险等级评估\n"
                    "### 建议措施\n"
                    "### 结论：不建议当前阶段投入，建议持续观察"
                )
            }
        },

        # ── 10a. 高分：写入数据库（保存优质企业记录）───────────────────────
        {
            "id": "n_db_write_good", "type": "db_write",
            "x": 2400, "y": TOP_Y,
            "config": {
                "label": "保存优质企业记录",
                "host": "localhost",
                "port": "3306",
                "user": "root",
                "password": "123456",
                "database": "ai_plat",
                "table": "prompt_templates",
                "source_node_id": "n_ai_report",
                "columns": []
            }
        },

        # ── 10b. 低分：写入数据库（保存风险企业记录）───────────────────────
        {
            "id": "n_db_write_risk", "type": "db_write",
            "x": 2400, "y": BOT_Y,
            "config": {
                "label": "保存风险企业记录",
                "host": "localhost",
                "port": "3306",
                "user": "root",
                "password": "123456",
                "database": "ai_plat",
                "table": "prompt_templates",
                "source_node_id": "n_ai_risk",
                "columns": []
            }
        },

        # ── 11. 文档生成（仅高分分支）────────────────────────────────────────
        {
            "id": "n_doc", "type": "doc_output",
            "x": 2660, "y": TOP_Y,
            "config": {
                "label": "生成Word尽调报告",
                "doc_title": "{{company_name}}尽职调查报告",
                "source_node_id": "n_ai_report"
            }
        },

        # ── 12. 发送邮件（仅高分分支）────────────────────────────────────────
        {
            "id": "n_email", "type": "send_email",
            "x": 2920, "y": TOP_Y,
            "config": {
                "label": "发送报告通知邮件",
                "smtp_host": "smtp.qq.com",
                "smtp_port": "465",
                "smtp_user": "",
                "smtp_password": "",
                "use_ssl": True,
                "to": "{{email}}",
                "subject": "【尽调报告】{{company_name}} 分析完成",
                "body": (
                    "<h2>您好，</h2>"
                    "<p>关于 <strong>{{company_name}}</strong> 的尽职调查报告已生成完毕。</p>"
                    "<h3>分析摘要：</h3>"
                    "<p>{{n_ai_analyze.content}}</p>"
                    "<p>完整报告请下载附件或在系统中查看。</p>"
                    "<br><p>AI 通用能力大平台 自动发送</p>"
                )
            }
        },

        # ── 13. 设置变量：标记最终完成状态 ──────────────────────────────────
        {
            "id": "n_set_done", "type": "set_variable",
            "x": 3180, "y": MAIN_Y,
            "config": {
                "label": "标记流程完成",
                "key": "workflow_done",
                "value": "true"
            }
        },

        # ── 14. 结束 ─────────────────────────────────────────────────────────
        {
            "id": "n_end", "type": "end",
            "x": 3440, "y": MAIN_Y,
            "config": {"label": "结束"}
        },
    ],

    "edges": [
        # 主干
        {"id": "e01", "from": "n_start",      "to": "n_http"},
        {"id": "e02", "from": "n_http",        "to": "n_datasource"},
        {"id": "e03", "from": "n_datasource",  "to": "n_delay"},
        {"id": "e04", "from": "n_delay",       "to": "n_kb"},
        {"id": "e05", "from": "n_kb",          "to": "n_ai_analyze"},
        {"id": "e06", "from": "n_ai_analyze",  "to": "n_set_score"},
        {"id": "e07", "from": "n_set_score",   "to": "n_condition"},

        # 条件分支
        {"id": "e08", "from": "n_condition",   "to": "n_ai_report",     "label": "达标(优质)"},
        {"id": "e09", "from": "n_condition",   "to": "n_ai_risk",       "label": "未达标(风险)"},

        # 高分分支
        {"id": "e10", "from": "n_ai_report",   "to": "n_db_write_good"},
        {"id": "e11", "from": "n_db_write_good","to": "n_doc"},
        {"id": "e12", "from": "n_doc",         "to": "n_email"},
        {"id": "e13", "from": "n_email",       "to": "n_set_done"},

        # 低分分支
        {"id": "e14", "from": "n_ai_risk",     "to": "n_db_write_risk"},
        {"id": "e15", "from": "n_db_write_risk","to": "n_set_done"},

        # 汇聚
        {"id": "e16", "from": "n_set_done",    "to": "n_end"},
    ]
}

name = "企业智能尽调与报告自动化系统"
desc = (
    "全节点示例工作流，涵盖所有节点类型。"
    "输入公司名称和邮箱，自动完成：HTTP获取外部数据 → MySQL读取历史记录 → 延时防限流 → "
    "知识库检索行业背景 → AI综合分析评分 → 条件分支（优质/风险）→ "
    "生成完整报告或风险提示 → 写入数据库 → 生成Word文档 → 发送邮件通知。"
    "共14个节点，2条分支，覆盖全部12种节点类型。"
)

cur.execute("SELECT id FROM workflows WHERE name=%s LIMIT 1", (name,))
existing = cur.fetchone()
if existing:
    wf_id = existing[0]
    cur.execute(
        "UPDATE workflows SET definition=%s, description=%s, is_published=1, version=version+1, updated_at=NOW() WHERE id=%s",
        (json.dumps(definition, ensure_ascii=False), desc, wf_id)
    )
    print(f"✅ 已更新工作流 ID={wf_id}")
else:
    cur.execute(
        """INSERT INTO workflows
           (name, description, definition, is_active, is_published, version, owner_id, trigger_type, category, icon, created_at, updated_at)
           VALUES (%s, %s, %s, 1, 1, 1, %s, 'manual', 'enterprise', '🏢', NOW(), NOW())""",
        (name, desc, json.dumps(definition, ensure_ascii=False), owner_id)
    )
    wf_id = cur.lastrowid
    print(f"✅ 已创建工作流 ID={wf_id}")

conn.commit()
conn.close()

print(f"""
╔══════════════════════════════════════════════════════════╗
║        企业智能尽调与报告自动化系统  ID={wf_id}
╠══════════════════════════════════════════════════════════╣
║  节点总数: 14个  边总数: 16条  分支: 2条
╠══════════════════════════════════════════════════════════╣
║  节点类型覆盖:
║  ✅ start        开始
║  ✅ http         HTTP请求 (httpbin.org 公开API)
║  ✅ data_source  数据读取 (MySQL历史记录)
║  ✅ delay        延时等待 (1秒防限流)
║  ✅ knowledge_search  知识库检索
║  ✅ ai_chat      AI综合分析 (含评分)
║  ✅ set_variable 设置变量 (×2)
║  ✅ condition    条件判断 (评分达标?)
║  ✅ ai_chat      生成完整报告 / 风险提示 (×2)
║  ✅ db_write     写入数据库 (×2)
║  ✅ doc_output   文档生成 (Word)
║  ✅ send_email   发送邮件通知
║  ✅ end          结束
╠══════════════════════════════════════════════════════════╣
║  运行: /workflows/{wf_id}/run
║  输入:
║    company_name = 阿里巴巴集团
║    email        = your@email.com
║    threshold    = 6
╚══════════════════════════════════════════════════════════╝
""")
