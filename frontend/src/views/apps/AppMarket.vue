<template>
  <div class="page-container">
    <div class="page-header">
      <h2>应用市场</h2>
      <el-input v-model="keyword" placeholder="搜索应用或工作流..." clearable style="width:240px;" @input="onSearch" />
    </div>

    <!-- Top-level tabs: AI应用 vs 工作流应用 -->
    <el-tabs v-model="mainTab" class="main-tabs" @tab-change="onMainTabChange">
      <el-tab-pane label="🤖 AI 应用" name="apps" />
      <el-tab-pane name="workflows">
        <template #label>
          <span>⚡ 工作流应用 <el-badge v-if="publishedWorkflows.length" :value="publishedWorkflows.length" type="primary" style="margin-left:4px;" /></span>
        </template>
      </el-tab-pane>
    </el-tabs>

    <!-- ── AI 应用 Tab ── -->
    <template v-if="mainTab === 'apps'">
      <el-tabs v-model="activeCategory" @tab-change="loadData" style="margin-bottom:16px;">
        <el-tab-pane label="全部" name="" />
        <el-tab-pane label="📄 法务合规" name="legal" />
        <el-tab-pane label="💰 财税金融" name="finance" />
        <el-tab-pane label="👥 人力资源" name="hr" />
        <el-tab-pane label="📦 采购供应链" name="procurement" />
        <el-tab-pane label="📣 市场营销" name="marketing" />
        <el-tab-pane label="🤝 客户服务" name="service" />
        <el-tab-pane label="🏛️ 政务办公" name="office" />
        <el-tab-pane label="⚖️ 合规管理" name="compliance" />
        <el-tab-pane label="💻 研发技术" name="tech" />
      </el-tabs>

      <el-row :gutter="16" v-loading="loading">
        <el-col :span="6" v-for="app in templates" :key="app.id" style="margin-bottom:16px;">
          <el-card class="app-card" shadow="never">
            <div class="app-icon">{{ app.icon || '🤖' }}</div>
            <div class="app-name">{{ app.display_name }}</div>
            <div class="app-desc">{{ app.description }}</div>
            <div class="app-meta">
              <el-tag size="small" type="info">{{ categoryLabel(app.category) }}</el-tag>
              <span class="app-usage">{{ app.usage_count }} 次使用</span>
            </div>
            <div class="app-tags" v-if="app.tags">
              <el-tag v-for="tag in app.tags.split(',')" :key="tag" size="small" style="margin-right:4px;margin-top:4px;">{{ tag }}</el-tag>
            </div>
            <el-button type="primary" size="small" style="width:100%;margin-top:12px;" @click="deployApp(app)">
              立即部署
            </el-button>
          </el-card>
        </el-col>
      </el-row>

      <div style="text-align:right;margin-top:8px;" v-if="total > pageSize">
        <el-pagination v-model:current-page="page" :page-size="pageSize" :total="total"
          layout="prev, pager, next" @current-change="loadData" />
      </div>
    </template>

    <!-- ── 工作流应用 Tab ── -->
    <template v-else>
      <div class="wf-market-hint">
        已发布的工作流可直接在此运行，无需部署配置。
        <el-button size="small" text type="primary" @click="router.push('/workflows')">去创建工作流 →</el-button>
      </div>

      <div v-if="wfLoading" style="text-align:center;padding:40px;">
        <el-icon class="is-loading" size="32"><Loading /></el-icon>
      </div>

      <el-empty v-else-if="filteredWorkflows.length === 0" description="暂无已发布的工作流">
        <el-button type="primary" @click="router.push('/workflows')">去创建工作流</el-button>
      </el-empty>

      <el-row v-else :gutter="16">
        <el-col :span="6" v-for="wf in filteredWorkflows" :key="wf.id" style="margin-bottom:16px;">
          <el-card class="app-card wf-card" shadow="never" @click="runWorkflow(wf.id)">
            <div class="app-icon">{{ wf.icon || '⚡' }}</div>
            <div class="wf-badge"><el-tag size="small" type="success">工作流</el-tag></div>
            <div class="app-name">{{ wf.name }}</div>
            <div class="app-desc">{{ wf.description || '暂无描述' }}</div>
            <div class="app-meta">
              <el-tag size="small" type="info" v-if="wf.category">{{ wf.category }}</el-tag>
              <span class="app-usage">v{{ wf.version }}</span>
            </div>
            <div class="wf-input-preview" v-if="getWfInputFields(wf).length">
              <span class="wf-input-label">输入参数：</span>
              <el-tag
                v-for="f in getWfInputFields(wf).slice(0, 3)"
                :key="f.key"
                size="small" type="warning" style="margin-right:4px;"
              >{{ f.label || f.key }}</el-tag>
              <span v-if="getWfInputFields(wf).length > 3" style="font-size:11px;color:#aaa;">+{{ getWfInputFields(wf).length - 3 }}</span>
            </div>
            <el-button type="success" size="small" style="width:100%;margin-top:12px;">
              ▶ 立即运行
            </el-button>
          </el-card>
        </el-col>
      </el-row>
    </template>

    <!-- Deploy Dialog (AI应用) -->
    <el-dialog v-model="deployDialogVisible" :title="`部署应用：${deployingApp?.display_name}`" width="520px">
      <el-form :model="deployForm" label-width="90px">
        <el-form-item label="应用名称">
          <el-input v-model="deployForm.name" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="deployForm.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="绑定模型">
          <el-select v-model="deployForm.model_config_id" placeholder="选择模型（可选）" clearable style="width:100%">
            <el-option v-for="m in chatModels" :key="m.id" :label="m.display_name" :value="m.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="绑定知识库">
          <el-select v-model="deployForm.knowledge_base_id" placeholder="选择知识库（可选）" clearable style="width:100%">
            <el-option v-for="kb in knowledgeBases" :key="kb.id" :label="kb.name" :value="kb.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="绑定工作流">
          <el-select v-model="deployForm.workflow_id" placeholder="选择已发布工作流（可选）" clearable style="width:100%">
            <el-option v-for="wf in publishedWorkflows" :key="wf.id" :label="wf.name" :value="wf.id" />
          </el-select>
          <div style="font-size:11px;color:#aaa;margin-top:4px;">绑定工作流后，应用将通过工作流引擎处理</div>
        </el-form-item>
        <el-form-item label="公开访问">
          <el-switch v-model="deployForm.is_public" active-text="公开" inactive-text="私有" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="deployDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="deploying" @click="confirmDeploy">部署</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Loading } from '@element-plus/icons-vue'
import { appApi, modelApi, knowledgeApi, workflowApi } from '@/api'
import type { AppTemplate, ModelConfig, KnowledgeBase, Workflow } from '@/types'

const router = useRouter()

// ── AI 应用 ──────────────────────────────────────────────
const templates = ref<AppTemplate[]>([])
const loading = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = 12
const keyword = ref('')
const activeCategory = ref('')

// ── 工作流应用 ────────────────────────────────────────────
const mainTab = ref('apps')
const publishedWorkflows = ref<Workflow[]>([])
const wfLoading = ref(false)

const filteredWorkflows = computed(() => {
  if (!keyword.value) return publishedWorkflows.value
  const kw = keyword.value.toLowerCase()
  return publishedWorkflows.value.filter(w =>
    w.name.toLowerCase().includes(kw) || (w.description || '').toLowerCase().includes(kw)
  )
})

function getWfInputFields(wf: Workflow): any[] {
  const nodes: any[] = wf.definition?.nodes || []
  const startNode = nodes.find((n: any) => n.type === 'start')
  return startNode?.config?.input_fields || []
}

function runWorkflow(id: number) {
  router.push(`/workflows/${id}/run`)
}

// ── 共用资源 ──────────────────────────────────────────────
const chatModels = ref<ModelConfig[]>([])
const knowledgeBases = ref<KnowledgeBase[]>([])

const deployDialogVisible = ref(false)
const deployingApp = ref<AppTemplate | null>(null)
const deploying = ref(false)
const deployForm = reactive({
  name: '', description: '', model_config_id: null as number | null,
  knowledge_base_id: null as number | null, workflow_id: null as number | null, is_public: false,
})

const categoryMap: Record<string, string> = {
  legal: '法务合规', finance: '财税金融', hr: '人力资源',
  procurement: '采购供应链', marketing: '市场营销', service: '客户服务',
  office: '政务办公', compliance: '合规管理', tech: '研发技术',
}

function categoryLabel(cat: string) {
  return categoryMap[cat] || cat
}

function onSearch() {
  if (mainTab.value === 'apps') loadData()
  // filteredWorkflows is computed, no extra call needed for workflows
}

function onMainTabChange(tab: string | number) {
  if (tab === 'workflows' && publishedWorkflows.value.length === 0) {
    loadWorkflows()
  }
}

async function loadData() {
  loading.value = true
  try {
    const res = await appApi.listTemplates({
      page: page.value, page_size: pageSize,
      category: activeCategory.value || undefined,
      keyword: keyword.value || undefined,
    })
    templates.value = res.items
    total.value = res.total
  } finally {
    loading.value = false
  }
}

async function loadWorkflows() {
  wfLoading.value = true
  try {
    const res = await workflowApi.list({ page_size: 100, is_published: true })
    publishedWorkflows.value = res.items.filter((w: Workflow) => w.is_published)
  } finally {
    wfLoading.value = false
  }
}

function deployApp(app: AppTemplate) {
  deployingApp.value = app
  Object.assign(deployForm, {
    name: app.display_name, description: app.description || '',
    model_config_id: null, knowledge_base_id: null, workflow_id: null, is_public: false,
  })
  deployDialogVisible.value = true
}

async function confirmDeploy() {
  if (!deployingApp.value) return
  if (!deployForm.name.trim()) return ElMessage.warning('请输入应用名称')
  deploying.value = true
  try {
    await appApi.createInstance({
      template_id: deployingApp.value.id,
      ...deployForm,
    })
    ElMessage.success('部署成功！')
    deployDialogVisible.value = false
    router.push('/apps/instances')
  } finally {
    deploying.value = false
  }
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
  publishedWorkflows.value = wfs.items.filter((w: Workflow) => w.is_published)
})
</script>

<style scoped>
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
.page-header h2 { font-size: 20px; font-weight: 700; color: #0d0d0d; }
.main-tabs { margin-bottom: 4px; }
.app-card {
  border-radius: 12px; transition: all 0.18s;
  border: 1px solid #e5e5e5 !important;
  box-shadow: 0 1px 3px rgba(0,0,0,0.04) !important;
}
.app-card:hover {
  border-color: #10a37f !important;
  box-shadow: 0 4px 16px rgba(16,163,127,0.1) !important;
  transform: translateY(-2px);
}
.wf-card { cursor: pointer; }
.wf-badge { margin-bottom: 6px; }
.app-icon { font-size: 34px; margin-bottom: 4px; }
.app-name { font-size: 15px; font-weight: 600; color: #0d0d0d; margin-bottom: 6px; }
.app-desc { font-size: 13px; color: #6b7280; line-height: 1.5; margin-bottom: 10px; min-height: 40px; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
.app-meta { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
.app-usage { font-size: 12px; color: #9ca3af; }
.wf-input-preview { margin-top: 6px; margin-bottom: 2px; font-size: 12px; }
.wf-input-label { color: #9ca3af; margin-right: 4px; }
.wf-market-hint {
  background: #f0fdf9;
  border: 1px solid #d1fae5; border-radius: 10px;
  padding: 10px 16px; margin-bottom: 16px;
  font-size: 13px; color: #374151;
  display: flex; align-items: center; gap: 8px;
}
</style>
