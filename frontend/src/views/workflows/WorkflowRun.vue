<template>
  <div class="run-page">
    <!-- Header -->
    <div class="run-header">
      <el-button :icon="ArrowLeft" text @click="router.back()">返回</el-button>
      <div class="run-title">
        <span class="run-icon">{{ workflow.icon || '📊' }}</span>
        <div>
          <div class="run-name">{{ workflow.name }}</div>
          <div class="run-desc">{{ workflow.description }}</div>
        </div>
      </div>
    </div>

    <div class="run-body">
      <!-- Left: Input Panel -->
      <div class="input-panel">
        <div class="panel-card">
          <div class="panel-card-title">填写参数</div>

          <template v-if="inputFields.length">
            <el-form label-position="top" class="run-form">
              <el-form-item
                v-for="field in inputFields"
                :key="field.key"
                :label="field.label || field.key"
              >
                <el-input
                  v-if="field.type === 'textarea'"
                  v-model="formData[field.key]"
                  type="textarea" :rows="4"
                  :placeholder="field.placeholder || ('请输入' + (field.label || field.key))"
                />
                <el-select
                  v-else-if="field.type === 'select'"
                  v-model="formData[field.key]"
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
                  v-model="formData[field.key]"
                  :placeholder="field.placeholder || ('请输入' + (field.label || field.key))"
                />
              </el-form-item>
            </el-form>
          </template>

          <template v-else-if="inferredFields.length">
            <div class="hint-text">💡 根据工作流配置检测到以下输入变量：</div>
            <el-form label-position="top" class="run-form">
              <el-form-item v-for="f in inferredFields" :key="f" :label="f">
                <el-input v-model="formData[f]" :placeholder="'请输入 ' + f" />
              </el-form-item>
            </el-form>
          </template>

          <template v-else>
            <div class="no-input-hint">该工作流无需输入参数，直接点击运行即可</div>
          </template>

          <el-button
            type="primary" size="large" style="width:100%;margin-top:16px;"
            :loading="running" @click="doRun"
          >
            {{ running ? '运行中...' : '▶ 运行工作流' }}
          </el-button>

          <!-- History -->
          <div v-if="history.length" style="margin-top:20px;">
            <div class="panel-card-title" style="margin-bottom:8px;">历史记录</div>
            <div
              v-for="(h, i) in history"
              :key="i"
              class="history-item"
              :class="{ active: currentHistoryIdx === i }"
              @click="viewHistory(i)"
            >
              <el-tag size="small" :type="h.status === 'success' ? 'success' : 'danger'">
                {{ h.status === 'success' ? '成功' : '失败' }}
              </el-tag>
              <span class="history-time">{{ h.time }}</span>
              <span class="history-ms">{{ h.duration_ms }}ms</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Right: Output Panel -->
      <div class="output-panel">
        <!-- Empty state -->
        <div v-if="!result" class="empty-output">
          <div class="empty-icon">🚀</div>
          <div class="empty-title">填写左侧参数，点击运行</div>
          <div class="empty-desc">工作流将自动处理并在此展示结果</div>
        </div>

        <!-- Running state -->
        <div v-else-if="running" class="running-state">
          <el-icon class="spin" size="40"><Loading /></el-icon>
          <div style="margin-top:16px;color:#666;">工作流执行中，请稍候...</div>
          <div style="margin-top:8px;color:#aaa;font-size:13px;">正在处理 {{ nodeCount }} 个节点</div>
        </div>

        <!-- Result -->
        <template v-else-if="result">
          <!-- Status -->
          <div class="result-status-bar">
            <el-tag :type="result.status === 'success' ? 'success' : 'danger'" size="large">
              {{ result.status === 'success' ? '✅ 执行成功' : '❌ 执行失败' }}
            </el-tag>
            <span class="result-duration">耗时 {{ result.duration_ms }}ms · {{ nodeExecutedCount }} 个节点</span>
            <el-button size="small" text @click="result = null">清空</el-button>
          </div>

          <!-- Error -->
          <div v-if="result.error_msg" class="error-box">{{ result.error_msg }}</div>

          <!-- Final Output — hero section -->
          <div v-if="finalOutput" class="output-hero" :class="{ 'output-hero-doc': hasDocOutputNode }">
            <div class="output-hero-header">
              <span>
                <template v-if="hasDocOutputNode">📄 {{ docOutputTitle || '文档输出' }}</template>
                <template v-else>📄 输出结果</template>
              </span>
              <div class="output-hero-actions">
                <el-button size="small" @click="copyFinal">复制全文</el-button>
                <el-button
                  size="small"
                  :type="hasDocOutputNode ? 'primary' : 'default'"
                  :plain="!hasDocOutputNode"
                  :loading="downloading"
                  @click="downloadDoc"
                >
                  ⬇ 下载 Word
                </el-button>
              </div>
            </div>
            <div v-if="hasDocOutputNode" class="doc-output-hint">
              ⚡ 此工作流包含文档生成节点，点击「⬇ 下载 Word」获取格式化文档
            </div>
            <div class="output-hero-body markdown-body" v-html="renderMd(finalOutput)" />
          </div>

          <!-- Execution trace — collapsible -->
          <el-collapse class="trace-collapse">
            <el-collapse-item name="trace">
              <template #title>
                <span class="trace-title">🔍 执行过程（{{ nodeExecutedCount }} 个节点）</span>
              </template>
              <div class="trace-list">
                <div
                  v-for="(r, nid) in result.node_results"
                  :key="nid"
                  class="trace-item"
                  :class="r.status"
                >
                  <div class="trace-item-header">
                    <el-tag size="small" :type="r.status === 'ok' ? 'success' : 'danger'">{{ r.status }}</el-tag>
                    <span class="trace-node-name">{{ getNodeLabel(String(nid)) }}</span>
                    <span v-if="r.condition_result !== undefined" class="trace-branch">
                      → {{ r.condition_result ? '上分支 (True)' : '下分支 (False)' }}
                    </span>
                    <span v-if="r.results" class="trace-branch">→ 检索到 {{ r.results.length }} 条</span>
                  </div>
                  <div v-if="r.content" class="trace-ai-content markdown-body" v-html="renderMd(r.content)" />
                  <div v-else-if="r.results" class="trace-kb-results">
                    <div v-for="(item, i) in r.results" :key="i" class="trace-kb-item">
                      <span class="kb-source">{{ item.filename }} · {{ (item.score * 100).toFixed(0) }}%</span>
                      <div>{{ item.content?.slice(0, 100) }}{{ item.content?.length > 100 ? '...' : '' }}</div>
                    </div>
                  </div>
                </div>
              </div>
            </el-collapse-item>
          </el-collapse>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowLeft, Loading } from '@element-plus/icons-vue'
import { marked } from 'marked'
import { workflowApi } from '@/api'

function renderMd(text: string): string {
  if (!text) return ''
  return marked(text) as string
}

const route = useRoute()
const router = useRouter()
const workflowId = Number(route.params.id)

const workflow = ref<any>({ name: '', description: '', icon: '' })
const nodes = ref<any[]>([])
const running = ref(false)
const downloading = ref(false)
const result = ref<any>(null)
const formData = ref<Record<string, string>>({})
const history = ref<any[]>([])
const currentHistoryIdx = ref(-1)
const nodeCount = ref(0)

// Input fields from start node config
const inputFields = computed(() => {
  const startNode = nodes.value.find(n => n.type === 'start')
  return startNode?.config?.input_fields || []
})

// Fallback: infer from {{var}} patterns in node configs
const inferredFields = computed(() => {
  const fields = new Set<string>()
  const re = /\{\{(\w+)\}\}/g
  for (const node of nodes.value) {
    for (const val of Object.values(node.config || {})) {
      if (typeof val === 'string') {
        let m: RegExpExecArray | null
        while ((m = re.exec(val)) !== null) fields.add(m[1])
        re.lastIndex = 0
      }
    }
  }
  return Array.from(fields)
})

const finalOutput = computed(() => {
  if (!result.value?.node_results) return ''
  const entries = Object.entries(result.value.node_results) as [string, any][]
  // Prefer doc_output node content
  const docEntry = entries.find(([, v]) => v.is_document && v.content)
  if (docEntry) return docEntry[1].content as string
  // Fall back to last AI output
  const aiEntries = entries.filter(([, v]) => v.content)
  if (!aiEntries.length) return ''
  return aiEntries[aiEntries.length - 1][1].content as string
})

const hasDocOutputNode = computed(() => {
  if (!result.value?.node_results) return false
  return Object.values(result.value.node_results).some((v: any) => v.is_document)
})

const docOutputTitle = computed(() => {
  if (!result.value?.node_results) return ''
  const docEntry = Object.values(result.value.node_results).find((v: any) => v.is_document)
  return (docEntry as any)?.doc_title || ''
})

const nodeExecutedCount = computed(() => Object.keys(result.value?.node_results || {}).length)

function getNodeLabel(nodeId: string) {
  const node = nodes.value.find(n => n.id === nodeId)
  if (!node) return nodeId
  const typeLabels: Record<string, string> = {
    start: '开始', end: '结束', ai_chat: 'AI 对话',
    knowledge_search: '知识检索', condition: '条件判断',
    http: 'HTTP 请求', delay: '延迟', set_variable: '设置变量',
    doc_output: '文档生成',
  }
  return node.config?.label || typeLabels[node.type] || nodeId
}

async function doRun() {
  running.value = true
  result.value = null
  nodeCount.value = nodes.value.length
  try {
    const res = await workflowApi.execute(workflowId, { input_data: formData.value })
    result.value = res
    // Add to history
    history.value.unshift({
      status: res.status,
      duration_ms: res.duration_ms,
      time: new Date().toLocaleTimeString(),
      result: res,
    })
    if (history.value.length > 10) history.value.pop()
    currentHistoryIdx.value = 0
  } catch (e: any) {
    ElMessage.error(e?.message || '执行失败')
  } finally {
    running.value = false
  }
}

function viewHistory(idx: number) {
  currentHistoryIdx.value = idx
  result.value = history.value[idx].result
}

function copyFinal() {
  if (!finalOutput.value) return
  navigator.clipboard.writeText(finalOutput.value).then(() => ElMessage.success('已复制到剪贴板'))
}

async function downloadDoc() {
  if (!finalOutput.value) return
  downloading.value = true
  try {
    const blob = await workflowApi.exportDoc(workflowId, {
      content: finalOutput.value,
      title: workflow.value.name || '工作流输出文档',
      filename: workflow.value.name || 'output',
    })
    const url = URL.createObjectURL(blob as unknown as Blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${workflow.value.name || 'output'}.docx`
    a.click()
    URL.revokeObjectURL(url)
    ElMessage.success('文档已下载')
  } catch {
    ElMessage.error('文档生成失败')
  } finally {
    downloading.value = false
  }
}

onMounted(async () => {
  const wf = await workflowApi.get(workflowId)
  workflow.value = wf
  nodes.value = wf.definition?.nodes || []
  // Init form data
  const fd: Record<string, string> = {}
  const fields = inputFields.value.length ? inputFields.value.map((f: any) => f.key) : inferredFields.value
  for (const f of fields) fd[f] = ''
  formData.value = fd
})
</script>

<style scoped>
.run-page {
  display: flex; flex-direction: column;
  height: calc(100vh - 56px); background: #fff;
}

.run-header {
  height: 52px; background: #fff; border-bottom: 1px solid #e5e5e5;
  display: flex; align-items: center; gap: 16px; padding: 0 20px;
  flex-shrink: 0;
}
.run-title { display: flex; align-items: center; gap: 12px; }
.run-icon { font-size: 26px; }
.run-name { font-size: 16px; font-weight: 600; color: #0d0d0d; }
.run-desc { font-size: 12px; color: #9ca3af; margin-top: 2px; }

.run-body {
  flex: 1; display: flex; overflow: hidden; gap: 0;
}

/* Left input panel */
.input-panel {
  width: 320px; flex-shrink: 0; overflow-y: auto;
  padding: 20px 16px; border-right: 1px solid #e5e5e5; background: #f9f9f9;
}
.panel-card { padding: 0; }
.panel-card-title {
  font-size: 14px; font-weight: 600; color: #0d0d0d; margin-bottom: 16px;
}
.run-form :deep(.el-form-item__label) {
  font-size: 13px; font-weight: 500; color: #374151;
}
.hint-text { font-size: 12px; color: #9ca3af; margin-bottom: 12px; }
.no-input-hint {
  text-align: center; color: #9ca3af; font-size: 13px;
  padding: 24px 0; background: #f9f9f9; border-radius: 8px; border: 1px solid #e5e5e5;
}

/* History */
.history-item {
  display: flex; align-items: center; gap: 8px; padding: 6px 8px;
  border-radius: 6px; cursor: pointer; transition: background 0.15s;
  font-size: 12px;
}
.history-item:hover { background: #f3f4f6; }
.history-item.active { background: #f0fdf9; }
.history-time { color: #6b7280; flex: 1; }
.history-ms { color: #9ca3af; }

/* Right output panel */
.output-panel {
  flex: 1; overflow-y: auto; padding: 20px 24px;
}

.empty-output {
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; height: 100%; color: #bbb;
}
.empty-icon { font-size: 64px; margin-bottom: 16px; }
.empty-title { font-size: 18px; font-weight: 500; color: #6b7280; margin-bottom: 8px; }
.empty-desc { font-size: 13px; color: #d1d5db; }

.running-state {
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; height: 100%;
}
.spin { animation: spin 1s linear infinite; }
@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }

.result-status-bar {
  display: flex; align-items: center; gap: 12px; margin-bottom: 16px;
}
.result-duration { color: #6b7280; font-size: 13px; flex: 1; }

.error-box {
  background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px;
  padding: 12px 16px; color: #dc2626; margin-bottom: 16px; font-size: 14px;
}

/* Hero output */
.output-hero {
  border: 1px solid #d1fae5; border-radius: 12px; overflow: hidden;
  box-shadow: 0 2px 12px rgba(16,163,127,0.1); margin-bottom: 16px;
}
.output-hero-doc {
  border-color: #bfdbfe;
  box-shadow: 0 2px 12px rgba(59,130,246,0.1);
}
.output-hero-doc .output-hero-header {
  background: #eff6ff;
  color: #1d4ed8; border-bottom-color: #bfdbfe;
}
.output-hero-header {
  background: #f0fdf9;
  padding: 12px 20px; display: flex; align-items: center; justify-content: space-between;
  font-size: 15px; font-weight: 600; color: #065f46;
  border-bottom: 1px solid #d1fae5;
}
.output-hero-actions { display: flex; gap: 8px; }
.doc-output-hint {
  background: #eff6ff; border-bottom: 1px solid #bfdbfe;
  padding: 6px 20px; font-size: 12px; color: #1d4ed8;
}
.output-hero-body {
  padding: 20px; font-size: 14px; line-height: 1.9; color: #0d0d0d;
  background: #fff; max-height: 500px; overflow-y: auto;
}

/* Execution trace */
.trace-collapse { border: 1px solid #e5e5e5; border-radius: 10px; overflow: hidden; }
.trace-title { font-size: 13px; color: #9ca3af; }
.trace-list { padding: 8px 0; }
.trace-item {
  border-left: 2px solid #e5e5e5; padding: 8px 12px; margin-bottom: 6px;
  border-radius: 0 6px 6px 0; background: #f9f9f9;
}
.trace-item.ok { border-left-color: #10a37f; }
.trace-item.error { border-left-color: #ef4444; }
.trace-item-header {
  display: flex; align-items: center; gap: 8px; font-size: 13px; margin-bottom: 4px;
}
.trace-node-name { font-weight: 500; color: #0d0d0d; }
.trace-branch { font-size: 12px; color: #9ca3af; }
.trace-ai-content {
  font-size: 13px; line-height: 1.7; color: #374151;
  background: #f0fdf9; border: 1px solid #d1fae5; border-radius: 6px;
  padding: 8px; margin-top: 4px; max-height: 120px; overflow-y: auto;
}
.trace-kb-results { margin-top: 4px; }
.trace-kb-item {
  background: #f3f4f6; border-radius: 4px; padding: 5px 8px;
  margin-bottom: 3px; font-size: 12px;
}
.kb-source { color: #9ca3af; display: block; margin-bottom: 2px; }

/* Markdown body styles */
.markdown-body :deep(h1),
.markdown-body :deep(h2),
.markdown-body :deep(h3),
.markdown-body :deep(h4) {
  font-weight: 600; margin: 12px 0 6px; line-height: 1.4; color: #0d0d0d;
}
.markdown-body :deep(h1) { font-size: 20px; }
.markdown-body :deep(h2) { font-size: 17px; border-bottom: 1px solid #e5e5e5; padding-bottom: 4px; }
.markdown-body :deep(h3) { font-size: 15px; }
.markdown-body :deep(h4) { font-size: 14px; }
.markdown-body :deep(p) { margin: 6px 0; line-height: 1.8; }
.markdown-body :deep(strong) { font-weight: 600; color: #0d0d0d; }
.markdown-body :deep(em) { font-style: italic; color: #6b7280; }
.markdown-body :deep(ul),
.markdown-body :deep(ol) { padding-left: 20px; margin: 6px 0; }
.markdown-body :deep(li) { margin: 3px 0; line-height: 1.7; }
.markdown-body :deep(blockquote) {
  border-left: 3px solid #10a37f; margin: 8px 0;
  padding: 4px 12px; background: #f0fdf9; color: #6b7280;
  border-radius: 0 6px 6px 0;
}
.markdown-body :deep(code) {
  background: #f3f4f6; border-radius: 4px;
  padding: 1px 5px; font-size: 13px; font-family: 'Consolas', monospace; color: #c7254e;
}
.markdown-body :deep(pre) {
  background: #1e1e1e; color: #d4d4d4; border-radius: 8px;
  padding: 12px; overflow-x: auto; margin: 8px 0;
}
.markdown-body :deep(pre code) { background: none; padding: 0; color: inherit; }
.markdown-body :deep(hr) { border: none; border-top: 1px solid #e5e5e5; margin: 12px 0; }
.markdown-body :deep(table) { border-collapse: collapse; width: 100%; margin: 8px 0; }
.markdown-body :deep(th),
.markdown-body :deep(td) { border: 1px solid #e5e5e5; padding: 6px 12px; font-size: 13px; }
.markdown-body :deep(th) { background: #f9f9f9; font-weight: 600; }
</style>
