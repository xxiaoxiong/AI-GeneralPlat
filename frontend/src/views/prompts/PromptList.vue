<template>
  <div class="prompt-page">
    <div class="page-header">
      <div>
        <h2>提示词模板库</h2>
        <p class="page-desc">管理可复用的提示词模板，支持版本历史和变量替换</p>
      </div>
      <div class="header-actions">
        <el-button type="primary" @click="openCreate">＋ 新建模板</el-button>
        <el-button @click="$router.push('/prompts/debugger')">🔬 调试器</el-button>
      </div>
    </div>

    <!-- Filter bar -->
    <div class="filter-bar">
      <el-input v-model="keyword" placeholder="搜索模板名称或内容..." clearable style="width:260px" @input="loadData" />
      <el-select v-model="category" clearable placeholder="分类" style="width:140px" @change="loadData">
        <el-option v-for="c in categories" :key="c.value" :label="c.label" :value="c.value" />
      </el-select>
      <el-checkbox v-model="myOnly" @change="loadData">仅我的</el-checkbox>
    </div>

    <!-- Template cards -->
    <div v-loading="loading" class="template-grid">
      <el-card
        v-for="tpl in templates"
        :key="tpl.id"
        class="tpl-card"
        shadow="hover"
      >
        <div class="tpl-header">
          <div class="tpl-name">{{ tpl.name }}</div>
          <div class="tpl-badges">
            <el-tag size="small" type="info">{{ getCategoryLabel(tpl.category) }}</el-tag>
            <el-tag v-if="tpl.is_public" size="small" type="success">公开</el-tag>
            <el-tag size="small">v{{ tpl.version }}</el-tag>
          </div>
        </div>
        <div class="tpl-desc">{{ tpl.description || '暂无描述' }}</div>
        <div class="tpl-preview">{{ tpl.content.slice(0, 120) }}{{ tpl.content.length > 120 ? '...' : '' }}</div>
        <div v-if="tpl.variables?.length" class="tpl-vars">
          <el-tag v-for="v in tpl.variables" :key="v" size="small" effect="plain" style="margin:2px">
            &#123;&#123;{{ v }}&#125;&#125;
          </el-tag>
        </div>
        <div class="tpl-footer">
          <span class="tpl-usage">使用 {{ tpl.usage_count }} 次</span>
          <div class="tpl-actions">
            <el-button size="small" @click="debugTemplate(tpl)">🔬 调试</el-button>
            <el-button size="small" @click="viewVersions(tpl)">📋 历史</el-button>
            <el-button style="color:#fff;" size="small" type="primary" plain @click="editTemplate(tpl)">编辑</el-button>
            <el-button size="small" type="danger" plain @click="deleteTemplate(tpl)">删除</el-button>
          </div>
        </div>
      </el-card>
      <div v-if="!loading && templates.length === 0" class="empty-hint">
        暂无模板，点击「新建模板」创建第一个
      </div>
    </div>

    <!-- Pagination -->
    <el-pagination
      v-if="total > pageSize"
      :current-page="page"
      :page-size="pageSize"
      :total="total"
      layout="prev, pager, next"
      style="margin-top:16px;justify-content:center;display:flex"
      @current-change="onPageChange"
    />

    <!-- Create/Edit Dialog -->
    <el-dialog v-model="editDialogVisible" :title="editingId ? '编辑模板' : '新建模板'" width="700px" top="5vh">
      <el-form :model="form" label-width="90px">
        <el-form-item label="模板名称" required>
          <el-input v-model="form.name" placeholder="简洁描述用途" />
        </el-form-item>
        <el-form-item label="分类">
          <el-select v-model="form.category" style="width:100%">
            <el-option v-for="c in categories" :key="c.value" :label="c.label" :value="c.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="提示词内容" required>
          <el-input
            v-model="form.content"
            type="textarea"
            :rows="10"
            placeholder="使用 {{变量名}} 定义变量，如：请分析以下内容：{{content}}"
            @input="detectVars"
          />
        </el-form-item>
        <el-form-item label="检测到变量">
          <div v-if="detectedVars.length">
            <el-tag v-for="v in detectedVars" :key="v" style="margin:2px">&#123;&#123;{{ v }}&#125;&#125;</el-tag>
          </div>
          <span v-else style="color:#aaa;font-size:12px">暂未检测到变量</span>
        </el-form-item>
        <el-form-item label="标签">
          <el-input v-model="form.tags" placeholder="逗号分隔，如: 分析,报告,总结" />
        </el-form-item>
        <el-form-item label="公开">
          <el-switch v-model="form.is_public" />
          <span style="font-size:12px;color:#888;margin-left:8px">公开后其他用户可查看和使用</span>
        </el-form-item>
        <el-form-item v-if="editingId" label="修改说明">
          <el-input v-model="form.change_note" placeholder="简述本次修改内容（可选）" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveTemplate">保存</el-button>
      </template>
    </el-dialog>

    <!-- Version History Dialog -->
    <el-dialog v-model="versionDialogVisible" title="版本历史" width="600px">
      <div v-loading="versionsLoading">
        <div v-for="ver in versions" :key="ver.id" class="version-item">
          <div class="version-header">
            <el-tag size="small">v{{ ver.version }}</el-tag>
            <span class="version-note">{{ ver.change_note || '无说明' }}</span>
            <span class="version-time">{{ new Date(ver.created_at).toLocaleString() }}</span>
          </div>
          <div class="version-content">{{ ver.content.slice(0, 200) }}{{ ver.content.length > 200 ? '...' : '' }}</div>
          <el-button size="small" @click="restoreVersion(ver)">恢复此版本</el-button>
        </div>
        <div v-if="!versionsLoading && versions.length === 0" style="color:#aaa;text-align:center;padding:20px">暂无历史版本</div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { promptApi } from '@/api'

const router = useRouter()

const templates = ref<any[]>([])
const loading = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = 20
const keyword = ref('')
const category = ref('')
const myOnly = ref(false)

const categories = [
  { value: 'general', label: '通用' },
  { value: 'analysis', label: '分析' },
  { value: 'writing', label: '写作' },
  { value: 'coding', label: '编程' },
  { value: 'summary', label: '总结' },
  { value: 'translation', label: '翻译' },
  { value: 'qa', label: '问答' },
  { value: 'other', label: '其他' },
]

function getCategoryLabel(val: string) {
  return categories.find(c => c.value === val)?.label || val
}

async function loadData() {
  loading.value = true
  try {
    const res = await promptApi.list({
      page: page.value,
      page_size: pageSize,
      keyword: keyword.value || undefined,
      category: category.value || undefined,
      include_public: !myOnly.value,
    })
    templates.value = res.items
    total.value = res.total
  } finally {
    loading.value = false
  }
}

// Create/Edit
const editDialogVisible = ref(false)
const editingId = ref<number | null>(null)
const saving = ref(false)
const detectedVars = ref<string[]>([])
const form = reactive({
  name: '',
  description: '',
  category: 'general',
  content: '',
  is_public: false,
  tags: '',
  change_note: '',
})

function detectVars() {
  const matches = form.content.match(/\{\{(\w+)\}\}/g) || []
  detectedVars.value = [...new Set(matches.map(m => m.slice(2, -2)))]
}

function openCreate() {
  editingId.value = null
  Object.assign(form, { name: '', description: '', category: 'general', content: '', is_public: false, tags: '', change_note: '' })
  detectedVars.value = []
  editDialogVisible.value = true
}

function editTemplate(tpl: any) {
  editingId.value = tpl.id
  Object.assign(form, {
    name: tpl.name, description: tpl.description || '',
    category: tpl.category, content: tpl.content,
    is_public: tpl.is_public, tags: tpl.tags || '', change_note: '',
  })
  detectVars()
  editDialogVisible.value = true
}

async function saveTemplate() {
  if (!form.name.trim()) return ElMessage.warning('请输入模板名称')
  if (!form.content.trim()) return ElMessage.warning('请输入提示词内容')
  saving.value = true
  try {
    if (editingId.value) {
      await promptApi.update(editingId.value, form)
      ElMessage.success('更新成功')
    } else {
      await promptApi.create(form)
      ElMessage.success('创建成功')
    }
    editDialogVisible.value = false
    await loadData()
  } finally {
    saving.value = false
  }
}

async function deleteTemplate(tpl: any) {
  await ElMessageBox.confirm(`确认删除模板「${tpl.name}」？`, '警告', { type: 'warning' })
  await promptApi.delete(tpl.id)
  ElMessage.success('删除成功')
  await loadData()
}

// Versions
const versionDialogVisible = ref(false)
const versionsLoading = ref(false)
const versions = ref<any[]>([])
const currentTplForVersion = ref<any>(null)

async function viewVersions(tpl: any) {
  currentTplForVersion.value = tpl
  versionDialogVisible.value = true
  versionsLoading.value = true
  try {
    versions.value = await promptApi.listVersions(tpl.id)
  } finally {
    versionsLoading.value = false
  }
}

async function restoreVersion(ver: any) {
  if (!currentTplForVersion.value) return
  await promptApi.update(currentTplForVersion.value.id, {
    content: ver.content,
    change_note: `恢复至版本 v${ver.version}`,
  })
  ElMessage.success('已恢复')
  versionDialogVisible.value = false
  await loadData()
}

function onPageChange(p: number) {
  page.value = p
  loadData()
}

function debugTemplate(tpl: any) {
  router.push({ path: '/prompts/debugger', query: { template_id: tpl.id } })
}

onMounted(loadData)
</script>

<style scoped>
.prompt-page { padding: 24px; background: #fff; min-height: calc(100vh - 56px); }
.page-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 20px; }
.page-header h2 { font-size: 20px; font-weight: 700; color: #0d0d0d; margin: 0 0 4px; }
.page-desc { font-size: 13px; color: #9ca3af; margin: 0; }
.header-actions { display: flex; gap: 8px; }
.filter-bar { display: flex; gap: 12px; align-items: center; margin-bottom: 16px; flex-wrap: wrap; }
.template-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; }
.tpl-card { cursor: default; border: 1px solid #e5e5e5 !important; border-radius: 12px !important; box-shadow: 0 1px 3px rgba(0,0,0,0.04) !important; transition: all 0.18s !important; }
.tpl-card:hover { border-color: #10a37f !important; box-shadow: 0 4px 12px rgba(16,163,127,0.1) !important; }
.tpl-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 6px; }
.tpl-name { font-size: 15px; font-weight: 600; color: #0d0d0d; }
.tpl-badges { display: flex; gap: 4px; flex-wrap: wrap; }
.tpl-desc { font-size: 12px; color: #9ca3af; margin-bottom: 8px; min-height: 18px; }
.tpl-preview {
  font-size: 12px; color: #374151; background: #f9f9f9; border-radius: 6px;
  padding: 8px; margin-bottom: 8px; line-height: 1.6;
  white-space: pre-wrap; max-height: 80px; overflow: hidden;
  border: 1px solid #e5e5e5;
}
.tpl-vars { margin-bottom: 8px; }
.tpl-footer { display: flex; justify-content: space-between; align-items: center; margin-top: 8px; }
.tpl-usage { font-size: 12px; color: #d1d5db; }
.tpl-actions { display: flex; gap: 4px; }
.empty-hint { grid-column: 1/-1; text-align: center; color: #9ca3af; padding: 60px 0; font-size: 14px; }
.version-item { border: 1px solid #e5e5e5; border-radius: 8px; padding: 12px; margin-bottom: 10px; }
.version-header { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.version-note { font-size: 13px; color: #0d0d0d; flex: 1; }
.version-time { font-size: 12px; color: #9ca3af; }
.version-content { font-size: 12px; color: #6b7280; background: #f9f9f9; padding: 8px; border-radius: 4px; margin-bottom: 8px; white-space: pre-wrap; }
</style>
