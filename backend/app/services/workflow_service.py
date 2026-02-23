import json
from typing import Dict, Any, Tuple, List
from sqlalchemy.ext.asyncio import AsyncSession


def _clean(obj: Any, _depth: int = 0) -> Any:
    """Recursively convert to JSON-safe plain types, max depth 10."""
    if _depth > 10:
        return str(obj)
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _clean(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(i, _depth + 1) for i in obj]
    try:
        return json.loads(json.dumps(obj, default=str))
    except Exception:
        return str(obj)


class WorkflowEngine:
    """
    流程编排执行引擎。
    definition 格式:
    {
        "nodes": [{"id": "n1", "type": "start|end|ai_chat|condition|http|delay", "config": {...}}],
        "edges": [{"source": "n1", "target": "n2", "condition": null}]
    }
    """

    @staticmethod
    async def execute(
        definition: Dict[str, Any],
        input_data: Dict[str, Any],
        db: AsyncSession,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        nodes: List[Dict] = definition.get("nodes", [])
        edges: List[Dict] = definition.get("edges", [])

        node_map = {n["id"]: n for n in nodes}

        # Support both "from/to" and "source/target" edge formats
        adj: Dict[str, List[str]] = {}
        in_degree: Dict[str, int] = {n["id"]: 0 for n in nodes}
        for edge in edges:
            src = edge.get("from") or edge.get("source", "")
            tgt = edge.get("to") or edge.get("target", "")
            if src and tgt:
                adj.setdefault(src, []).append(tgt)
                in_degree[tgt] = in_degree.get(tgt, 0) + 1

        start_node = next((n for n in nodes if n.get("type") == "start"), None)
        if not start_node:
            raise ValueError("流程缺少 start 节点")

        context = {"input": input_data, "variables": {}}
        node_results: Dict[str, Any] = {}

        # Use a queue-based BFS execution to handle merge nodes (nodes with multiple incoming edges)
        # We track which nodes are ready to execute (all predecessors done)
        from collections import deque

        executed: set = set()
        # Start with the start node
        queue: deque = deque([start_node["id"]])
        # Track pending count for merge nodes
        pending_in: Dict[str, int] = dict(in_degree)
        # Nodes skipped by condition branches
        skipped: set = set()

        max_steps = 200
        steps = 0

        while queue and steps < max_steps:
            steps += 1
            current_id = queue.popleft()

            if current_id in executed:
                continue
            if current_id in skipped:
                # Still mark successors as having one less pending predecessor
                for nxt in adj.get(current_id, []):
                    pending_in[nxt] = max(0, pending_in.get(nxt, 1) - 1)
                    if pending_in[nxt] == 0 and nxt not in executed and nxt not in skipped:
                        queue.append(nxt)
                continue

            node = node_map.get(current_id)
            if not node:
                continue

            result = await WorkflowEngine._execute_node(node, context, db)
            node_results[current_id] = result
            context["variables"][current_id] = result
            executed.add(current_id)

            if node.get("type") == "end":
                break

            next_nodes = adj.get(current_id, [])
            if not next_nodes:
                continue

            if node.get("type") == "condition":
                condition_result = result.get("condition_result", True)
                if len(next_nodes) >= 2:
                    chosen = next_nodes[0] if condition_result else next_nodes[1]
                    not_chosen = next_nodes[1] if condition_result else next_nodes[0]
                    # Reduce pending count for chosen branch
                    pending_in[chosen] = max(0, pending_in.get(chosen, 1) - 1)
                    if pending_in[chosen] == 0 and chosen not in executed:
                        queue.append(chosen)
                    # Reduce pending count for not-chosen branch (treat as "skipped predecessor")
                    # Only mark as skipped if in_degree becomes 0 via skipped paths only
                    skip_queue = deque([not_chosen])
                    while skip_queue:
                        sid = skip_queue.popleft()
                        if sid in skipped or sid in executed:
                            continue
                        pending_in[sid] = max(0, pending_in.get(sid, 1) - 1)
                        if pending_in[sid] > 0:
                            # This node still has other incoming edges (merge node) - don't skip it
                            continue
                        skipped.add(sid)
                        for s_nxt in adj.get(sid, []):
                            if s_nxt not in executed and s_nxt not in skipped:
                                skip_queue.append(s_nxt)
                elif next_nodes:
                    nxt = next_nodes[0]
                    pending_in[nxt] = max(0, pending_in.get(nxt, 1) - 1)
                    if pending_in[nxt] == 0:
                        queue.append(nxt)
            else:
                for nxt in next_nodes:
                    pending_in[nxt] = max(0, pending_in.get(nxt, 1) - 1)
                    if pending_in[nxt] == 0 and nxt not in executed and nxt not in skipped:
                        queue.append(nxt)

        # Build clean output
        output = {k: _clean(v) for k, v in node_results.items()}
        return output, {k: _clean(v) for k, v in node_results.items()}

    @staticmethod
    async def _execute_node(
        node: Dict[str, Any],
        context: Dict[str, Any],
        db: AsyncSession,
    ) -> Dict[str, Any]:
        node_type = node.get("type", "")
        config = node.get("config", {})

        if node_type == "start":
            return {"status": "ok", "output": context.get("input", {})}

        elif node_type == "end":
            return {"status": "ok"}

        elif node_type == "ai_chat":
            return await WorkflowEngine._exec_ai_chat(config, context, db)

        elif node_type == "condition":
            return await WorkflowEngine._exec_condition(config, context)

        elif node_type == "http":
            return await WorkflowEngine._exec_http(config, context)

        elif node_type == "delay":
            import asyncio
            delay_seconds = config.get("seconds", 1)
            await asyncio.sleep(min(delay_seconds, 30))
            return {"status": "ok", "delayed": delay_seconds}

        elif node_type == "set_variable":
            key = config.get("key", "result")
            value = config.get("value", "")
            context["variables"][key] = value
            return {"status": "ok", "key": key, "value": value}

        elif node_type == "knowledge_search":
            return await WorkflowEngine._exec_knowledge_search(config, context, db)

        elif node_type == "doc_output":
            return await WorkflowEngine._exec_doc_output(config, context)

        elif node_type == "data_source":
            return await WorkflowEngine._exec_data_source(config, context)

        elif node_type == "db_write":
            return await WorkflowEngine._exec_db_write(config, context)

        elif node_type == "send_email":
            return await WorkflowEngine._exec_send_email(config, context)

        elif node_type == "agent_invoke":
            return await WorkflowEngine._exec_agent_invoke(config, context, db)

        elif node_type == "loop":
            return await WorkflowEngine._exec_loop(config, context, db)

        elif node_type == "parallel":
            return await WorkflowEngine._exec_parallel(config, context, db)

        elif node_type == "code":
            return await WorkflowEngine._exec_code(config, context)

        elif node_type == "web_crawl":
            return await WorkflowEngine._exec_web_crawl(config, context)

        else:
            return {"status": "skipped", "node_type": node_type}

    @staticmethod
    async def _exec_ai_chat(config: Dict, context: Dict, db: AsyncSession) -> Dict:
        from sqlalchemy import select
        from app.models.model_provider import ModelConfig, ModelProvider
        from app.services.model_service import ModelService
        from app.schemas.model_provider import ChatRequest, ChatMessage

        model_config_id = config.get("model_config_id")
        prompt_template = config.get("prompt", "")
        system_prompt = config.get("system_prompt", "")

        prompt = prompt_template
        for key, val in context.get("variables", {}).items():
            prompt = prompt.replace(f"{{{{{key}}}}}", str(val))
        for key, val in context.get("input", {}).items():
            prompt = prompt.replace(f"{{{{{key}}}}}", str(val))

        if not model_config_id:
            return {"status": "error", "error": "未配置模型"}

        result = await db.execute(select(ModelConfig).where(ModelConfig.id == model_config_id))
        model_config = result.scalar_one_or_none()
        if not model_config:
            return {"status": "error", "error": "模型配置不存在"}

        result = await db.execute(select(ModelProvider).where(ModelProvider.id == model_config.provider_id))
        provider = result.scalar_one_or_none()
        if not provider:
            return {"status": "error", "error": "模型供应商不存在"}

        req = ChatRequest(
            model_config_id=model_config_id,
            messages=[ChatMessage(role="user", content=prompt)],
            system_prompt=system_prompt or None,
            stream=False,
        )
        try:
            response = await ModelService.chat(provider, model_config, req)
            return {"status": "ok", "content": response.get("content", ""), "usage": response.get("usage")}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    @staticmethod
    async def _exec_condition(config: Dict, context: Dict) -> Dict:
        expression = config.get("expression", "true")
        try:
            # Build eval namespace: input vars + node results (by id, replacing - with _)
            ns: Dict[str, Any] = {"len": len, "str": str, "int": int, "bool": bool, "True": True, "False": False}
            ns.update(context.get("input", {}))
            for node_id, node_result in context.get("variables", {}).items():
                safe_key = node_id.replace("-", "_")
                ns[safe_key] = node_result
                # Also expose sub-fields directly for convenience
                if isinstance(node_result, dict):
                    for sub_k, sub_v in node_result.items():
                        ns[f"{safe_key}_{sub_k}"] = sub_v
            result = bool(eval(expression, {"__builtins__": {}}, ns))
        except Exception as ex:
            result = False
        return {"status": "ok", "condition_result": result, "expression": expression}

    @staticmethod
    async def _exec_http(config: Dict, context: Dict) -> Dict:
        import httpx
        url = config.get("url", "")
        method = config.get("method", "GET").upper()
        headers = config.get("headers", {})
        body = config.get("body", None)

        if not url:
            return {"status": "error", "error": "未配置URL"}

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.request(method, url, headers=headers, json=body)
                return {
                    "status": "ok",
                    "status_code": response.status_code,
                    "body": response.text[:2000],
                }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    @staticmethod
    async def _exec_doc_output(config: Dict, context: Dict) -> Dict:
        """Collect content from a referenced node and mark it as a document output."""
        source_node_id = config.get("source_node_id", "")
        doc_title = config.get("doc_title", "输出文档")

        content = ""
        if source_node_id:
            node_result = context.get("variables", {}).get(source_node_id, {})
            if isinstance(node_result, dict):
                content = node_result.get("content", "")
        else:
            # Collect content from all ai_chat nodes executed so far
            for node_result in context.get("variables", {}).values():
                if isinstance(node_result, dict) and node_result.get("content"):
                    content = node_result["content"]
                    break

        return {
            "status": "ok",
            "content": content,
            "doc_title": doc_title,
            "is_document": True,
        }

    @staticmethod
    async def _exec_data_source(config: Dict, context: Dict) -> Dict:
        """Read data from MySQL, CSV, or Excel file."""
        source_type = config.get("source_type", "csv")

        if source_type == "csv":
            import csv, io as _io
            file_path = config.get("file_path", "")
            if not file_path:
                return {"status": "error", "error": "未配置文件路径"}
            try:
                import aiofiles
                async with aiofiles.open(file_path, "r", encoding="utf-8-sig") as f:
                    content = await f.read()
                reader = csv.DictReader(_io.StringIO(content))
                rows = [dict(r) for r in reader]
                return {"status": "ok", "rows": rows, "count": len(rows), "source_type": "csv"}
            except Exception as e:
                return {"status": "error", "error": str(e)}

        elif source_type == "excel":
            file_path = config.get("file_path", "")
            sheet = config.get("sheet", 0)
            if not file_path:
                return {"status": "error", "error": "未配置文件路径"}
            try:
                import asyncio as _asyncio
                def _read_excel():
                    import openpyxl
                    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
                    ws = wb.worksheets[int(sheet)] if str(sheet).isdigit() else wb[sheet]
                    rows_iter = ws.iter_rows(values_only=True)
                    headers = [str(h) if h is not None else f"col{i}" for i, h in enumerate(next(rows_iter, []))]
                    rows = [dict(zip(headers, row)) for row in rows_iter]
                    return rows
                loop = _asyncio.get_event_loop()
                rows = await loop.run_in_executor(None, _read_excel)
                return {"status": "ok", "rows": rows, "count": len(rows), "source_type": "excel"}
            except Exception as e:
                return {"status": "error", "error": str(e)}

        elif source_type == "mysql":
            host = config.get("host", "localhost")
            port = int(config.get("port", 3306))
            user = config.get("user", "root")
            password = config.get("password", "")
            database = config.get("database", "")
            sql = config.get("sql", "")
            # Substitute context variables into SQL
            for key, val in context.get("input", {}).items():
                sql = sql.replace(f"{{{{{key}}}}}", str(val))
            for key, val in context.get("variables", {}).items():
                if isinstance(val, dict):
                    for sub_k, sub_v in val.items():
                        sql = sql.replace(f"{{{{{key}.{sub_k}}}}}", str(sub_v))
                sql = sql.replace(f"{{{{{key}}}}}", str(val))
            if not sql:
                return {"status": "error", "error": "未配置 SQL 查询"}
            try:
                import aiomysql
                conn = await aiomysql.connect(host=host, port=port, user=user, password=password, db=database)
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(sql)
                    rows = await cur.fetchall()
                conn.close()
                rows = [dict(r) for r in rows]
                return {"status": "ok", "rows": rows, "count": len(rows), "source_type": "mysql"}
            except Exception as e:
                return {"status": "error", "error": str(e)}

        return {"status": "error", "error": f"不支持的数据源类型: {source_type}"}

    @staticmethod
    async def _exec_db_write(config: Dict, context: Dict) -> Dict:
        """Write data to MySQL database."""
        host = config.get("host", "localhost")
        port = int(config.get("port", 3306))
        user = config.get("user", "root")
        password = config.get("password", "")
        database = config.get("database", "")
        table = config.get("table", "")
        source_node_id = config.get("source_node_id", "")
        columns = config.get("columns", [])

        if not table:
            return {"status": "error", "error": "未配置目标表名"}

        # Get data from referenced node
        rows = []
        if source_node_id:
            node_result = context.get("variables", {}).get(source_node_id, {})
            if isinstance(node_result, dict):
                rows = node_result.get("rows", [])
                if not rows and node_result.get("content"):
                    rows = [{"content": node_result["content"]}]
        if not rows:
            return {"status": "error", "error": "没有可写入的数据"}

        try:
            import aiomysql
            conn = await aiomysql.connect(host=host, port=port, user=user, password=password, db=database)
            async with conn.cursor() as cur:
                if columns:
                    cols = columns
                else:
                    cols = list(rows[0].keys()) if rows else []
                if not cols:
                    return {"status": "error", "error": "无法确定列名"}
                placeholders = ", ".join(["%s"] * len(cols))
                col_names = ", ".join(f"`{c}`" for c in cols)
                sql = f"INSERT INTO `{table}` ({col_names}) VALUES ({placeholders})"
                values = [tuple(str(r.get(c, "")) for c in cols) for r in rows]
                await cur.executemany(sql, values)
            await conn.commit()
            conn.close()
            return {"status": "ok", "written": len(rows), "table": table}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    @staticmethod
    async def _exec_send_email(config: Dict, context: Dict) -> Dict:
        """Send email via SMTP."""
        import asyncio as _asyncio
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        smtp_host = config.get("smtp_host", "")
        smtp_port = int(config.get("smtp_port", 465))
        smtp_user = config.get("smtp_user", "")
        smtp_password = config.get("smtp_password", "")
        use_ssl = config.get("use_ssl", True)
        to_addrs = config.get("to", "")
        subject = config.get("subject", "工作流通知")
        body_template = config.get("body", "")

        if not smtp_host or not smtp_user:
            return {"status": "error", "error": "未配置 SMTP 服务器"}
        if not to_addrs:
            return {"status": "error", "error": "未配置收件人"}

        # Substitute variables into subject and body
        for key, val in context.get("input", {}).items():
            subject = subject.replace(f"{{{{{key}}}}}", str(val))
            body_template = body_template.replace(f"{{{{{key}}}}}", str(val))
        for node_id, node_result in context.get("variables", {}).items():
            if isinstance(node_result, dict):
                content = node_result.get("content", "")
                body_template = body_template.replace(f"{{{{{node_id}.content}}}}", content)
                body_template = body_template.replace(f"{{{{{node_id}}}}}", str(node_result))

        def _send():
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = smtp_user
            msg["To"] = to_addrs
            msg.attach(MIMEText(body_template, "html", "utf-8"))
            if use_ssl:
                server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15)
            else:
                server = smtplib.SMTP(smtp_host, smtp_port, timeout=15)
                server.starttls()
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, [a.strip() for a in to_addrs.split(",")], msg.as_string())
            server.quit()

        try:
            loop = _asyncio.get_event_loop()
            await loop.run_in_executor(None, _send)
            return {"status": "ok", "to": to_addrs, "subject": subject}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    @staticmethod
    async def _exec_knowledge_search(config: Dict, context: Dict, db: AsyncSession) -> Dict:
        from sqlalchemy import select
        from app.models.knowledge import KnowledgeBase
        from app.services.knowledge_service import KnowledgeService

        kb_id = config.get("knowledge_base_id")
        query_template = config.get("query", "")
        top_k = config.get("top_k", 3)

        query = query_template
        for key, val in context.get("variables", {}).items():
            query = query.replace(f"{{{{{key}}}}}", str(val))
        for key, val in context.get("input", {}).items():
            query = query.replace(f"{{{{{key}}}}}", str(val))

        if not kb_id:
            return {"status": "error", "error": "未配置知识库"}

        result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
        kb = result.scalar_one_or_none()
        if not kb:
            return {"status": "error", "error": "知识库不存在"}

        items = await KnowledgeService.search(
            collection_name=kb.collection_name,
            query=query,
            top_k=top_k,
        )
        return {"status": "ok", "results": items, "query": query}

    @staticmethod
    async def _exec_agent_invoke(config: Dict, context: Dict, db: AsyncSession) -> Dict:
        """在工作流中调用 Agent（ReAct 模式）"""
        from sqlalchemy import select
        from app.models.agent import Agent
        from app.services.agent_service import AgentEngine

        agent_id = config.get("agent_id")
        input_template = config.get("input", "{{input}}")

        # 模板变量替换
        user_input = input_template
        for key, val in context.get("input", {}).items():
            user_input = user_input.replace(f"{{{{{key}}}}}", str(val))
        for key, val in context.get("variables", {}).items():
            if isinstance(val, dict):
                user_input = user_input.replace(f"{{{{{key}}}}}", str(val.get("content", val)))
            else:
                user_input = user_input.replace(f"{{{{{key}}}}}", str(val))

        if not agent_id:
            return {"status": "error", "error": "未配置 Agent ID"}

        res = await db.execute(select(Agent).where(Agent.id == agent_id, Agent.is_active == True))
        agent = res.scalar_one_or_none()
        if not agent:
            return {"status": "error", "error": f"Agent {agent_id} 不存在"}

        agent_config = {
            "id": agent.id,
            "model_config_id": agent.model_config_id,
            "system_prompt": agent.system_prompt,
            "max_iterations": agent.max_iterations,
            "temperature": agent.temperature,
            "tools": agent.tools or [],
            "knowledge_base_id": agent.knowledge_base_id,
        }

        messages = [{"role": "user", "content": user_input}]
        steps = []
        final_content = ""

        try:
            async for event in AgentEngine.run_stream(agent_config, messages, db):
                if event["type"] == "done":
                    break
                steps.append(event)
                if event["type"] == "final":
                    final_content = event["content"]
        except Exception as e:
            return {"status": "error", "error": str(e)}

        return {
            "status": "ok",
            "content": final_content,
            "steps": steps,
            "iterations": len([s for s in steps if s["type"] == "thought"]),
        }

    @staticmethod
    async def _exec_loop(config: Dict, context: Dict, db: AsyncSession) -> Dict:
        """循环节点：对列表中的每个元素执行子节点序列"""
        import asyncio as _asyncio
        items_source = config.get("items_source", "")  # 来源节点 id
        items_key = config.get("items_key", "rows")     # 取哪个字段的列表
        max_iterations = min(int(config.get("max_iterations", 10)), 50)
        sub_nodes = config.get("sub_nodes", [])         # 内嵌子节点列表

        # 获取要迭代的列表
        items = []
        if items_source:
            node_result = context.get("variables", {}).get(items_source, {})
            if isinstance(node_result, dict):
                items = node_result.get(items_key, [])
        if not items:
            items = context.get("input", {}).get(items_key, [])

        if not items:
            return {"status": "ok", "results": [], "count": 0, "message": "没有可迭代的数据"}

        results = []
        for i, item in enumerate(items[:max_iterations]):
            # 为每次迭代创建子上下文
            iter_context = {
                "input": {**context.get("input", {}), "item": item, "index": i},
                "variables": dict(context.get("variables", {})),
            }
            iter_result = {"index": i, "item": item}
            for sub_node in sub_nodes:
                try:
                    sub_result = await WorkflowEngine._execute_node(sub_node, iter_context, db)
                    iter_context["variables"][sub_node.get("id", f"sub_{i}")] = sub_result
                    iter_result[sub_node.get("id", f"sub_{i}")] = sub_result
                except Exception as e:
                    iter_result["error"] = str(e)
                    break
            results.append(iter_result)

        return {"status": "ok", "results": results, "count": len(results)}

    @staticmethod
    async def _exec_parallel(config: Dict, context: Dict, db: AsyncSession) -> Dict:
        """并行节点：同时执行多个子节点，汇总结果"""
        import asyncio as _asyncio
        sub_nodes = config.get("sub_nodes", [])

        if not sub_nodes:
            return {"status": "ok", "results": {}, "message": "没有配置并行子节点"}

        async def _run_sub(sub_node):
            try:
                return sub_node.get("id", "unknown"), await WorkflowEngine._execute_node(sub_node, context, db)
            except Exception as e:
                return sub_node.get("id", "unknown"), {"status": "error", "error": str(e)}

        tasks = [_run_sub(n) for n in sub_nodes]
        pairs = await _asyncio.gather(*tasks, return_exceptions=False)
        results = {k: v for k, v in pairs}

        # 合并所有子节点的 content 字段
        combined_content = "\n\n".join(
            f"[{k}]: {v.get('content', '')}" for k, v in results.items() if v.get("content")
        )
        return {"status": "ok", "results": results, "content": combined_content}

    @staticmethod
    async def _exec_code(config: Dict, context: Dict) -> Dict:
        """代码执行节点：在沙箱中运行 Python 代码"""
        from app.services.tools.code_tools import execute_python_exec

        code = config.get("code", "")
        # 将上下文变量注入代码前缀
        var_lines = []
        for k, v in context.get("input", {}).items():
            safe_v = repr(v) if not isinstance(v, str) else repr(v)
            var_lines.append(f"{k} = {safe_v}")
        for k, v in context.get("variables", {}).items():
            if isinstance(v, dict):
                safe_v = repr(v)
                var_lines.append(f"{k} = {safe_v}")

        full_code = "\n".join(var_lines) + "\n" + code if var_lines else code
        result_str = execute_python_exec({"code": full_code, "timeout": config.get("timeout", 5)})
        return {"status": "ok", "content": result_str, "output": result_str}

    @staticmethod
    async def _exec_web_crawl(config: Dict, context: Dict) -> Dict:
        """网页爬取节点：抓取 URL 内容并提取文本"""
        from app.services.knowledge_service import KnowledgeService

        url = config.get("url", "")
        # 支持模板变量替换
        for k, v in context.get("input", {}).items():
            url = url.replace(f"{{{{{k}}}}}", str(v))
        for k, v in context.get("variables", {}).items():
            if isinstance(v, dict):
                url = url.replace(f"{{{{{k}}}}}", str(v.get("content", "")))
            else:
                url = url.replace(f"{{{{{k}}}}}", str(v))

        if not url:
            return {"status": "error", "error": "未配置 URL"}

        text = await KnowledgeService.extract_text_from_url(url)
        max_len = int(config.get("max_length", 5000))
        return {"status": "ok", "content": text[:max_len], "url": url, "length": len(text)}
