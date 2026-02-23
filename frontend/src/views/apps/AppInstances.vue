<template>
  <div class="page-container">
    <div class="page-header">
      <h2>我的应用</h2>
      <el-button type="primary" @click="router.push('/apps')">去应用市场</el-button>
    </div>

    <el-row :gutter="16" v-loading="loading">
      <el-col :span="6" v-for="inst in instances" :key="inst.id" style="margin-bottom:16px;">
        <el-card class="inst-card" shadow="never">
          <div class="inst-header">
            <div class="inst-name">{{ inst.name }}</div>
            <el-tag :type="inst.is_active ? 'success' : 'info'" size="small">
              {{ inst.is_active ? '运行中' : '已停用' }}
            </el-tag>
          </div>
          <div class="inst-desc">{{ inst.description || '暂无描述' }}</div>
          <div class="inst-meta">
            <span v-if="inst.is_public"><el-tag size="small" type="warning">公开</el-tag></span>
            <span v-else><el-tag size="small" type="info">私有</el-tag></span>
          </div>
          <div class="inst-type-badge" v-if="inst.workflow_id">
            <el-tag size="small" type="success" effect="light">⚡ 工作流应用</el-tag>
          </div>
          <div class="inst-actions">
            <el-button v-if="inst.workflow_id" size="small" type="success" plain @click="runWorkflow(inst.workflow_id)">▶ 运行</el-button>
            <el-button v-else size="small" type="primary" plain @click="openChat(inst)">对话</el-button>
            <el-button size="small" @click="editInstance(inst)">配置</el-button>
            <el-button size="small" type="danger" @click="deleteInstance(inst)">删除</el-button>
          </div>
        </el-card>
      </el-col>
      <el-col :span="24" v-if="instances.length === 0 && !loading">
        <el-empty description="暂无应用，去应用市场部署一个吧">
          <el-button type="primary" @click="router.push('/apps')">去应用市场</el-button>
        </el-empty>
      </el-col>
    </el-row>

    <!-- Chat Dialog -->
    <el-dialog v-model="chatDialogVisible" :title="chatInstance?.name" width="700px" :close-on-click-modal="false">
      <div class="app-chat">
        <div class="app-messages" ref="appMsgRef">
          <div v-for="(msg, i) in appMessages" :key="i" class="app-msg" :class="msg.role">
            <div class="app-msg-body" v-html="renderMd(msg.content)" />
          </div>
          <div v-if="appStreaming" class="app-msg assistant">
            <div class="app-msg-body" v-html="renderMd(appStreamContent)" />
            <span class="typing-cursor">▋</span>
          </div>
        </div>
        <div class="app-input">
          <el-input
            v-model="appInput"
            type="textarea"
            :rows="2"
            placeholder="输入消息..."
            @keydown.ctrl.enter="sendAppMessage"
          />
          <el-button type="primary" :loading="appStreaming" @click="sendAppMessage" style="margin-top:8px;width:100%;">
            发送
          </el-button>
        </div>
      </div>
    </el-dialog>

    <!-- Edit Dialog -->
    <el-dialog v-model="editDialogVisible" title="应用配置" width="480px">
      <el-form :model="editForm" label-width="90px">
        <el-form-item label="应用名称"><el-input v-model="editForm.name" /></el-form-item>
        <el-form-item label="描述"><el-input v-model="editForm.description" type="textarea" :rows="2" /></el-form-item>
        <el-form-item label="绑定模型">
          <el-select v-model="editForm.model_config_id" clearable style="width:100%">
            <el-option v-for="m in chatModels" :key="m.id" :label="m.display_name" :value="m.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="绑定知识库">
          <el-select v-model="editForm.knowledge_base_id" clearable style="width:100%">
            <el-option v-for="kb in knowledgeBases" :key="kb.id" :label="kb.name" :value="kb.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="绑定工作流">
          <el-select v-model="editForm.workflow_id" clearable style="width:100%" placeholder="选择工作流（可选）">
            <el-option v-for="wf in editWorkflows" :key="wf.id" :label="wf.name" :value="wf.id" />
          </el-select>
          <div v-if="editForm.workflow_id" style="font-size:11px;color:#67c23a;margin-top:4px;">⚡ 已绑定工作流，点击「▶ 运行」直接使用</div>
        </el-form-item>
        <el-form-item label="状态"><el-switch v-model="editForm.is_active" /></el-form-item>
        <el-form-item label="公开访问"><el-switch v-model="editForm.is_public" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveEdit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, nextTick } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { marked } from 'marked'
import { appApi, modelApi, knowledgeApi, workflowApi } from '@/api'
import type { AppInstance, ModelConfig, KnowledgeBase, Workflow } from '@/types'

const router = useRouter()
const route = useRoute()

function runWorkflow(workflowId: number) {
  router.push(`/workflows/${workflowId}/run`)
}
const instances = ref<AppInstance[]>([])
const loading = ref(false)
const chatModels = ref<ModelConfig[]>([])
const knowledgeBases = ref<KnowledgeBase[]>([])
const editWorkflows = ref<Workflow[]>([])

const chatDialogVisible = ref(false)
const chatInstance = ref<AppInstance | null>(null)
const appMessages = ref<any[]>([])
const appInput = ref('')
const appStreaming = ref(false)
const appStreamContent = ref('')
const appMsgRef = ref<HTMLElement>()

const editDialogVisible = ref(false)
const editingInstance = ref<AppInstance | null>(null)
const saving = ref(false)
const editForm = reactive<any>({})

function renderMd(text: string) { return marked(text) as string }

async function loadData() {
  loading.value = true
  try {
    const res = await appApi.listInstances({ page_size: 100 })
    instances.value = res.items
  } finally {
    loading.value = false
  }
}

function openChat(inst: AppInstance) {
  chatInstance.value = inst
  appMessages.value = []
  appInput.value = ''
  chatDialogVisible.value = true
}

async function sendAppMessage() {
  if (!appInput.value.trim() || !chatInstance.value) return
  const content = appInput.value.trim()
  appMessages.value.push({ role: 'user', content })
  appInput.value = ''
  appStreaming.value = true
  appStreamContent.value = ''

  await nextTick()
  if (appMsgRef.value) appMsgRef.value.scrollTop = appMsgRef.value.scrollHeight

  const modelId = chatInstance.value.model_config_id
  if (!modelId) {
    appMessages.value.push({ role: 'assistant', content: '该应用未配置模型，请先在配置中绑定模型。' })
    appStreaming.value = false
    return
  }

  try {
    const payload: any = {
      model_config_id: modelId,
      messages: appMessages.value.map(m => ({ role: m.role, content: m.content })),
      stream: true,
    }
    if (chatInstance.value.knowledge_base_id) payload.knowledge_base_id = chatInstance.value.knowledge_base_id

    const response = await fetch('/api/v1/models/chat', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${localStorage.getItem('access_token')}`,
      },
      body: JSON.stringify(payload),
    })

    const reader = response.body!.getReader()
    const decoder = new TextDecoder()
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      const text = decoder.decode(value)
      for (const line of text.split('\n')) {
        if (line.startsWith('data: ')) {
          const data = line.slice(6).trim()
          if (data === '[DONE]') break
          try {
            const parsed = JSON.parse(data)
            if (parsed.type === 'content') {
              appStreamContent.value += parsed.content
              await nextTick()
              if (appMsgRef.value) appMsgRef.value.scrollTop = appMsgRef.value.scrollHeight
            }
          } catch {}
        }
      }
    }
    appMessages.value.push({ role: 'assistant', content: appStreamContent.value })
    appStreamContent.value = ''
  } catch (e: any) {
    ElMessage.error('对话失败')
  } finally {
    appStreaming.value = false
  }
}

function editInstance(inst: AppInstance) {
  editingInstance.value = inst
  Object.assign(editForm, {
    name: inst.name, description: inst.description || '',
    model_config_id: inst.model_config_id || null,
    knowledge_base_id: inst.knowledge_base_id || null,
    workflow_id: inst.workflow_id || null,
    is_active: inst.is_active, is_public: inst.is_public,
  })
  editDialogVisible.value = true
}

async function saveEdit() {
  if (!editingInstance.value) return
  saving.value = true
  try {
    await appApi.updateInstance(editingInstance.value.id, editForm)
    ElMessage.success('保存成功')
    editDialogVisible.value = false
    await loadData()
  } finally {
    saving.value = false
  }
}

async function deleteInstance(inst: AppInstance) {
  await ElMessageBox.confirm(`确认删除应用「${inst.name}」？`, '警告', { type: 'warning' })
  await appApi.deleteInstance(inst.id)
  ElMessage.success('删除成功')
  await loadData()
}

onMounted(async () => {
  await loadData()
  const [models, kbs, wfs] = await Promise.all([
    modelApi.listConfigs({ model_type: 'chat' }),
    knowledgeApi.listBases({ page_size: 100 }),
    workflowApi.list({ page_size: 100 }),
  ])
  chatModels.value = models.filter((m: ModelConfig) => m.is_active)
  knowledgeBases.value = kbs.items
  editWorkflows.value = wfs.items
})
</script>

<style scoped>
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
.page-header h2 { font-size: 20px; font-weight: 700; color: #0d0d0d; }
.inst-card {
  border-radius: 12px;
  border: 1px solid #e5e5e5 !important;
  box-shadow: 0 1px 3px rgba(0,0,0,0.04) !important;
  transition: all 0.18s;
}
.inst-card:hover { border-color: #10a37f !important; box-shadow: 0 4px 12px rgba(16,163,127,0.1) !important; }
.inst-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.inst-name { font-size: 15px; font-weight: 600; color: #0d0d0d; }
.inst-desc { font-size: 13px; color: #6b7280; margin-bottom: 10px; min-height: 36px; }
.inst-meta { margin-bottom: 10px; }
.inst-type-badge { margin-bottom: 8px; }
.inst-actions { display: flex; gap: 6px; flex-wrap: wrap; }
.inst-actions :deep(.el-button--success.is-plain) { color: #10a37f !important; background: #f0fdf9 !important; border-color: #10a37f !important; }
.inst-actions :deep(.el-button--success.is-plain:hover) { color: #fff !important; background: #10a37f !important; }
.inst-actions :deep(.el-button--primary.is-plain) { color: #3b82f6 !important; background: #eff6ff !important; border-color: #3b82f6 !important; }
.inst-actions :deep(.el-button--primary.is-plain:hover) { color: #fff !important; background: #3b82f6 !important; }
.inst-actions :deep(.el-button--danger) { color: #fff !important; }
.inst-actions :deep(.el-button--default) { color: #374151 !important; }
.app-chat { display: flex; flex-direction: column; height: 400px; }
.app-messages { flex: 1; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 12px; background: #f9f9f9; border-radius: 8px; margin-bottom: 12px; border: 1px solid #e5e5e5; }
.app-msg { max-width: 80%; }
.app-msg.user { align-self: flex-end; }
.app-msg.assistant { align-self: flex-start; }
.app-msg-body { background: #fff; border-radius: 10px; padding: 10px 14px; font-size: 14px; line-height: 1.6; box-shadow: 0 1px 3px rgba(0,0,0,0.05); border: 1px solid #e5e5e5; }
.app-msg.user .app-msg-body { background: #f3f4f6; border-color: #e5e5e5; color: #0d0d0d; }
.typing-cursor { display: inline-block; width: 2px; height: 1em; background: #9ca3af; margin-left: 1px; vertical-align: text-bottom; animation: blink 0.8s infinite; }
@keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }
</style>
