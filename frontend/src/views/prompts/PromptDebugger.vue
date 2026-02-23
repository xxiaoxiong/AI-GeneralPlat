<template>
  <div class="debugger-page">
    <div class="page-header">
      <div class="header-left">
        <el-button text @click="$router.push('/prompts')">← 返回模板库</el-button>
        <h2>提示词调试器</h2>
      </div>
    </div>

    <div class="debugger-layout">
      <!-- Left: Editor -->
      <div class="editor-panel">
        <el-card class="panel-card">
          <template #header>
            <div class="card-header">
              <span>✏️ 提示词编辑</span>
              <div class="header-actions">
                <el-select v-model="selectedTemplateId" clearable placeholder="从模板库加载" style="width:180px" @change="loadTemplate">
                  <el-option v-for="t in templateOptions" :key="t.id" :label="t.name" :value="t.id" />
                </el-select>
                <el-button size="small" @click="saveAsTemplate">💾 保存为模板</el-button>
              </div>
            </div>
          </template>

          <el-form label-width="80px" size="small">
            <el-form-item label="系统提示">
              <el-input v-model="systemPrompt" type="textarea" :rows="2" placeholder="可选，设置 AI 角色" />
            </el-form-item>
            <el-form-item label="提示词">
              <el-input
                v-model="promptContent"
                type="textarea"
                :rows="10"
                placeholder="输入提示词，使用 {{变量名}} 定义变量"
                @input="onContentChange"
              />
            </el-form-item>
          </el-form>

          <!-- Variables -->
          <div v-if="detectedVars.length" class="vars-section">
            <div class="vars-title">📌 变量填写</div>
            <el-form label-width="100px" size="small">
              <el-form-item v-for="v in detectedVars" :key="v" :label="v">
                <el-input v-model="varValues[v]" :placeholder="`填写 ${v} 的值`" @input="updatePreview" />
              </el-form-item>
            </el-form>
          </div>

          <!-- Preview -->
          <div class="preview-section">
            <div class="preview-title">
              👁 实时预览
              <el-tag v-if="missingVars.length" type="warning" size="small">
                缺少变量: {{ missingVars.join(', ') }}
              </el-tag>
            </div>
            <div class="preview-content">{{ renderedPrompt || '（请输入提示词）' }}</div>
          </div>

          <!-- Model selection -->
          <div class="model-section">
            <div class="model-title">🤖 选择对比模型（可多选）</div>
            <el-checkbox-group v-model="selectedModelIds">
              <el-checkbox v-for="m in chatModels" :key="m.id" :label="m.id">
                {{ m.display_name }}
              </el-checkbox>
            </el-checkbox-group>
          </div>

          <el-button
            type="primary"
            :loading="running"
            :disabled="!renderedPrompt || selectedModelIds.length === 0"
            style="width:100%;margin-top:12px"
            @click="runDebug"
          >
            ▶ 运行调试{{ selectedModelIds.length > 1 ? `（${selectedModelIds.length} 个模型对比）` : '' }}
          </el-button>
        </el-card>
      </div>

      <!-- Right: Results -->
      <div class="results-panel">
        <el-card class="panel-card" v-if="results.length || running">
          <template #header>
            <div class="card-header">
              <span>📊 调试结果</span>
              <el-button size="small" text @click="results = []">清空</el-button>
            </div>
          </template>

          <div v-if="running" class="running-hint">
            <el-icon class="is-loading"><Loading /></el-icon> 正在运行...
          </div>

          <div v-for="(r, idx) in results" :key="idx" class="result-item">
            <div class="result-header">
              <span class="result-model">🤖 {{ r.model_name || `模型 ${r.model_config_id}` }}</span>
              <div class="result-meta">
                <el-tag v-if="r.error" type="danger" size="small">失败</el-tag>
                <el-tag v-else type="success" size="small">成功</el-tag>
                <span class="result-time">{{ r.duration_ms }}ms</span>
                <span v-if="r.usage" class="result-tokens">
                  {{ r.usage.prompt_tokens || 0 }}+{{ r.usage.completion_tokens || 0 }} tokens
                </span>
              </div>
            </div>
            <div v-if="r.error" class="result-error">{{ r.error }}</div>
            <div v-else class="result-content markdown-body" v-html="renderMd(r.content)" />
            <div class="result-actions">
              <el-button size="small" @click="copyResult(r.content)">复制</el-button>
              <el-button size="small" @click="useAsPrompt(r.content)">用作提示词</el-button>
            </div>
          </div>

          <div v-if="!running && results.length === 0" class="empty-hint">
            点击「运行调试」查看结果
          </div>
        </el-card>

        <div v-else class="results-placeholder">
          <div class="placeholder-icon">🔬</div>
          <div class="placeholder-text">填写提示词并选择模型，点击运行查看结果</div>
          <div class="placeholder-tips">
            <div>💡 支持多模型同时对比，找出最佳提示词</div>
            <div>💡 使用 &#123;&#123;变量名&#125;&#125; 定义可复用变量</div>
            <div>💡 满意后可保存为模板，在工作流中引用</div>
          </div>
        </div>
      </div>
    </div>

    <!-- Save as Template Dialog -->
    <el-dialog v-model="saveDialogVisible" title="保存为模板" width="480px">
      <el-form :model="saveForm" label-width="80px">
        <el-form-item label="模板名称" required>
          <el-input v-model="saveForm.name" />
        </el-form-item>
        <el-form-item label="分类">
          <el-select v-model="saveForm.category" style="width:100%">
            <el-option label="通用" value="general" />
            <el-option label="分析" value="analysis" />
            <el-option label="写作" value="writing" />
            <el-option label="编程" value="coding" />
            <el-option label="总结" value="summary" />
            <el-option label="翻译" value="translation" />
            <el-option label="问答" value="qa" />
            <el-option label="其他" value="other" />
          </el-select>
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="saveForm.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="公开">
          <el-switch v-model="saveForm.is_public" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="saveDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="confirmSaveTemplate">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Loading } from '@element-plus/icons-vue'
import { marked } from 'marked'
import { promptApi, modelApi } from '@/api'
import type { ModelConfig } from '@/types'

const route = useRoute()

function renderMd(text: string): string {
  if (!text) return ''
  return marked(text) as string
}

const promptContent = ref('')
const systemPrompt = ref('')
const varValues = reactive<Record<string, string>>({})
const detectedVars = ref<string[]>([])
const renderedPrompt = ref('')
const missingVars = ref<string[]>([])
const selectedModelIds = ref<number[]>([])
const chatModels = ref<ModelConfig[]>([])
const running = ref(false)
const results = ref<any[]>([])

const templateOptions = ref<any[]>([])
const selectedTemplateId = ref<number | null>(null)

function onContentChange() {
  const matches = promptContent.value.match(/\{\{(\w+)\}\}/g) || []
  const vars = [...new Set(matches.map((m: string) => m.slice(2, -2)))]
  // Remove vars no longer in content
  for (const k of Object.keys(varValues)) {
    if (!vars.includes(k)) delete varValues[k]
  }
  // Add new vars
  for (const v of vars) {
    if (!(v in varValues)) varValues[v] = ''
  }
  detectedVars.value = vars
  updatePreview()
}

async function updatePreview() {
  if (!promptContent.value) {
    renderedPrompt.value = ''
    missingVars.value = []
    return
  }
  try {
    const res = await promptApi.render({ content: promptContent.value, variables: { ...varValues } })
    renderedPrompt.value = res.rendered
    missingVars.value = res.missing_vars || []
  } catch {
    // Fallback: simple replace
    let text = promptContent.value
    for (const [k, v] of Object.entries(varValues)) {
      text = text.split(`{{${k}}}`).join(v)
    }
    renderedPrompt.value = text
    missingVars.value = []
  }
}

async function runDebug() {
  if (!renderedPrompt.value || selectedModelIds.value.length === 0) return
  running.value = true
  results.value = []
  try {
    const res = await promptApi.debug({
      content: promptContent.value,
      variables: { ...varValues },
      model_config_ids: selectedModelIds.value,
      system_prompt: systemPrompt.value || undefined,
    })
    results.value = res.results || []
  } catch (e: any) {
    ElMessage.error(e?.message || '调试失败')
  } finally {
    running.value = false
  }
}

function copyResult(content: string) {
  navigator.clipboard.writeText(content).then(() => ElMessage.success('已复制'))
}

function useAsPrompt(content: string) {
  promptContent.value = content
  onContentChange()
  ElMessage.success('已填入提示词编辑框')
}

async function loadTemplate(id: number | null) {
  if (!id) return
  try {
    const tpl = await promptApi.get(id)
    promptContent.value = tpl.content
    systemPrompt.value = ''
    onContentChange()
  } catch {
    ElMessage.error('加载模板失败')
  }
}

// Save as template
const saveDialogVisible = ref(false)
const saving = ref(false)
const saveForm = reactive({ name: '', category: 'general', description: '', is_public: false })

function saveAsTemplate() {
  if (!promptContent.value) return ElMessage.warning('请先输入提示词')
  saveForm.name = ''
  saveForm.description = ''
  saveDialogVisible.value = true
}

async function confirmSaveTemplate() {
  if (!saveForm.name.trim()) return ElMessage.warning('请输入模板名称')
  saving.value = true
  try {
    await promptApi.create({
      name: saveForm.name,
      category: saveForm.category,
      description: saveForm.description,
      content: promptContent.value,
      is_public: saveForm.is_public,
    })
    ElMessage.success('已保存为模板')
    saveDialogVisible.value = false
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  const [models, tpls] = await Promise.all([
    modelApi.listConfigs({ model_type: 'chat' }),
    promptApi.list({ page_size: 100, include_public: true }),
  ])
  chatModels.value = models.filter((m: ModelConfig) => m.is_active)
  templateOptions.value = tpls.items || []

  // Load from query param
  const tid = route.query.template_id
  if (tid) {
    selectedTemplateId.value = Number(tid)
    await loadTemplate(Number(tid))
  }
})
</script>

<style scoped>
.debugger-page { padding: 24px; height: calc(100vh - 56px); display: flex; flex-direction: column; background: #fff; }
.page-header { display: flex; align-items: center; margin-bottom: 16px; }
.header-left { display: flex; align-items: center; gap: 8px; }
.header-left h2 { font-size: 20px; font-weight: 700; color: #0d0d0d; margin: 0; }
.debugger-layout { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; flex: 1; min-height: 0; overflow: hidden; }
.editor-panel, .results-panel { overflow-y: auto; }
.panel-card { height: 100%; border: 1px solid #e5e5e5 !important; border-radius: 12px !important; box-shadow: 0 1px 3px rgba(0,0,0,0.04) !important; }
.card-header { display: flex; justify-content: space-between; align-items: center; }
.header-actions { display: flex; gap: 8px; align-items: center; }
.vars-section { border-top: 1px solid #f3f4f6; padding-top: 12px; margin-top: 12px; }
.vars-title { font-size: 13px; font-weight: 600; color: #374151; margin-bottom: 8px; }
.preview-section { border: 1px solid #e5e5e5; border-radius: 8px; padding: 12px; margin: 12px 0; background: #f9f9f9; }
.preview-title { font-size: 12px; font-weight: 600; color: #9ca3af; margin-bottom: 8px; display: flex; align-items: center; gap: 8px; }
.preview-content { font-size: 13px; color: #374151; white-space: pre-wrap; line-height: 1.7; max-height: 150px; overflow-y: auto; }
.model-section { margin-top: 12px; }
.model-title { font-size: 13px; font-weight: 600; color: #374151; margin-bottom: 8px; }
.running-hint { text-align: center; padding: 20px; color: #9ca3af; display: flex; align-items: center; justify-content: center; gap: 8px; }
.result-item { border: 1px solid #e5e5e5; border-radius: 10px; padding: 12px; margin-bottom: 12px; }
.result-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.result-model { font-size: 14px; font-weight: 600; color: #0d0d0d; }
.result-meta { display: flex; align-items: center; gap: 8px; }
.result-time { font-size: 12px; color: #9ca3af; }
.result-tokens { font-size: 12px; color: #6b7280; }
.result-error { color: #dc2626; font-size: 13px; background: #fef2f2; padding: 8px; border-radius: 6px; }
.result-content { font-size: 13px; line-height: 1.7; color: #0d0d0d; max-height: 400px; overflow-y: auto; }
.result-actions { display: flex; gap: 8px; margin-top: 8px; border-top: 1px solid #f3f4f6; padding-top: 8px; }
.results-placeholder { display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100%; color: #9ca3af; text-align: center; gap: 12px; }
.placeholder-icon { font-size: 48px; }
.placeholder-text { font-size: 15px; color: #6b7280; }
.placeholder-tips { font-size: 13px; color: #d1d5db; line-height: 2; }
.empty-hint { text-align: center; color: #9ca3af; padding: 40px; }

/* Markdown */
.markdown-body :deep(h1), .markdown-body :deep(h2), .markdown-body :deep(h3) { font-weight: 600; margin: 8px 0 4px; color: #0d0d0d; }
.markdown-body :deep(p) { margin: 4px 0; }
.markdown-body :deep(ul), .markdown-body :deep(ol) { padding-left: 20px; margin: 4px 0; }
.markdown-body :deep(code) { background: #f3f4f6; border-radius: 4px; padding: 1px 5px; font-size: 12px; color: #c7254e; }
.markdown-body :deep(pre) { background: #1e1e1e; color: #d4d4d4; border-radius: 8px; padding: 12px; overflow-x: auto; }
.markdown-body :deep(pre code) { background: none; padding: 0; color: inherit; }
</style>
