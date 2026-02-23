"""
创建演示工作流：多维度竞品情报自动分析系统
使用节点：start, set_variable, parallel(内含http+web_crawl+ai_chat),
          code, loop(内含ai_chat), condition, ai_chat×2, doc_output, end
共 14 个节点（含子节点），覆盖所有主要节点类型。

运行输入：
  topic      = 要分析的主题，如"大模型应用平台"
  detail     = yes / no（是否生成完整报告）
"""
import json
import pymysql

conn = pymysql.connect(
    host='localhost', port=3306, user='root',
    password='123456', db='ai_plat', charset='utf8mb4'
)
cur = conn.cursor()

# ── 获取模型配置 ──────────────────────────────────────────────────────────────
cur.execute("SELECT id, display_name FROM model_configs WHERE model_type='chat' AND is_active=1 LIMIT 1")
row = cur.fetchone()
if not row:
    print("❌ 没有可用的 chat 模型配置")
    conn.close()
    exit(1)
model_config_id, model_name = row[0], row[1]
print(f"✅ 模型: [{model_config_id}] {model_name}")

# ── 获取管理员 ID ─────────────────────────────────────────────────────────────
cur.execute("SELECT id FROM users WHERE username='admin' LIMIT 1")
admin = cur.fetchone()
owner_id = admin[0] if admin else 1

# ══════════════════════════════════════════════════════════════════════════════
# 工作流定义
# ══════════════════════════════════════════════════════════════════════════════
definition = {
    "nodes": [

        # ── 1. 开始节点 ───────────────────────────────────────────────────────
        {
            "id": "n_start",
            "type": "start",
            "x": 60, "y": 320,
            "config": {
                "label": "开始",
                "description": "接收分析主题和报告详细程度",
                "input_fields": [
                    {"key": "topic",     "label": "分析主题",   "type": "text",   "placeholder": "如：大模型应用平台"},
                    {"key": "detail",    "label": "详细报告",   "type": "select", "options": "yes,no", "default": "yes"},
                    {"key": "competitors", "label": "竞品列表(逗号分隔)", "type": "text", "placeholder": "如：ChatGPT,文心一言,通义千问"}
                ]
            }
        },

        # ── 2. 设置变量：初始化分析参数 ───────────────────────────────────────
        {
            "id": "n_set_var",
            "type": "set_variable",
            "x": 280, "y": 320,
            "config": {
                "label": "初始化参数",
                "description": "设置分析时间戳和版本号",
                "key": "report_version",
                "value": "v1.0"
            }
        },

        # ── 3. 并行节点：同时执行3路数据采集 ─────────────────────────────────
        {
            "id": "n_parallel",
            "type": "parallel",
            "x": 500, "y": 320,
            "config": {
                "label": "并行数据采集",
                "description": "同时执行：HTTP获取行业数据 + 网页爬取参考资料 + AI生成分析框架",
                "sub_nodes": [
                    # 并行子节点1：HTTP请求获取公开数据
                    {
                        "id": "sub_http",
                        "type": "http",
                        "config": {
                            "label": "HTTP获取行业数据",
                            "url": "https://httpbin.org/json",
                            "method": "GET",
                            "headers": {"Accept": "application/json"}
                        }
                    },
                    # 并行子节点2：网页爬取
                    {
                        "id": "sub_crawl",
                        "type": "web_crawl",
                        "config": {
                            "label": "爬取参考资料",
                            "url": "https://httpbin.org/html",
                            "max_length": 2000
                        }
                    },
                    # 并行子节点3：AI生成分析框架
                    {
                        "id": "sub_ai_framework",
                        "type": "ai_chat",
                        "config": {
                            "label": "AI生成分析框架",
                            "model_config_id": model_config_id,
                            "system_prompt": "你是一位专业的市场分析师，擅长构建结构化分析框架。",
                            "prompt": "请为「{{topic}}」这一主题设计一个竞品分析框架，包含：\n1. 核心评估维度（5个）\n2. 数据收集要点\n3. 评分标准\n\n请用简洁的列表格式输出，每个维度不超过20字。"
                        }
                    }
                ]
            }
        },

        # ── 4. 代码执行：汇总并行结果，生成竞品列表 ─────────────────────────
        {
            "id": "n_code",
            "type": "code",
            "x": 740, "y": 320,
            "config": {
                "label": "处理数据生成竞品列表",
                "description": "解析输入的竞品字符串，生成结构化列表",
                "timeout": 5,
                "code": (
                    "# 从输入中解析竞品列表\n"
                    "raw = competitors if 'competitors' in dir() else 'ChatGPT,文心一言,通义千问'\n"
                    "items = [c.strip() for c in str(raw).split(',') if c.strip()]\n"
                    "if not items:\n"
                    "    items = ['ChatGPT', '文心一言', '通义千问']\n"
                    "# 限制最多3个，避免运行时间过长\n"
                    "items = items[:3]\n"
                    "result = {'competitors': items, 'count': len(items), 'topic': topic if 'topic' in dir() else '未知主题'}\n"
                    "print(f'竞品数量: {len(items)}')\n"
                    "print(f'竞品列表: {items}')\n"
                    "output = result\n"
                )
            }
        },

        # ── 5. 循环节点：对每个竞品逐一分析 ─────────────────────────────────
        {
            "id": "n_loop",
            "type": "loop",
            "x": 980, "y": 320,
            "config": {
                "label": "逐一分析竞品",
                "description": "对每个竞品执行AI分析",
                "items_source": "n_code",
                "items_key": "competitors",
                "max_iterations": 3,
                "sub_nodes": [
                    {
                        "id": "sub_competitor_analysis",
                        "type": "ai_chat",
                        "config": {
                            "label": "竞品单项分析",
                            "model_config_id": model_config_id,
                            "system_prompt": "你是一位专业的竞品分析师，擅长简洁客观地评估产品特点。",
                            "prompt": "请对「{{item}}」进行简要竞品分析（100字以内），从以下角度：\n1. 核心优势\n2. 主要不足\n3. 市场定位\n\n主题背景：{{topic}}"
                        }
                    }
                ]
            }
        },

        # ── 6. 条件判断：是否生成详细报告 ────────────────────────────────────
        {
            "id": "n_condition",
            "type": "condition",
            "x": 1220, "y": 320,
            "config": {
                "label": "是否详细报告",
                "description": "根据 detail 参数决定报告详细程度",
                "expression": "detail == 'yes'"
            }
        },

        # ── 7a. AI对话：生成完整竞品分析报告（detail=yes分支）───────────────
        {
            "id": "n_ai_full_report",
            "type": "ai_chat",
            "x": 1460, "y": 160,
            "config": {
                "label": "生成完整竞品报告",
                "model_config_id": model_config_id,
                "system_prompt": "你是一位资深市场分析师，擅长撰写专业、深度的竞品分析报告。",
                "prompt": (
                    "请基于以下信息，撰写一份完整的竞品分析报告：\n\n"
                    "**分析主题**：{{topic}}\n\n"
                    "**分析框架**（来自并行分析）：\n{{n_parallel}}\n\n"
                    "**竞品逐项分析**（来自循环分析）：\n{{n_loop}}\n\n"
                    "**报告要求**：\n"
                    "- 使用 Markdown 格式\n"
                    "- 包含：执行摘要、竞品对比矩阵、各竞品详细分析、综合结论、战略建议\n"
                    "- 专业严谨，有数据支撑的观点\n"
                    "- 字数600字以上"
                )
            }
        },

        # ── 7b. AI对话：生成简要结论（detail=no分支）────────────────────────
        {
            "id": "n_ai_brief",
            "type": "ai_chat",
            "x": 1460, "y": 480,
            "config": {
                "label": "生成简要结论",
                "model_config_id": model_config_id,
                "system_prompt": "你是一位擅长信息提炼的分析师，能用最简洁的语言传达核心洞察。",
                "prompt": (
                    "请基于以下竞品分析结果，用200字以内给出核心结论和最重要的3条建议：\n\n"
                    "**主题**：{{topic}}\n\n"
                    "**竞品分析**：{{n_loop}}\n\n"
                    "格式：先写2句核心结论，再列3条具体建议。"
                )
            }
        },

        # ── 8. 文档生成（仅详细报告分支）────────────────────────────────────
        {
            "id": "n_doc",
            "type": "doc_output",
            "x": 1700, "y": 160,
            "config": {
                "label": "生成分析报告文档",
                "doc_title": "{{topic}} 竞品分析报告",
                "source_node_id": "n_ai_full_report"
            }
        },

        # ── 9. 结束节点 ───────────────────────────────────────────────────────
        {
            "id": "n_end",
            "type": "end",
            "x": 1940, "y": 320,
            "config": {"label": "结束"}
        }
    ],

    "edges": [
        # 主干流程
        {"id": "e1",  "source": "n_start",          "target": "n_set_var"},
        {"id": "e2",  "source": "n_set_var",         "target": "n_parallel"},
        {"id": "e3",  "source": "n_parallel",        "target": "n_code"},
        {"id": "e4",  "source": "n_code",            "target": "n_loop"},
        {"id": "e5",  "source": "n_loop",            "target": "n_condition"},
        # 条件分支：True → 完整报告，False → 简要结论
        {"id": "e6",  "source": "n_condition",       "target": "n_ai_full_report", "label": "详细(yes)"},
        {"id": "e7",  "source": "n_condition",       "target": "n_ai_brief",       "label": "简要(no)"},
        # 完整报告分支 → 文档 → 结束
        {"id": "e8",  "source": "n_ai_full_report",  "target": "n_doc"},
        {"id": "e9",  "source": "n_doc",             "target": "n_end"},
        # 简要结论分支 → 结束
        {"id": "e10", "source": "n_ai_brief",        "target": "n_end"},
    ]
}

# ── 写入数据库 ────────────────────────────────────────────────────────────────
WF_NAME = "多维度竞品情报自动分析系统"
DESCRIPTION = (
    "全节点类型演示工作流。输入 topic(主题)+competitors(竞品列表)+detail(yes/no)，"
    "经过：设置变量→并行采集(HTTP+网页爬取+AI框架)→代码处理→循环竞品分析→条件分支→"
    "完整报告/简要结论→文档生成→结束。共14个节点，覆盖所有主要节点类型。"
)

cur.execute("SELECT id FROM workflows WHERE name=%s LIMIT 1", (WF_NAME,))
existing = cur.fetchone()
if existing:
    wf_id = existing[0]
    cur.execute(
        "UPDATE workflows SET definition=%s, description=%s, is_published=1, is_active=1, version=version+1, updated_at=NOW() WHERE id=%s",
        (json.dumps(definition, ensure_ascii=False), DESCRIPTION, wf_id)
    )
    print(f"✅ 已更新工作流 ID={wf_id}")
else:
    cur.execute(
        """INSERT INTO workflows
           (name, description, definition, is_active, is_published, version, owner_id, trigger_type, category, icon, created_at, updated_at)
           VALUES (%s, %s, %s, 1, 1, 1, %s, 'manual', 'analysis', '�', NOW(), NOW())""",
        (WF_NAME, DESCRIPTION, json.dumps(definition, ensure_ascii=False), owner_id)
    )
    wf_id = cur.lastrowid
    print(f"✅ 已创建工作流 ID={wf_id}")

conn.commit()
conn.close()

print(f"""
╔══════════════════════════════════════════════════════════════╗
║          多维度竞品情报自动分析系统  (ID={wf_id})
╠══════════════════════════════════════════════════════════════╣
║  模型: {model_name} (id={model_config_id})
╠══════════════════════════════════════════════════════════════╣
║  节点流程（共14个节点）:
║
║  [开始] → [设置变量]
║         → [并行采集]
║               ├─ HTTP请求 (httpbin.org/json)
║               ├─ 网页爬取 (httpbin.org/html)
║               └─ AI生成分析框架
║         → [代码执行] 解析竞品列表
║         → [循环] 逐一分析每个竞品
║               └─ AI竞品单项分析
║         → [条件判断] detail == 'yes'?
║               ├─ 是 → [AI完整报告] → [文档生成] → [结束]
║               └─ 否 → [AI简要结论]              → [结束]
║
╠══════════════════════════════════════════════════════════════╣
║  运行方式（前端点击"运行"按钮，输入以下参数）:
║
║  topic       = 大模型应用平台
║  competitors = ChatGPT,文心一言,通义千问
║  detail      = yes
╚══════════════════════════════════════════════════════════════╝
""")
