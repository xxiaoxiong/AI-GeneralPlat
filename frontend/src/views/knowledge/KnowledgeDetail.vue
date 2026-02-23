<template>
  <div class="page-container">
    <div class="page-header">
      <div style="display:flex;align-items:center;gap:12px;">
        <el-button :icon="ArrowLeft" @click="router.back()">返回</el-button>
        <h2>{{ kb?.name || '知识库详情' }}</h2>
        <el-tag v-if="kb" type="info">{{ kb.doc_count }} 文档 · {{ kb.vector_count }} 向量</el-tag>
      </div>
      <div style="display:flex;gap:8px;">
        <el-button :icon="Link" @click="crawlDialogVisible = true">网页爬取</el-button>
        <el-upload
          :show-file-list="false"
          :before-upload="handleUpload"
          multiple
          accept=".pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt,.md,.csv,.mp3,.wav,.m4a"
        >
          <el-button type="primary" :icon="Upload">上传文档</el-button>
        </el-upload>
      </div>
    </div>

    <el-card shadow="never">
      <el-table :data="docs" v-loading="loading" stripe>
        <el-table-column label="文件名" min-width="200">
          <template #default="{ row }">
            <div style="display:flex;align-items:center;gap:8px;">
              <span>{{ fileIcon(row.file_type) }}</span>
              <span>{{ row.filename }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="80">
          <template #default="{ row }">
            <el-tag size="small">{{ row.file_type.toUpperCase() }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="大小" width="90">
          <template #default="{ row }">{{ formatSize(row.file_size) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">
              {{ statusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="chunk_count" label="分块数" width="80" />
        <el-table-column label="上传时间" width="160">
          <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="80" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="danger" @click="deleteDoc(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div style="margin-top:16px;text-align:right;" v-if="total > pageSize">
        <el-pagination
          v-model:current-page="page"
          :page-size="pageSize"
          :total="total"
          layout="prev, pager, next"
          @current-change="() => loadDocs()"
        />
      </div>
    </el-card>
    <!-- 网页爬取对话框 -->
    <el-dialog v-model="crawlDialogVisible" title="网页爬取" width="480px">
      <el-form label-width="80px">
        <el-form-item label="网页 URL">
          <el-input v-model="crawlUrl" placeholder="https://example.com/article" clearable />
        </el-form-item>
        <el-form-item label="标题(可选)">
          <el-input v-model="crawlTitle" placeholder="自定义文档标题" clearable />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="crawlDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="crawling" @click="doCrawl">开始爬取</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft, Upload, Link } from '@element-plus/icons-vue'
import { knowledgeApi } from '@/api'
import type { KnowledgeBase, KnowledgeDocument } from '@/types'
import dayjs from 'dayjs'

const route = useRoute()
const router = useRouter()
const kbId = Number(route.params.id)

const kb = ref<KnowledgeBase | null>(null)
const docs = ref<KnowledgeDocument[]>([])
const loading = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = 20
let pollTimer: ReturnType<typeof setInterval> | null = null

const crawlDialogVisible = ref(false)
const crawlUrl = ref('')
const crawlTitle = ref('')
const crawling = ref(false)

function fileIcon(type: string) {
  const map: Record<string, string> = { pdf: '📕', doc: '📘', docx: '📘', xls: '📗', xlsx: '📗', ppt: '📙', pptx: '📙', txt: '📄', md: '📝', csv: '📊' }
  return map[type] || '📄'
}

function formatSize(bytes: number) {
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / 1024 / 1024).toFixed(1) + ' MB'
}

function formatDate(d: string) {
  return dayjs(d).format('YYYY-MM-DD HH:mm')
}

function statusType(s: string) {
  const map: Record<string, any> = { pending: 'info', processing: 'warning', done: 'success', failed: 'danger' }
  return map[s] || 'info'
}

function statusLabel(s: string) {
  const map: Record<string, string> = { pending: '待处理', processing: '处理中', done: '已完成', failed: '失败' }
  return map[s] || s
}

async function loadDocs(silent = false) {
  if (!silent) loading.value = true
  try {
    const res = await knowledgeApi.listDocuments(kbId, { page: page.value, page_size: pageSize })
    docs.value = res.items
    total.value = res.total
    // Refresh KB info to update vector_count
    kb.value = await knowledgeApi.getBase(kbId)
    // Stop polling when no pending/processing docs
    const hasPending = res.items.some((d: KnowledgeDocument) => d.status === 'pending' || d.status === 'processing')
    if (!hasPending && pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  } finally {
    if (!silent) loading.value = false
  }
}

function startPolling() {
  if (pollTimer) return
  pollTimer = setInterval(() => loadDocs(true), 2000)
}

async function handleUpload(file: File) {
  try {
    ElMessage.info(`正在上传 ${file.name}...`)
    await knowledgeApi.uploadDocument(kbId, file)
    ElMessage.success(`${file.name} 上传成功，正在后台处理`)
    await loadDocs()
    startPolling()
  } catch {
    ElMessage.error(`${file.name} 上传失败`)
  }
  return false
}

async function deleteDoc(doc: KnowledgeDocument) {
  await ElMessageBox.confirm(`确认删除文档「${doc.filename}」？`, '警告', { type: 'warning' })
  await knowledgeApi.deleteDocument(kbId, doc.id)
  ElMessage.success('删除成功')
  await loadDocs()
  kb.value = await knowledgeApi.getBase(kbId)
}

async function doCrawl() {
  if (!crawlUrl.value.trim()) {
    ElMessage.warning('请输入网页 URL')
    return
  }
  crawling.value = true
  try {
    await knowledgeApi.crawlWebpage(kbId, { url: crawlUrl.value.trim(), title: crawlTitle.value.trim() || undefined })
    ElMessage.success('网页爬取成功，正在后台处理')
    crawlDialogVisible.value = false
    crawlUrl.value = ''
    crawlTitle.value = ''
    await loadDocs()
    startPolling()
  } catch (e: any) {
    ElMessage.error('爬取失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    crawling.value = false
  }
}

onMounted(async () => {
  kb.value = await knowledgeApi.getBase(kbId)
  await loadDocs()
  // Auto-poll if there are pending docs on load
  const hasPending = docs.value.some((d: KnowledgeDocument) => d.status === 'pending' || d.status === 'processing')
  if (hasPending) startPolling()
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<style scoped>
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
.page-header h2 { font-size: 20px; font-weight: 700; color: #0d0d0d; }
</style>
