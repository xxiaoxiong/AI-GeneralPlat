<template>
  <div class="agent-list-page">
    <div class="page-header">
      <div class="header-left">
        <h2>🤖 Agent 管理</h2>
        <span class="subtitle">创建和管理 AI Agent，支持 ReAct 自主推理与工具调用</span>
      </div>
      <el-button type="primary" :icon="Plus" @click="openCreate">新建 Agent</el-button>
    </div>

    <div class="agent-grid" v-loading="loading">
      <div v-if="agents.length === 0 && !loading" class="empty-state">
        <el-empty description="暂无 Agent，点击「新建 Agent」开始创建">
          <el-button type="primary" @click="openCreate">新建 Agent</el-button>
        </el-empty>
      </div>

      <div
        v-for="agent in agents"
        :key="agent.id"
        class="agent-card"
        @click="goChat(agent)"
      >
        <div class="card-icon">{{ agent.icon || '🤖' }}</div>
        <div class="card-body">
          <div class="card-name">{{ agent.name }}</div>
          <div class="card-desc">{{ agent.description || '暂无描述' }}</div>
          <div class="card-meta">
            <el-tag size="small" type="info">{{ agent.tools?.length || 0 }} 个工具</el-tag>
            <el-tag size="small" type="success" v-if="agent.knowledge_base_id">含知识库</el-tag>
            <el-tag size="small">最多 {{ agent.max_iterations }} 轮</el-tag>
          </div>
        </div>
        <div class="card-actions" @click.stop>
          <el-button style="color: #fff;" size="small" type="primary" plain @click="goChat(agent)">
            <el-icon><ChatDotRound /></el-icon> 对话
          </el-button>
          <el-button size="small" plain @click="openEdit(agent)">
            <el-icon><Edit /></el-icon>
          </el-button>
          <el-button size="small" type="danger" plain @click="deleteAgent(agent)">
            <el-icon><Delete /></el-icon>
          </el-button>
        </div>
      </div>
    </div>

    <!-- 新建/编辑 Dialog -->
    <el-dialog
      v-model="dialogVisible"
      :title="editingAgent ? '编辑 Agent' : '新建 Agent'"
      width="680px"
      :close-on-click-modal="false"
    >
      <el-form :model="form" label-width="100px" class="agent-form">
        <el-row :gutter="16">
          <!-- <el-col :span="4">
            <el-form-item label="图标">
              <el-input v-model="form.icon" maxlength="2" style="text-align:center;font-size:24px" />
            </el-form-item>
          </el-col> -->
          <el-col :span="24">
            <el-form-item label="名称" required>
              <el-input v-model="form.name" placeholder="如：数据分析助手" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-form-item label="描述">
          <el-input v-model="form.description" type="textarea" :rows="2" placeholder="描述 Agent 的用途" />
        </el-form-item>

        <el-form-item label="AI 模型" required>
          <el-select v-model="form.model_config_id" placeholder="选择模型" style="width:100%">
            <el-option
              v-for="m in chatModels"
              :key="m.id"
              :label="m.display_name"
              :value="m.id"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="关联知识库">
          <el-select v-model="form.knowledge_base_id" placeholder="可选，赋予知识库检索能力" clearable style="width:100%">
            <el-option
              v-for="kb in knowledgeBases"
              :key="kb.id"
              :label="kb.name"
              :value="kb.id"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="系统提示词">
          <el-input
            v-model="form.system_prompt"
            type="textarea"
            :rows="3"
            placeholder="定义 Agent 的角色和行为准则"
          />
        </el-form-item>

        <el-form-item label="启用工具">
          <div class="tools-grid">
            <el-checkbox-group v-model="form.tools">
              <el-checkbox
                v-for="tool in allTools"
                :key="tool.name"
                :label="tool.name"
                class="tool-checkbox"
              >
                <div class="tool-item">
                  <span class="tool-name">{{ tool.display_name }}</span>
                  <span class="tool-desc">{{ tool.description }}</span>
                </div>
              </el-checkbox>
            </el-checkbox-group>
          </div>
        </el-form-item>

        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="最大迭代">
              <el-input-number v-model="form.max_iterations" :min="1" :max="20" style="width:100%" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="温度">
              <el-slider v-model="tempSlider" :min="0" :max="100" :step="5" show-input />
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>

      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveAgent">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Edit, Delete, ChatDotRound } from '@element-plus/icons-vue'
import { agentApi, modelApi, knowledgeApi } from '@/api'

const router = useRouter()

const loading = ref(false)
const saving = ref(false)
const agents = ref<any[]>([])
const chatModels = ref<any[]>([])
const knowledgeBases = ref<any[]>([])
const allTools = ref<any[]>([])
const dialogVisible = ref(false)
const editingAgent = ref<any>(null)

const defaultForm = () => ({
  name: '',
  description: '',
  icon: '🤖',
  model_config_id: null as number | null,
  system_prompt: '你是一个智能助手，可以使用工具来帮助用户解决问题。',
  max_iterations: 10,
  temperature: '0.7',
  tools: [] as string[],
  knowledge_base_id: null as number | null,
})
const form = ref(defaultForm())
const tempSlider = computed({
  get: () => Math.round(parseFloat(form.value.temperature) * 100),
  set: (v: number) => { form.value.temperature = (v / 100).toFixed(2) },
})

async function loadAgents() {
  loading.value = true
  try {
    const res = await agentApi.list({ page_size: 100 })
    agents.value = res.items || []
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editingAgent.value = null
  form.value = defaultForm()
  dialogVisible.value = true
}

function openEdit(agent: any) {
  editingAgent.value = agent
  form.value = {
    name: agent.name,
    description: agent.description || '',
    icon: agent.icon || '🤖',
    model_config_id: agent.model_config_id,
    system_prompt: agent.system_prompt || '',
    max_iterations: agent.max_iterations || 10,
    temperature: String(agent.temperature || '0.7'),
    tools: (agent.tools || []).map((t: any) => typeof t === 'string' ? t : t.name),
    knowledge_base_id: agent.knowledge_base_id || null,
  }
  dialogVisible.value = true
}

async function saveAgent() {
  if (!form.value.name.trim()) {
    ElMessage.warning('请输入 Agent 名称')
    return
  }
  saving.value = true
  try {
    const payload = {
      ...form.value,
      tools: form.value.tools.map(name => ({ name, enabled: true })),
    }
    if (editingAgent.value) {
      await agentApi.update(editingAgent.value.id, payload)
      ElMessage.success('已更新')
    } else {
      await agentApi.create(payload)
      ElMessage.success('已创建')
    }
    dialogVisible.value = false
    await loadAgents()
  } finally {
    saving.value = false
  }
}

async function deleteAgent(agent: any) {
  await ElMessageBox.confirm(`确定删除 Agent「${agent.name}」？`, '确认删除', { type: 'warning' })
  await agentApi.delete(agent.id)
  ElMessage.success('已删除')
  await loadAgents()
}

function goChat(agent: any) {
  router.push(`/agents/${agent.id}/chat`)
}

onMounted(async () => {
  await loadAgents()
  try {
    const [models, kbs, tools] = await Promise.all([
      modelApi.listConfigs({ model_type: 'chat' }),
      knowledgeApi.listBases({ page_size: 100 }),
      agentApi.listTools(),
    ])
    chatModels.value = models.filter((m: any) => m.model_type === 'chat' && m.is_active)
    knowledgeBases.value = kbs.items || []
    allTools.value = tools.items || []
  } catch { /* ignore */ }
})
</script>

<style scoped>
.agent-list-page { padding: 24px; background: #fff; min-height: calc(100vh - 56px); }
.page-header {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 24px;
}
.header-left h2 { margin: 0 0 4px; font-size: 20px; font-weight: 700; color: #0d0d0d; }
.subtitle { color: #9ca3af; font-size: 13px; }

.agent-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 16px;
}
.agent-card {
  background: #fff;
  border: 1px solid #e5e5e5;
  border-radius: 12px;
  padding: 20px;
  cursor: pointer;
  transition: all 0.18s;
  display: flex;
  flex-direction: column;
  gap: 12px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.04);
}
.agent-card:hover {
  border-color: #10a37f;
  box-shadow: 0 4px 16px rgba(16,163,127,0.12);
  transform: translateY(-2px);
}
.card-icon { font-size: 34px; line-height: 1; }
.card-name { font-size: 15px; font-weight: 600; color: #0d0d0d; margin-bottom: 4px; }
.card-desc { font-size: 13px; color: #6b7280; line-height: 1.5; min-height: 38px; }
.card-meta { display: flex; gap: 6px; flex-wrap: wrap; }
.card-actions { display: flex; gap: 8px; border-top: 1px solid #f3f4f6; padding-top: 12px; }

.tools-grid { width: 100%; }
.tool-checkbox { display: block; margin: 0 0 8px; height: auto; }
.tool-item { display: flex; flex-direction: column; }
.tool-name { font-size: 13px; font-weight: 500; color: #0d0d0d; }
.tool-desc { font-size: 11px; color: #9ca3af; line-height: 1.4; white-space: normal; }

.agent-form :deep(.el-form-item__label) { font-weight: 500; color: #374151; }
</style>
