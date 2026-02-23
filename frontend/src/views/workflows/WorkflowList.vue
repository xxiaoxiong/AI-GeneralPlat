<template>
  <div class="page-container">
    <div class="page-header">
      <h2>流程编排</h2>
      <el-button type="primary" :icon="Plus" @click="createWorkflow">新建流程</el-button>
    </div>

    <el-card shadow="never">
      <div class="toolbar">
        <el-input v-model="keyword" placeholder="搜索流程名称..." clearable style="width:240px;" @input="loadData" />
        <el-select v-model="filterCategory" placeholder="全部分类" clearable style="width:140px;" @change="loadData">
          <el-option label="合同法务" value="legal" />
          <el-option label="财税金融" value="finance" />
          <el-option label="人力资源" value="hr" />
          <el-option label="采购供应链" value="procurement" />
          <el-option label="市场营销" value="marketing" />
          <el-option label="其他" value="other" />
        </el-select>
      </div>

      <el-table :data="workflows" v-loading="loading" stripe style="margin-top:12px;">
        <el-table-column label="流程名称" min-width="180">
          <template #default="{ row }">
            <div style="display:flex;align-items:center;gap:8px;">
              <span>{{ row.icon || '⚙️' }}</span>
              <span style="font-weight:500;">{{ row.name }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="description" label="描述" min-width="200" show-overflow-tooltip />
        <el-table-column label="分类" width="100">
          <template #default="{ row }">
            <el-tag size="small" v-if="row.category">{{ row.category }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="触发方式" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="row.trigger_type === 'manual' ? 'info' : 'warning'">
              {{ row.trigger_type === 'manual' ? '手动' : row.trigger_type }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="版本" width="70">
          <template #default="{ row }">v{{ row.version }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.is_published ? 'success' : 'info'" size="small">
              {{ row.is_published ? '已发布' : '草稿' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="150">
          <template #default="{ row }">{{ formatDate(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="240" fixed="right">
          <template #default="{ row }">
            <el-button style="color: #fff;" size="small" type="primary" plain @click="openEditor(row.id)">编辑</el-button>
            <el-button size="small" type="success" plain @click="openRun(row.id)">▶ 运行</el-button>
            <el-button size="small" type="danger" @click="deleteWorkflow(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div style="margin-top:16px;text-align:right;" v-if="total > pageSize">
        <el-pagination
          v-model:current-page="page"
          :page-size="pageSize"
          :total="total"
          layout="prev, pager, next"
          @current-change="loadData"
        />
      </div>
    </el-card>

  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { workflowApi } from '@/api'
import type { Workflow } from '@/types'
import dayjs from 'dayjs'

const router = useRouter()
const workflows = ref<Workflow[]>([])
const loading = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = 20
const keyword = ref('')
const filterCategory = ref('')


function formatDate(d: string) {
  return dayjs(d).format('YYYY-MM-DD HH:mm')
}

async function loadData() {
  loading.value = true
  try {
    const res = await workflowApi.list({
      page: page.value, page_size: pageSize,
      keyword: keyword.value || undefined,
      category: filterCategory.value || undefined,
    })
    workflows.value = res.items
    total.value = res.total
  } finally {
    loading.value = false
  }
}

async function createWorkflow() {
  const wf = await workflowApi.create({
    name: '新建流程',
    definition: { nodes: [
      { id: 'start', type: 'start', config: {}, position: { x: 100, y: 200 } },
      { id: 'end', type: 'end', config: {}, position: { x: 600, y: 200 } },
    ], edges: [] },
  })
  router.push(`/workflows/${wf.id}/editor`)
}

function openEditor(id: number) {
  router.push(`/workflows/${id}/editor`)
}

function openRun(id: number) {
  router.push(`/workflows/${id}/run`)
}

async function deleteWorkflow(wf: Workflow) {
  await ElMessageBox.confirm(`确认删除流程「${wf.name}」？`, '警告', { type: 'warning' })
  await workflowApi.delete(wf.id)
  ElMessage.success('删除成功')
  await loadData()
}

onMounted(loadData)
</script>

<style scoped>
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
.page-header h2 { font-size: 20px; font-weight: 700; color: #0d0d0d; }
.toolbar { display: flex; gap: 12px; }
</style>
