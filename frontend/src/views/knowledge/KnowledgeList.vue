<template>
  <div class="page-container">
    <div class="page-header">
      <h2>知识库管理</h2>
      <el-button type="primary" :icon="Plus" @click="openDialog()">新建知识库</el-button>
    </div>

    <el-row :gutter="16">
      <el-col :span="6" v-for="kb in kbList" :key="kb.id">
        <el-card class="kb-card" shadow="never" @click="goDetail(kb.id)">
          <div class="kb-icon">📚</div>
          <div class="kb-name">{{ kb.name }}</div>
          <div class="kb-desc text-ellipsis">{{ kb.description || '暂无描述' }}</div>
          <div class="kb-stats">
            <span><el-icon><Document /></el-icon> {{ kb.doc_count }} 文档</span>
            <span><el-icon><DataLine /></el-icon> {{ kb.vector_count }} 向量</span>
          </div>
          <div class="kb-actions" @click.stop>
            <el-button size="small" type="primary" plain @click="goDetail(kb.id)">管理文档</el-button>
            <el-button size="small" @click="openDialog(kb)">编辑</el-button>
            <el-button size="small" type="danger" @click="deleteKb(kb)">删除</el-button>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <div class="kb-add" @click="openDialog()">
          <el-icon size="36" color="#c0c4cc"><Plus /></el-icon>
          <span>新建知识库</span>
        </div>
      </el-col>
    </el-row>

    <div v-if="total > pageSize" style="margin-top:16px;text-align:right;">
      <el-pagination
        v-model:current-page="page"
        :page-size="pageSize"
        :total="total"
        layout="prev, pager, next"
        @current-change="loadData"
      />
    </div>

    <!-- Knowledge Search -->
    <el-card shadow="never" style="margin-top:16px;">
      <template #header><span class="card-title">知识库检索测试</span></template>
      <el-row :gutter="12">
        <el-col :span="16">
          <el-input v-model="searchQuery" placeholder="输入检索问题..." clearable />
        </el-col>
        <el-col :span="4">
          <el-select v-model="searchKbIds" multiple placeholder="选择知识库" style="width:100%">
            <el-option v-for="kb in kbList" :key="kb.id" :label="kb.name" :value="kb.id" />
          </el-select>
        </el-col>
        <el-col :span="4">
          <el-button type="primary" @click="doSearch" :loading="searching" style="width:100%">检索</el-button>
        </el-col>
      </el-row>
      <div v-if="searchResults.length > 0" style="margin-top:16px;">
        <div v-for="(r, i) in searchResults" :key="i" class="search-result">
          <div class="result-header">
            <span class="result-file">📄 {{ r.filename }}</span>
            <el-tag size="small" type="success">相关度 {{ (r.score * 100).toFixed(1) }}%</el-tag>
          </div>
          <div class="result-content">{{ r.content }}</div>
        </div>
      </div>
    </el-card>

    <!-- Dialog -->
    <el-dialog v-model="dialogVisible" :title="editing ? '编辑知识库' : '新建知识库'" width="480px">
      <el-form :model="form" label-width="90px">
        <el-form-item label="名称"><el-input v-model="form.name" /></el-form-item>
        <el-form-item label="描述"><el-input v-model="form.description" type="textarea" :rows="2" /></el-form-item>
        <el-form-item label="分块大小"><el-input-number v-model="form.chunk_size" :min="100" :max="2000" /></el-form-item>
        <el-form-item label="分块重叠"><el-input-number v-model="form.chunk_overlap" :min="0" :max="500" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { knowledgeApi } from '@/api'
import type { KnowledgeBase } from '@/types'

const router = useRouter()
const kbList = ref<KnowledgeBase[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 12
const dialogVisible = ref(false)
const editing = ref<KnowledgeBase | null>(null)
const saving = ref(false)
const form = reactive({ name: '', description: '', chunk_size: 500, chunk_overlap: 50 })

const searchQuery = ref('')
const searchKbIds = ref<number[]>([])
const searchResults = ref<any[]>([])
const searching = ref(false)

async function loadData() {
  const res = await knowledgeApi.listBases({ page: page.value, page_size: pageSize })
  kbList.value = res.items
  total.value = res.total
}

function goDetail(id: number) {
  router.push(`/knowledge/${id}`)
}

function openDialog(kb?: KnowledgeBase) {
  editing.value = kb || null
  Object.assign(form, kb ? { name: kb.name, description: kb.description || '', chunk_size: kb.chunk_size, chunk_overlap: kb.chunk_overlap }
    : { name: '', description: '', chunk_size: 500, chunk_overlap: 50 })
  dialogVisible.value = true
}

async function save() {
  if (!form.name.trim()) return ElMessage.warning('请输入名称')
  saving.value = true
  try {
    if (editing.value) {
      await knowledgeApi.updateBase(editing.value.id, form)
      ElMessage.success('更新成功')
    } else {
      await knowledgeApi.createBase(form)
      ElMessage.success('创建成功')
    }
    dialogVisible.value = false
    await loadData()
  } finally {
    saving.value = false
  }
}

async function deleteKb(kb: KnowledgeBase) {
  await ElMessageBox.confirm(`确认删除知识库「${kb.name}」？此操作不可恢复`, '警告', { type: 'warning' })
  await knowledgeApi.deleteBase(kb.id)
  ElMessage.success('删除成功')
  await loadData()
}

async function doSearch() {
  if (!searchQuery.value.trim()) return ElMessage.warning('请输入检索内容')
  if (searchKbIds.value.length === 0) return ElMessage.warning('请选择知识库')
  searching.value = true
  try {
    const res = await knowledgeApi.search({
      query: searchQuery.value,
      knowledge_base_ids: searchKbIds.value,
      top_k: 5,
      score_threshold: 0.3,
    })
    searchResults.value = res.results
    if (res.results.length === 0) ElMessage.info('未找到相关内容')
  } finally {
    searching.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
.page-header h2 { font-size: 20px; font-weight: 700; color: #0d0d0d; }
.kb-card {
  cursor: pointer; transition: all 0.18s;
  border-radius: 12px; border: 1px solid #e5e5e5 !important;
  box-shadow: 0 1px 3px rgba(0,0,0,0.04) !important;
}
.kb-card:hover {
  border-color: #10a37f !important;
  box-shadow: 0 4px 16px rgba(16,163,127,0.1) !important;
  transform: translateY(-2px);
}
.kb-icon { font-size: 30px; margin-bottom: 8px; }
.kb-name { font-size: 15px; font-weight: 600; color: #0d0d0d; margin-bottom: 4px; }
.kb-desc { font-size: 13px; color: #6b7280; margin-bottom: 12px; }
.kb-stats { display: flex; gap: 12px; font-size: 12px; color: #9ca3af; margin-bottom: 12px; }
.kb-stats span { display: flex; align-items: center; gap: 4px; }
.kb-actions { display: flex; gap: 6px; flex-wrap: wrap; }
.kb-actions :deep(.el-button--primary.is-plain) { color: #10a37f !important; background: #f0fdf9 !important; border-color: #10a37f !important; }
.kb-actions :deep(.el-button--primary.is-plain:hover) { color: #fff !important; background: #10a37f !important; }
.kb-actions :deep(.el-button--danger) { color: #fff !important; }
.kb-actions :deep(.el-button--default) { color: #374151 !important; }
.kb-add {
  border: 2px dashed #e5e5e5; border-radius: 12px; min-height: 180px;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  cursor: pointer; color: #d1d5db; gap: 10px; transition: all 0.18s;
}
.kb-add:hover { border-color: #10a37f; color: #10a37f; background: #f0fdf9; }
.card-title { font-weight: 600; color: #0d0d0d; }
.search-result { border: 1px solid #e5e5e5; border-radius: 8px; padding: 12px; margin-bottom: 10px; }
.result-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.result-file { font-size: 13px; font-weight: 500; color: #374151; }
.result-content { font-size: 13px; color: #6b7280; line-height: 1.6; }
</style>
