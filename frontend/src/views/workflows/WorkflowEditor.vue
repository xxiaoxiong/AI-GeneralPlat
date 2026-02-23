<template>
  <div class="editor-page" @click="closeContextMenu" @contextmenu.prevent>
    <!-- Toolbar -->
    <div class="editor-toolbar">
      <div class="toolbar-left">
        <el-button :icon="ArrowLeft" @click="router.back()">返回</el-button>
        <el-input v-model="workflow.name" style="width:200px;" size="small" />
        <el-tag type="info" size="small">v{{ workflow.version }}</el-tag>
      </div>
      <div class="toolbar-center">
        <span class="toolbar-hint">拖拽节点到画布 · 从右侧端口拖出连线 · 右键查看操作</span>
      </div>
      <div class="toolbar-right">
        <el-button size="small" @click="saveWorkflow" :loading="saving">保存</el-button>
        <el-button size="small" type="success" @click="publishWorkflow">
          {{ workflow.is_published ? '已发布' : '发布' }}
        </el-button>
        <el-button size="small" type="warning" plain @click="router.push(`/workflows/${workflowId}/run`)">▶ 运行</el-button>
        <el-button size="small" type="info" plain @click="openPublishAsApp">📦 发布为应用</el-button>
        <el-button size="small" type="primary" @click="testRun">调试</el-button>
      </div>
    </div>

    <div class="editor-body">
      <!-- Node Panel -->
      <div class="node-panel">
        <div class="panel-title">节点库</div>
        <div
          v-for="nt in nodeTypes"
          :key="nt.type"
          class="node-item"
          draggable="true"
          @dragstart="onDragStart($event, nt)"
        >
          <span class="node-icon">{{ nt.icon }}</span>
          <div>
            <div class="node-name">{{ nt.label }}</div>
            <div class="node-desc">{{ nt.desc }}</div>
          </div>
        </div>
      </div>

      <!-- Canvas -->
      <div
        class="canvas-area"
        ref="canvasRef"
        @dragover.prevent
        @drop="onDrop"
        @mouseup="onCanvasMouseUp"
        @mousemove="onCanvasMouseMove"
        @contextmenu.prevent="onCanvasRightClick($event)"
        @click.self="deselectAll"
      >
        <svg class="canvas-svg" :width="canvasWidth" :height="canvasHeight">
          <defs>
            <marker id="arrowhead" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto">
              <polygon points="0 0, 10 3.5, 0 7" fill="#409eff" />
            </marker>
            <marker id="arrowhead-hover" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto">
              <polygon points="0 0, 10 3.5, 0 7" fill="#f56c6c" />
            </marker>
          </defs>

          <!-- Existing edges -->
          <g v-for="edge in edges" :key="edge.id"
            class="edge-group"
            @contextmenu.prevent="onEdgeRightClick($event, edge)"
          >
            <path
              :d="getEdgePath(edge)"
              :stroke="hoveredEdge?.id === edge.id ? '#f56c6c' : '#409eff'"
              stroke-width="2"
              fill="none"
              marker-end="url(#arrowhead)"
              class="edge-path"
            />
            <!-- Invisible wider hit area -->
            <path
              :d="getEdgePath(edge)"
              stroke="transparent"
              stroke-width="12"
              fill="none"
              class="edge-hit"
              @mouseenter="hoveredEdge = edge"
              @mouseleave="hoveredEdge = null"
              @contextmenu.prevent="onEdgeRightClick($event, edge)"
            />
          </g>

          <!-- Connecting preview line -->
          <path
            v-if="connectingState.active"
            :d="connectingPreviewPath"
            stroke="#67c23a"
            stroke-width="2"
            stroke-dasharray="6,3"
            fill="none"
            pointer-events="none"
          />
        </svg>

        <!-- Nodes -->
        <div
          v-for="node in nodes"
          :key="node.id"
          class="canvas-node"
          :class="{ selected: selectedNode?.id === node.id, [node.type]: true, connecting: connectingState.active }"
          :style="{ left: node.position.x + 'px', top: node.position.y + 'px' }"
          @mousedown.left="startDragNode($event, node)"
          @click.stop="selectNode(node)"
          @contextmenu.prevent="onNodeRightClick($event, node)"
        >
          <div class="node-header">
            <span>{{ getNodeType(node.type)?.icon }}</span>
            <span>{{ node.config?.label || getNodeType(node.type)?.label }}</span>
          </div>
          <!-- Input port (left) -->
          <div
            class="node-port port-in"
            :class="{ 'port-highlight': connectingState.active && connectingState.fromNode?.id !== node.id }"
            @mouseup.stop="onPortMouseUp(node, 'in')"
            @mousedown.stop
          />
          <!-- Output port (right) -->
          <div
            class="node-port port-out"
            @mousedown.stop="startConnect($event, node)"
          />
        </div>
      </div>

      <!-- Config Panel -->
      <div class="config-panel" v-if="selectedNode">
        <div class="panel-title">节点配置 - {{ getNodeType(selectedNode.type)?.label }}</div>
        <el-form label-width="80px" size="small">
          <el-form-item label="节点标签">
            <el-input v-model="selectedNode.config.label" @change="markDirty" />
          </el-form-item>

          <template v-if="selectedNode.type === 'ai_chat'">
            <el-form-item label="选择模型">
              <el-select v-model="selectedNode.config.model_config_id" style="width:100%" @change="markDirty">
                <el-option v-for="m in chatModels" :key="m.id" :label="m.display_name" :value="m.id" />
              </el-select>
            </el-form-item>
            <el-form-item label="提示词模板">
              <el-select
                v-model="selectedNode.config._template_id"
                clearable
                placeholder="从模板库选择（可选）"
                style="width:100%"
                @change="applyPromptTemplate(selectedNode, $event)"
              >
                <el-option v-for="t in promptTemplates" :key="t.id" :label="t.name" :value="t.id">
                  <span>{{ t.name }}</span>
                  <span style="float:right;font-size:11px;color:#aaa">{{ t.category }}</span>
                </el-option>
              </el-select>
            </el-form-item>
            <el-form-item label="系统提示">
              <el-input v-model="selectedNode.config.system_prompt" type="textarea" :rows="2" @change="markDirty" />
            </el-form-item>
            <el-form-item label="提示词">
              <el-input v-model="selectedNode.config.prompt" type="textarea" :rows="5"
                placeholder="使用 {{变量名}} 引用上下文变量" @change="markDirty" />
            </el-form-item>
          </template>

          <template v-if="selectedNode.type === 'condition'">
            <el-form-item label="条件表达式">
              <el-input v-model="selectedNode.config.expression" placeholder="如: score > 0.8" @change="markDirty" />
            </el-form-item>
          </template>

          <template v-if="selectedNode.type === 'http'">
            <el-form-item label="请求方法">
              <el-select v-model="selectedNode.config.method" style="width:100%" @change="markDirty">
                <el-option label="GET" value="GET" /><el-option label="POST" value="POST" />
                <el-option label="PUT" value="PUT" /><el-option label="DELETE" value="DELETE" />
              </el-select>
            </el-form-item>
            <el-form-item label="URL">
              <el-input v-model="selectedNode.config.url" @change="markDirty" />
            </el-form-item>
          </template>

          <template v-if="selectedNode.type === 'knowledge_search'">
            <el-form-item label="知识库">
              <el-select v-model="selectedNode.config.knowledge_base_id" style="width:100%" @change="markDirty">
                <el-option v-for="kb in knowledgeBases" :key="kb.id" :label="kb.name" :value="kb.id" />
              </el-select>
            </el-form-item>
            <el-form-item label="检索问题">
              <el-input v-model="selectedNode.config.query" type="textarea" :rows="2"
                placeholder="使用 {{变量名}} 引用变量" @change="markDirty" />
            </el-form-item>
            <el-form-item label="返回数量">
              <el-input-number v-model="selectedNode.config.top_k" :min="1" :max="10" @change="markDirty" />
            </el-form-item>
          </template>

          <template v-if="selectedNode.type === 'delay'">
            <el-form-item label="延迟秒数">
              <el-input-number v-model="selectedNode.config.seconds" :min="1" :max="30" @change="markDirty" />
            </el-form-item>
          </template>

          <template v-if="selectedNode.type === 'doc_output'">
            <el-form-item label="文档标题">
              <el-input v-model="selectedNode.config.doc_title" placeholder="生成的 Word 文档标题" @change="markDirty" />
            </el-form-item>
            <el-form-item label="内容来源">
              <el-select v-model="selectedNode.config.source_node_id" clearable style="width:100%"
                placeholder="选择 AI 节点（留空=自动取最后一个AI输出）" @change="markDirty">
                <el-option
                  v-for="n in nodes.filter(n => n.type === 'ai_chat')"
                  :key="n.id"
                  :label="n.config?.label || 'AI 对话节点'"
                  :value="n.id"
                />
              </el-select>
            </el-form-item>
            <div style="font-size:11px;color:#888;padding:0 4px 8px;">
              运行后在结果页点击「⬇ 下载 Word」即可获取文档。
            </div>
          </template>

          <!-- Data Source Node -->
          <template v-if="selectedNode.type === 'data_source'">
            <el-form-item label="数据源类型">
              <el-select v-model="selectedNode.config.source_type" style="width:100%" @change="markDirty">
                <el-option label="CSV 文件" value="csv" />
                <el-option label="Excel 文件" value="excel" />
                <el-option label="MySQL 数据库" value="mysql" />
              </el-select>
            </el-form-item>
            <template v-if="selectedNode.config.source_type === 'csv' || selectedNode.config.source_type === 'excel'">
              <el-form-item label="文件路径">
                <el-input v-model="selectedNode.config.file_path" placeholder="服务器上的绝对路径" @change="markDirty" />
              </el-form-item>
              <el-form-item v-if="selectedNode.config.source_type === 'excel'" label="工作表">
                <el-input v-model="selectedNode.config.sheet" placeholder="工作表名或索引(0开始)" @change="markDirty" />
              </el-form-item>
            </template>
            <template v-if="selectedNode.config.source_type === 'mysql'">
              <el-form-item label="主机">
                <el-input v-model="selectedNode.config.host" placeholder="localhost" @change="markDirty" />
              </el-form-item>
              <el-form-item label="端口">
                <el-input v-model="selectedNode.config.port" placeholder="3306" @change="markDirty" />
              </el-form-item>
              <el-form-item label="用户名">
                <el-input v-model="selectedNode.config.user" @change="markDirty" />
              </el-form-item>
              <el-form-item label="密码">
                <el-input v-model="selectedNode.config.password" type="password" show-password @change="markDirty" />
              </el-form-item>
              <el-form-item label="数据库名">
                <el-input v-model="selectedNode.config.database" @change="markDirty" />
              </el-form-item>
              <el-form-item label="SQL 查询">
                <el-input v-model="selectedNode.config.sql" type="textarea" :rows="3"
                  placeholder="SELECT * FROM table WHERE id = {{input_id}}" @change="markDirty" />
              </el-form-item>
            </template>
            <div style="font-size:11px;color:#888;padding:0 4px 8px;">
              读取结果存入 rows 数组，可在 AI 节点提示词中引用上游节点数据。
            </div>
          </template>

          <!-- DB Write Node -->
          <template v-if="selectedNode.type === 'db_write'">
            <el-form-item label="主机">
              <el-input v-model="selectedNode.config.host" placeholder="localhost" @change="markDirty" />
            </el-form-item>
            <el-form-item label="端口">
              <el-input v-model="selectedNode.config.port" placeholder="3306" @change="markDirty" />
            </el-form-item>
            <el-form-item label="用户名">
              <el-input v-model="selectedNode.config.user" @change="markDirty" />
            </el-form-item>
            <el-form-item label="密码">
              <el-input v-model="selectedNode.config.password" type="password" show-password @change="markDirty" />
            </el-form-item>
            <el-form-item label="数据库名">
              <el-input v-model="selectedNode.config.database" @change="markDirty" />
            </el-form-item>
            <el-form-item label="目标表名">
              <el-input v-model="selectedNode.config.table" placeholder="target_table" @change="markDirty" />
            </el-form-item>
            <el-form-item label="数据来源节点">
              <el-select v-model="selectedNode.config.source_node_id" clearable style="width:100%" @change="markDirty">
                <el-option
                  v-for="n in nodes.filter(n => n.id !== selectedNode.id)"
                  :key="n.id"
                  :label="n.config?.label || getNodeType(n.type)?.label || n.id"
                  :value="n.id"
                />
              </el-select>
            </el-form-item>
          </template>

          <!-- Send Email Node -->
          <template v-if="selectedNode.type === 'send_email'">
            <el-form-item label="SMTP 服务器">
              <el-input v-model="selectedNode.config.smtp_host" placeholder="smtp.qq.com" @change="markDirty" />
            </el-form-item>
            <el-form-item label="SMTP 端口">
              <el-input v-model="selectedNode.config.smtp_port" placeholder="465" @change="markDirty" />
            </el-form-item>
            <el-form-item label="发件人邮箱">
              <el-input v-model="selectedNode.config.smtp_user" @change="markDirty" />
            </el-form-item>
            <el-form-item label="邮箱密码">
              <el-input v-model="selectedNode.config.smtp_password" type="password" show-password @change="markDirty" />
            </el-form-item>
            <el-form-item label="SSL">
              <el-switch v-model="selectedNode.config.use_ssl" @change="markDirty" />
            </el-form-item>
            <el-form-item label="收件人">
              <el-input v-model="selectedNode.config.to" placeholder="a@x.com, b@x.com" @change="markDirty" />
            </el-form-item>
            <el-form-item label="邮件主题">
              <el-input v-model="selectedNode.config.subject" placeholder="支持 {{变量}}" @change="markDirty" />
            </el-form-item>
            <el-form-item label="邮件正文">
              <el-input v-model="selectedNode.config.body" type="textarea" :rows="4"
                placeholder="支持 HTML 和 {{节点ID.content}} 变量" @change="markDirty" />
            </el-form-item>
          </template>

          <!-- Agent Invoke Node -->
          <template v-if="selectedNode.type === 'agent_invoke'">
            <el-form-item label="Agent ID">
              <el-input v-model="selectedNode.config.agent_id" placeholder="Agent ID（数字）" @change="markDirty" />
            </el-form-item>
            <el-form-item label="输入内容">
              <el-input v-model="selectedNode.config.input" type="textarea" :rows="3"
                placeholder="支持 {{变量名}} 引用上游节点输出，如 {{n_ai_analyze.content}}" @change="markDirty" />
            </el-form-item>
            <div style="font-size:11px;color:#888;padding:0 4px 8px;">
              Agent 将自主思考并调用工具完成任务，结果存入 content 字段。
            </div>
          </template>

          <template v-if="selectedNode.type === 'start'">
            <div style="font-size:12px;color:#888;margin-bottom:8px;padding:0 4px;">
              在此定义用户运行工作流时需要填写的输入字段，运行时将自动生成表单。
            </div>
            <div
              v-for="(field, idx) in (selectedNode.config.input_fields || [])"
              :key="idx"
              style="background:#f5f7fa;border-radius:6px;padding:8px;margin-bottom:8px;"
            >
              <div style="display:flex;gap:4px;margin-bottom:4px;">
                <el-input v-model="field.key" placeholder="字段名(英文)" size="small" style="width:100px;" @change="markDirty" />
                <el-select v-model="field.type" size="small" style="width:90px;" @change="markDirty">
                  <el-option label="单行文本" value="text" />
                  <el-option label="多行文本" value="textarea" />
                  <el-option label="下拉选择" value="select" />
                </el-select>
                <el-button size="small" type="danger" text @click="removeInputField(Number(idx))">删除</el-button>
              </div>
              <el-input v-model="field.label" placeholder="显示标签" size="small" style="margin-bottom:4px;" @change="markDirty" />
              <el-input v-model="field.placeholder" placeholder="占位提示文字" size="small" style="margin-bottom:4px;" @change="markDirty" />
              <el-input v-if="field.type === 'select'" v-model="field.options" placeholder="选项,用逗号分隔,如: yes,no" size="small" @change="markDirty" />
            </div>
            <el-button size="small" type="primary" text @click="addInputField">＋ 添加输入字段</el-button>
          </template>
        </el-form>
      </div>
    </div>

    <!-- Right-click Context Menu -->
    <div
      v-if="contextMenu.visible"
      class="context-menu"
      :style="{ left: contextMenu.x + 'px', top: contextMenu.y + 'px' }"
      @click.stop
    >
      <template v-if="contextMenu.type === 'node'">
        <div class="ctx-item" @click="ctxSelectNode">选中节点</div>
        <div class="ctx-item" @click="ctxConnectFrom">从此节点连线</div>
        <div class="ctx-divider" />
        <div class="ctx-item" @click="ctxDeleteInEdges">删除所有入连线</div>
        <div class="ctx-item" @click="ctxDeleteOutEdges">删除所有出连线</div>
        <div class="ctx-divider" />
        <div class="ctx-item danger" @click="ctxDeleteNode">删除节点</div>
      </template>
      <template v-else-if="contextMenu.type === 'edge'">
        <div class="ctx-item danger" @click="ctxDeleteEdge">删除此连线</div>
      </template>
      <template v-else>
        <div class="ctx-item" @click="ctxAddNode('ai_chat')">➕ 添加 AI 对话节点</div>
        <div class="ctx-item" @click="ctxAddNode('condition')">➕ 添加条件判断节点</div>
        <div class="ctx-item" @click="ctxAddNode('knowledge_search')">➕ 添加知识检索节点</div>
        <div class="ctx-item" @click="ctxAddNode('http')">➕ 添加 HTTP 请求节点</div>
        <div class="ctx-divider" />
        <div class="ctx-item" @click="ctxCancelConnect" v-if="connectingState.active">取消连线</div>
      </template>
    </div>

    <!-- Test Run Dialog -->
    <el-dialog v-model="testDialogVisible" title="测试运行" width="700px" top="3vh" :close-on-click-modal="false">
      <div class="test-dialog-body">
        <!-- Input Form -->
        <div v-if="!testResult" class="test-input-section">
          <div class="test-section-title">填写输入参数</div>
          <!-- Smart form from start node input_fields -->
          <template v-if="startInputFields.length">
            <el-form label-position="top" style="margin-top:8px;">
              <el-form-item
                v-for="field in startInputFields"
                :key="field.key"
                :label="field.label || field.key"
              >
                <el-input
                  v-if="field.type === 'textarea'"
                  v-model="testFormData[field.key]"
                  type="textarea" :rows="3"
                  :placeholder="field.placeholder || '请输入' + (field.label || field.key)"
                />
                <el-select
                  v-else-if="field.type === 'select'"
                  v-model="testFormData[field.key]"
                  style="width:100%"
                  :placeholder="field.placeholder || '请选择'"
                >
                  <el-option
                    v-for="opt in (field.options || '').split(',')"
                    :key="opt.trim()" :label="opt.trim()" :value="opt.trim()"
                  />
                </el-select>
                <el-input
                  v-else
                  v-model="testFormData[field.key]"
                  :placeholder="field.placeholder || '请输入' + (field.label || field.key)"
                />
              </el-form-item>
            </el-form>
          </template>
          <!-- Fallback: inferred fields as simple inputs -->
          <template v-else-if="inferredInputFields.length">
            <div style="font-size:12px;color:#999;margin-bottom:10px;">
              💡 从节点配置中检测到以下变量，请填写对应的值：
            </div>
            <el-form label-position="top">
              <el-form-item v-for="f in inferredInputFields" :key="f" :label="f">
                <el-input v-model="testFormData[f]" :placeholder="'请输入 ' + f" />
              </el-form-item>
            </el-form>
          </template>
          <template v-else>
            <div style="color:#aaa;font-size:13px;padding:20px 0;text-align:center;">
              该工作流无需输入参数，直接点击运行即可
            </div>
          </template>
        </div>

        <!-- Result Section -->
        <div v-if="testResult">
          <!-- Status bar -->
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;">
            <div style="display:flex;align-items:center;gap:10px;">
              <el-tag :type="testResult.status === 'success' ? 'success' : 'danger'" size="large">
                {{ testResult.status === 'success' ? '✅ 执行成功' : '❌ 执行失败' }}
              </el-tag>
              <span style="color:#888;font-size:13px;">耗时 {{ testResult.duration_ms }}ms</span>
            </div>
            <el-button size="small" text @click="testResult = null">← 重新填写</el-button>
          </div>

          <!-- Error -->
          <div v-if="testResult.error_msg" style="background:#fff2f0;border:1px solid #ffccc7;border-radius:8px;padding:12px;color:#cf1322;margin-bottom:16px;">
            {{ testResult.error_msg }}
          </div>

          <!-- Final Output — most prominent -->
          <div v-if="finalOutput" class="final-output-card">
            <div class="final-output-header">
              <span>📄 最终输出</span>
              <el-button size="small" text @click="copyOutput">复制</el-button>
            </div>
            <div class="final-output-content">{{ finalOutput }}</div>
          </div>

          <!-- Node execution log — collapsed by default -->
          <el-collapse style="margin-top:12px;">
            <el-collapse-item name="log">
              <template #title>
                <span style="font-size:13px;color:#888;">🔍 查看节点执行详情（{{ Object.keys(testResult.node_results || {}).length }} 个节点）</span>
              </template>
              <div
                v-for="(r, nid) in testResult.node_results"
                :key="nid"
                class="node-log-item"
              >
                <div class="node-log-header">
                  <el-tag size="small" :type="r.status === 'ok' ? 'success' : 'danger'">{{ r.status }}</el-tag>
                  <span>{{ getNodeLabel(nid) }}</span>
                  <span v-if="r.condition_result !== undefined" class="node-log-cond">
                    → {{ r.condition_result ? '走上分支 True' : '走下分支 False' }}
                  </span>
                  <span v-if="r.results" class="node-log-cond">→ 检索到 {{ r.results.length }} 条</span>
                </div>
                <div v-if="r.content" class="node-log-content ai">{{ r.content }}</div>
                <div v-else-if="r.results">
                  <div v-for="(item, i) in r.results" :key="i" class="node-log-kb-item">
                    <span class="kb-meta">{{ item.filename }} · {{ (item.score*100).toFixed(0) }}%</span>
                    <div>{{ item.content?.slice(0, 120) }}{{ item.content?.length > 120 ? '...' : '' }}</div>
                  </div>
                </div>
              </div>
            </el-collapse-item>
          </el-collapse>
        </div>
      </div>

      <template #footer>
        <el-button @click="testDialogVisible = false">关闭</el-button>
        <el-button v-if="!testResult" type="primary" :loading="testing" @click="runTest">
          ▶ 运行
        </el-button>
      </template>
    </el-dialog>

    <!-- Publish as App Dialog -->
    <el-dialog v-model="publishAppDialogVisible" title="📦 发布为应用市场" width="480px">
      <div style="font-size:13px;color:#888;margin-bottom:16px;">
        将此工作流包装成应用，用户可在应用市场直接找到并运行，无需了解工作流细节。
      </div>
      <el-form :model="publishAppForm" label-width="80px">
        <el-form-item label="应用名称">
          <el-input v-model="publishAppForm.name" placeholder="应用展示名称" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="publishAppForm.description" type="textarea" :rows="2" placeholder="简要描述应用的用途" />
        </el-form-item>
        <el-form-item label="分类">
          <el-select v-model="publishAppForm.category" style="width:100%">
            <el-option label="法务合规" value="legal" />
            <el-option label="财税金融" value="finance" />
            <el-option label="人力资源" value="hr" />
            <el-option label="采购供应链" value="procurement" />
            <el-option label="市场营销" value="marketing" />
            <el-option label="客户服务" value="service" />
            <el-option label="政务办公" value="office" />
            <el-option label="合规管理" value="compliance" />
            <el-option label="研发技术" value="tech" />
            <el-option label="其他" value="other" />
          </el-select>
        </el-form-item>
        <el-form-item label="公开访问">
          <el-switch v-model="publishAppForm.is_public" active-text="公开" inactive-text="私有" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="publishAppDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="publishingApp" @click="confirmPublishAsApp">发布</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowLeft } from '@element-plus/icons-vue'
import { workflowApi, modelApi, knowledgeApi, appApi, promptApi } from '@/api'
import type { ModelConfig, KnowledgeBase } from '@/types'

const route = useRoute()
const router = useRouter()
const workflowId = Number(route.params.id)

const workflow = ref<any>({ name: '', version: 1, is_published: false })
const nodes = ref<any[]>([])
const edges = ref<any[]>([])
const selectedNode = ref<any>(null)
const hoveredEdge = ref<any>(null)
const saving = ref(false)
const isDirty = ref(false)
const canvasRef = ref<HTMLElement>()
const canvasWidth = 2000
const canvasHeight = 1200

const chatModels = ref<ModelConfig[]>([])
const knowledgeBases = ref<KnowledgeBase[]>([])
const promptTemplates = ref<any[]>([])

const testDialogVisible = ref(false)
const testInput = ref('{}')
const testFormData = ref<Record<string, string>>({})
const testResult = ref<any>(null)
const testing = ref(false)

// Start node input fields (defined by workflow designer)
const startInputFields = computed(() => {
  const startNode = nodes.value.find(n => n.type === 'start')
  return startNode?.config?.input_fields || []
})

// Final output: last ai_chat node's content in execution order
const finalOutput = computed(() => {
  if (!testResult.value?.node_results) return ''
  const nr = testResult.value.node_results
  // Find last node with content (ai_chat output)
  const entries = Object.entries(nr) as [string, any][]
  const aiEntries = entries.filter(([, v]) => v.content)
  if (!aiEntries.length) return ''
  return aiEntries[aiEntries.length - 1][1].content as string
})

function addInputField() {
  const startNode = nodes.value.find(n => n.type === 'start')
  if (!startNode) return
  if (!startNode.config.input_fields) startNode.config.input_fields = []
  startNode.config.input_fields.push({ key: '', label: '', type: 'text', placeholder: '', options: '' })
  markDirty()
}

function removeInputField(idx: number) {
  const startNode = nodes.value.find(n => n.type === 'start')
  if (!startNode?.config?.input_fields) return
  startNode.config.input_fields.splice(idx, 1)
  markDirty()
}

function copyOutput() {
  if (!finalOutput.value) return
  navigator.clipboard.writeText(finalOutput.value).then(() => ElMessage.success('已复制到剪贴板'))
}

// ── Publish as App ─────────────────────────────────────────────────────────────
const publishAppDialogVisible = ref(false)
const publishingApp = ref(false)
const publishAppForm = reactive({
  name: '',
  description: '',
  category: 'other',
  is_public: false,
})

function openPublishAsApp() {
  publishAppForm.name = workflow.value.name || ''
  publishAppForm.description = workflow.value.description || ''
  publishAppForm.category = workflow.value.category || 'other'
  publishAppForm.is_public = false
  publishAppDialogVisible.value = true
}

async function confirmPublishAsApp() {
  if (!publishAppForm.name.trim()) return ElMessage.warning('请输入应用名称')
  publishingApp.value = true
  try {
    // Ensure workflow is published first
    if (!workflow.value.is_published) {
      await workflowApi.update(workflowId, { is_published: true })
      workflow.value.is_published = true
    }
    // Create app instance directly bound to this workflow (no template needed)
    await appApi.createInstance({
      template_id: 1,
      name: publishAppForm.name,
      description: publishAppForm.description,
      workflow_id: workflowId,
      is_public: publishAppForm.is_public,
    })
    ElMessage.success('已发布到应用市场！')
    publishAppDialogVisible.value = false
  } catch (e: any) {
    ElMessage.error(e?.message || '发布失败')
  } finally {
    publishingApp.value = false
  }
}

// ── Connecting state (drag from out-port to in-port) ──────────────────────────
const connectingState = reactive({
  active: false,
  fromNode: null as any,
  mouseX: 0,
  mouseY: 0,
})

const connectingPreviewPath = computed(() => {
  if (!connectingState.active || !connectingState.fromNode) return ''
  const src = connectingState.fromNode
  const x1 = src.position.x + 124
  const y1 = src.position.y + 30
  const x2 = connectingState.mouseX
  const y2 = connectingState.mouseY
  const cx = (x1 + x2) / 2
  return `M ${x1} ${y1} C ${cx} ${y1}, ${cx} ${y2}, ${x2} ${y2}`
})

// ── Context menu ──────────────────────────────────────────────────────────────
const contextMenu = reactive({
  visible: false,
  x: 0,
  y: 0,
  type: '' as 'node' | 'edge' | 'canvas',
  target: null as any,
})

function showContextMenu(e: MouseEvent, type: 'node' | 'edge' | 'canvas', target: any = null) {
  contextMenu.visible = true
  contextMenu.x = e.clientX
  contextMenu.y = e.clientY
  contextMenu.type = type
  contextMenu.target = target
}

function closeContextMenu() {
  contextMenu.visible = false
}

// ── Node types ────────────────────────────────────────────────────────────────
let draggingNodeType: any = null
let draggingNode: any = null
let dragOffsetX = 0
let dragOffsetY = 0
let nodeCounter = 0

const nodeTypes = [
  { type: 'start', label: '开始', icon: '▶️', desc: '流程入口' },
  { type: 'end', label: '结束', icon: '⏹️', desc: '流程出口' },
  { type: 'ai_chat', label: 'AI 对话', icon: '🤖', desc: '调用大模型' },
  { type: 'knowledge_search', label: '知识检索', icon: '🔍', desc: '检索知识库' },
  { type: 'condition', label: '条件判断', icon: '🔀', desc: '分支条件' },
  { type: 'http', label: 'HTTP 请求', icon: '🌐', desc: '调用外部接口' },
  { type: 'delay', label: '延时等待', icon: '⏱️', desc: '等待指定时间' },
  { type: 'set_variable', label: '设置变量', icon: '📝', desc: '设置上下文变量' },
  { type: 'doc_output', label: '文档生成', icon: '📄', desc: '生成 Word 文档' },
  { type: 'data_source', label: '数据读取', icon: '🗄️', desc: 'MySQL/CSV/Excel' },
  { type: 'db_write', label: '写入数据库', icon: '💾', desc: '结果写回 MySQL' },
  { type: 'send_email', label: '发送邮件', icon: '📧', desc: 'SMTP 邮件通知' },
  { type: 'agent_invoke', label: 'Agent 调用', icon: '🧠', desc: 'ReAct 智能体' },
  { type: 'loop', label: '循环', icon: '🔁', desc: '遍历列表执行' },
  { type: 'parallel', label: '并行', icon: '⚡', desc: '并行执行子节点' },
  { type: 'code', label: '代码执行', icon: '🐍', desc: 'Python 沙箱执行' },
  { type: 'web_crawl', label: '网页爬取', icon: '🕷️', desc: '抓取网页文本' },
]

function getNodeType(type: string) {
  return nodeTypes.find(n => n.type === type)
}

function markDirty() { isDirty.value = true }

// ── Drag from node library ────────────────────────────────────────────────────
function onDragStart(e: DragEvent, nt: any) {
  draggingNodeType = nt
}

function onDrop(e: DragEvent) {
  if (!draggingNodeType || !canvasRef.value) return
  const rect = canvasRef.value.getBoundingClientRect()
  const scrollLeft = canvasRef.value.scrollLeft
  const scrollTop = canvasRef.value.scrollTop
  const x = e.clientX - rect.left + scrollLeft - 60
  const y = e.clientY - rect.top + scrollTop - 20
  nodeCounter++
  nodes.value.push({
    id: `node_${Date.now()}_${nodeCounter}`,
    type: draggingNodeType.type,
    config: { label: draggingNodeType.label },
    position: { x: Math.max(0, x), y: Math.max(0, y) },
  })
  draggingNodeType = null
  markDirty()
}

// ── Node drag-move ────────────────────────────────────────────────────────────
function startDragNode(e: MouseEvent, node: any) {
  if (connectingState.active) return
  e.stopPropagation()
  draggingNode = node
  dragOffsetX = e.clientX - node.position.x
  dragOffsetY = e.clientY - node.position.y

  const onMove = (ev: MouseEvent) => {
    if (!draggingNode) return
    draggingNode.position.x = Math.max(0, ev.clientX - dragOffsetX)
    draggingNode.position.y = Math.max(0, ev.clientY - dragOffsetY)
    markDirty()
  }
  const onUp = () => {
    draggingNode = null
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
  }
  document.addEventListener('mousemove', onMove)
  document.addEventListener('mouseup', onUp)
}

// ── Connect: drag from out-port ───────────────────────────────────────────────
function startConnect(e: MouseEvent, node: any) {
  e.stopPropagation()
  e.preventDefault()
  connectingState.active = true
  connectingState.fromNode = node
  if (canvasRef.value) {
    const rect = canvasRef.value.getBoundingClientRect()
    connectingState.mouseX = e.clientX - rect.left + canvasRef.value.scrollLeft
    connectingState.mouseY = e.clientY - rect.top + canvasRef.value.scrollTop
  }
}

function onCanvasMouseMove(e: MouseEvent) {
  if (!connectingState.active || !canvasRef.value) return
  const rect = canvasRef.value.getBoundingClientRect()
  connectingState.mouseX = e.clientX - rect.left + canvasRef.value.scrollLeft
  connectingState.mouseY = e.clientY - rect.top + canvasRef.value.scrollTop
}

function onCanvasMouseUp(e: MouseEvent) {
  if (connectingState.active) {
    connectingState.active = false
    connectingState.fromNode = null
  }
}

function onPortMouseUp(node: any, portType: string) {
  if (!connectingState.active || portType !== 'in') return
  const from = connectingState.fromNode
  if (!from || from.id === node.id) {
    connectingState.active = false
    connectingState.fromNode = null
    return
  }
  const exists = edges.value.find(ed => ed.source === from.id && ed.target === node.id)
  if (!exists) {
    edges.value.push({ id: `edge_${from.id}_${node.id}`, source: from.id, target: node.id })
    markDirty()
    ElMessage.success('连线成功')
  }
  connectingState.active = false
  connectingState.fromNode = null
}

// ── Select / deselect ─────────────────────────────────────────────────────────
function selectNode(node: any) {
  if (connectingState.active) return
  selectedNode.value = node
  closeContextMenu()
}

function deselectAll() {
  selectedNode.value = null
  closeContextMenu()
}

// ── Delete ────────────────────────────────────────────────────────────────────
function deleteNode(node: any) {
  nodes.value = nodes.value.filter(n => n.id !== node.id)
  edges.value = edges.value.filter(e => e.source !== node.id && e.target !== node.id)
  if (selectedNode.value?.id === node.id) selectedNode.value = null
  markDirty()
}

// ── Edge path ─────────────────────────────────────────────────────────────────
function getEdgePath(edge: any) {
  const src = nodes.value.find(n => n.id === edge.source)
  const tgt = nodes.value.find(n => n.id === edge.target)
  if (!src || !tgt) return ''
  const x1 = src.position.x + 124
  const y1 = src.position.y + 30
  const x2 = tgt.position.x
  const y2 = tgt.position.y + 30
  const cx = (x1 + x2) / 2
  return `M ${x1} ${y1} C ${cx} ${y1}, ${cx} ${y2}, ${x2} ${y2}`
}

// ── Right-click handlers ──────────────────────────────────────────────────────
function onNodeRightClick(e: MouseEvent, node: any) {
  e.stopPropagation()
  selectedNode.value = node
  showContextMenu(e, 'node', node)
}

function onEdgeRightClick(e: MouseEvent, edge: any) {
  e.stopPropagation()
  showContextMenu(e, 'edge', edge)
}

function onCanvasRightClick(e: MouseEvent) {
  showContextMenu(e, 'canvas', { x: e.clientX, y: e.clientY })
}

// ── Context menu actions ──────────────────────────────────────────────────────
function ctxSelectNode() {
  selectedNode.value = contextMenu.target
  closeContextMenu()
}

function ctxConnectFrom() {
  const node = contextMenu.target
  closeContextMenu()
  connectingState.active = true
  connectingState.fromNode = node
  if (canvasRef.value) {
    connectingState.mouseX = node.position.x + 124
    connectingState.mouseY = node.position.y + 30
  }
  ElMessage.info('请点击目标节点的左侧端口完成连线，或右键取消')
}

function ctxDeleteNode() {
  deleteNode(contextMenu.target)
  closeContextMenu()
}

function ctxDeleteEdge() {
  edges.value = edges.value.filter(e => e.id !== contextMenu.target.id)
  markDirty()
  closeContextMenu()
}

function ctxDeleteInEdges() {
  const nodeId = contextMenu.target.id
  edges.value = edges.value.filter(e => e.target !== nodeId)
  markDirty()
  closeContextMenu()
}

function ctxDeleteOutEdges() {
  const nodeId = contextMenu.target.id
  edges.value = edges.value.filter(e => e.source !== nodeId)
  markDirty()
  closeContextMenu()
}

function ctxAddNode(type: string) {
  const nt = getNodeType(type)
  if (!nt || !canvasRef.value) return
  const rect = canvasRef.value.getBoundingClientRect()
  const x = contextMenu.x - rect.left + canvasRef.value.scrollLeft - 60
  const y = contextMenu.y - rect.top + canvasRef.value.scrollTop - 20
  nodeCounter++
  nodes.value.push({
    id: `node_${Date.now()}_${nodeCounter}`,
    type,
    config: { label: nt.label },
    position: { x: Math.max(0, x), y: Math.max(0, y) },
  })
  markDirty()
  closeContextMenu()
}

function ctxCancelConnect() {
  connectingState.active = false
  connectingState.fromNode = null
  closeContextMenu()
}

// ── Save / Publish / Test ─────────────────────────────────────────────────────
async function saveWorkflow() {
  saving.value = true
  try {
    const definition = { nodes: nodes.value, edges: edges.value }
    await workflowApi.update(workflowId, { name: workflow.value.name, definition })
    workflow.value.version++
    isDirty.value = false
    ElMessage.success('保存成功')
  } finally {
    saving.value = false
  }
}

async function publishWorkflow() {
  await workflowApi.update(workflowId, { is_published: !workflow.value.is_published })
  workflow.value.is_published = !workflow.value.is_published
  ElMessage.success(workflow.value.is_published ? '已发布' : '已取消发布')
}

// ── Infer input fields from node configs (find {{var}} patterns) ──────────────
const inferredInputFields = computed(() => {
  const fields = new Set<string>()
  const re = /\{\{(\w+)\}\}/g
  for (const node of nodes.value) {
    const cfg = node.config || {}
    for (const val of Object.values(cfg)) {
      if (typeof val === 'string') {
        let m: RegExpExecArray | null
        while ((m = re.exec(val)) !== null) fields.add(m[1])
        re.lastIndex = 0
      }
    }
  }
  return Array.from(fields)
})

function insertField(field: string) {
  try {
    const obj = JSON.parse(testInput.value || '{}')
    if (!(field in obj)) obj[field] = `示例${field}`
    testInput.value = JSON.stringify(obj, null, 2)
  } catch {
    testInput.value = JSON.stringify({ [field]: `示例${field}` }, null, 2)
  }
}

function getNodeLabel(nodeId: string | number) {
  const id = String(nodeId)
  const node = nodes.value.find(n => n.id === id)
  if (!node) return id
  return node.config?.label || getNodeType(node.type)?.label || id
}

function testRun() {
  testResult.value = null
  // Init form data: use start node fields or inferred fields
  const fd: Record<string, string> = {}
  if (startInputFields.value.length) {
    for (const f of startInputFields.value) fd[f.key] = ''
  } else {
    for (const f of inferredInputFields.value) fd[f] = ''
  }
  testFormData.value = fd
  testDialogVisible.value = true
}

async function runTest() {
  testing.value = true
  try {
    if (isDirty.value) await saveWorkflow()
    const result = await workflowApi.execute(workflowId, { input_data: testFormData.value })
    testResult.value = result
  } finally {
    testing.value = false
  }
}

function applyPromptTemplate(node: any, templateId: number | null) {
  if (!templateId) return
  const tpl = promptTemplates.value.find(t => t.id === templateId)
  if (!tpl) return
  node.config.prompt = tpl.content
  markDirty()
  ElMessage.success(`已应用模板「${tpl.name}」`)
}

onMounted(async () => {
  try {
    const wf = await workflowApi.get(workflowId)
    workflow.value = wf
    nodes.value = (wf.definition?.nodes || []).map((n: any) => {
      if (!n.position) {
        n.position = { x: n.x ?? 100, y: n.y ?? 100 }
      }
      return n
    })
    edges.value = (wf.definition?.edges || []).map((e: any) => ({
      ...e,
      source: e.source || e.from || '',
      target: e.target || e.to || '',
    }))
  } catch (e: any) {
    ElMessage.error('加载工作流失败: ' + (e?.message || e))
    return
  }

  // Load auxiliary data (non-blocking failures)
  try {
    const models = await modelApi.listConfigs({ model_type: 'chat' })
    chatModels.value = models.filter((m: ModelConfig) => m.model_type === 'chat' && m.is_active)
  } catch { /* ignore */ }
  try {
    const kbs = await knowledgeApi.listBases({ page_size: 100 })
    knowledgeBases.value = kbs.items
  } catch { /* ignore */ }
  try {
    const tpls = await promptApi.list({ page_size: 100, include_public: true })
    promptTemplates.value = tpls.items || []
  } catch { /* ignore */ }
})
</script>

<style scoped>
.editor-page {
  display: flex; flex-direction: column; height: calc(100vh - 56px);
  background: #f9f9f9; position: relative; user-select: none;
}
.editor-toolbar {
  height: 52px; background: #fff; border-bottom: 1px solid #e5e5e5;
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 16px; gap: 12px; flex-shrink: 0; z-index: 10;
}
.toolbar-left, .toolbar-right { display: flex; align-items: center; gap: 8px; }
.toolbar-hint { font-size: 13px; color: #9ca3af; }
.editor-body { flex: 1; display: flex; overflow: hidden; }

/* Node panel */
.node-panel {
  width: 180px; background: #fff; border-right: 1px solid #e5e5e5;
  overflow-y: auto; flex-shrink: 0; padding: 8px;
}
.panel-title {
  font-size: 13px; font-weight: 600; color: #0d0d0d;
  padding: 8px 4px; border-bottom: 1px solid #f3f4f6; margin-bottom: 8px;
}
.node-item {
  display: flex; align-items: center; gap: 8px; padding: 8px;
  border-radius: 8px; cursor: grab; transition: background 0.15s; margin-bottom: 4px;
}
.node-item:hover { background: #f3f4f6; }
.node-icon { font-size: 18px; }
.node-name { font-size: 13px; font-weight: 500; color: #374151; }
.node-desc { font-size: 11px; color: #9ca3af; }

/* Canvas */
.canvas-area {
  flex: 1; position: relative; overflow: auto; background: #fafafa;
  background-image: radial-gradient(circle, #e5e5e5 1px, transparent 1px);
  background-size: 24px 24px; cursor: default;
}
.canvas-area.is-connecting { cursor: crosshair; }
.canvas-svg {
  position: absolute; top: 0; left: 0;
  pointer-events: none; overflow: visible;
}
:deep(.edge-path) { pointer-events: none; }
:deep(.edge-hit) { pointer-events: stroke; cursor: pointer; }

/* Nodes */
.canvas-node {
  position: absolute; min-width: 120px; background: #fff;
  border: 1.5px solid #e5e5e5; border-radius: 10px; padding: 8px 16px 8px 12px;
  cursor: move; user-select: none; box-shadow: 0 1px 4px rgba(0,0,0,0.07);
  transition: border-color 0.15s, box-shadow 0.15s;
}
.canvas-node:hover { border-color: #10a37f; box-shadow: 0 2px 8px rgba(16,163,127,0.12); }
.canvas-node.selected { border-color: #10a37f; box-shadow: 0 0 0 3px rgba(16,163,127,0.15); }
.canvas-node.start { border-color: #22c55e; }
.canvas-node.end { border-color: #ef4444; }
.canvas-node.connecting { cursor: default; }
.node-header { display: flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 500; white-space: nowrap; }

/* Ports */
.node-port {
  position: absolute; width: 12px; height: 12px; border-radius: 50%;
  background: #d1d5db; border: 2px solid #fff;
  box-shadow: 0 0 0 1px #d1d5db;
  transition: background 0.15s, transform 0.15s;
  z-index: 2;
}
.port-in {
  left: -7px; top: 50%; transform: translateY(-50%);
  cursor: cell;
}
.port-out {
  right: -7px; top: 50%; transform: translateY(-50%);
  cursor: crosshair; background: #10a37f; box-shadow: 0 0 0 1px #10a37f;
}
.port-out:hover { background: #0d8f6f; transform: translateY(-50%) scale(1.3); }
.port-in:hover, .port-in.port-highlight {
  background: #22c55e; box-shadow: 0 0 0 1px #22c55e;
  transform: translateY(-50%) scale(1.4);
}

/* Config panel */
.config-panel {
  width: 280px; background: #fff; border-left: 1px solid #e5e5e5;
  overflow-y: auto; flex-shrink: 0; padding: 12px;
}

/* Context menu */
.context-menu {
  position: fixed; z-index: 9999;
  background: #fff; border: 1px solid #e5e5e5;
  border-radius: 10px; padding: 4px 0;
  box-shadow: 0 4px 20px rgba(0,0,0,0.1);
  min-width: 160px;
}
.ctx-item {
  padding: 8px 16px; font-size: 13px; color: #374151;
  cursor: pointer; transition: background 0.15s;
}
.ctx-item:hover { background: #f3f4f6; }
.ctx-item.danger { color: #ef4444; }
.ctx-item.danger:hover { background: #fef2f2; }
.ctx-divider { height: 1px; background: #f3f4f6; margin: 4px 0; }

/* Test dialog */
.test-dialog-body { max-height: 70vh; overflow-y: auto; padding-right: 4px; }
.test-input-section { padding: 4px 0; }
.test-section-title { font-size: 14px; font-weight: 600; color: #0d0d0d; margin-bottom: 12px; }

/* Final output card */
.final-output-card {
  border: 1px solid #d1fae5; border-radius: 12px; overflow: hidden;
  box-shadow: 0 2px 12px rgba(16,163,127,0.1);
}
.final-output-header {
  background: #f0fdf9; padding: 10px 16px;
  display: flex; align-items: center; justify-content: space-between;
  font-size: 14px; font-weight: 600; color: #065f46;
  border-bottom: 1px solid #d1fae5;
}
.final-output-content {
  padding: 16px; font-size: 14px; line-height: 1.8; color: #0d0d0d;
  white-space: pre-wrap; max-height: 360px; overflow-y: auto;
  background: #fff;
}

/* Node execution log */
.node-log-item {
  border-left: 2px solid #e5e5e5; padding: 8px 12px; margin-bottom: 8px;
  border-radius: 0 6px 6px 0; background: #f9f9f9;
}
.node-log-header {
  display: flex; align-items: center; gap: 8px;
  font-size: 13px; font-weight: 500; margin-bottom: 4px;
}
.node-log-cond { font-size: 12px; color: #9ca3af; }
.node-log-content { font-size: 13px; line-height: 1.7; color: #374151; white-space: pre-wrap; }
.node-log-content.ai {
  background: #f0fdf9; border: 1px solid #d1fae5; border-radius: 6px;
  padding: 8px; margin-top: 4px; max-height: 120px; overflow-y: auto;
}
.node-log-kb-item {
  background: #f3f4f6; border-radius: 4px; padding: 6px 8px;
  margin-bottom: 4px; font-size: 12px;
}
.kb-meta { color: #9ca3af; display: block; margin-bottom: 2px; }
</style>
