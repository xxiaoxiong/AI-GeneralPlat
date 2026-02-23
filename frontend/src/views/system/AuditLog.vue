<template>
  <div class="audit-page">
    <div class="page-header">
      <h2>审计日志</h2>
    </div>

    <div class="toolbar">
        <el-input v-model="filters.username" placeholder="用户名" clearable style="width:140px;" />
        <el-select v-model="filters.module" placeholder="模块" clearable style="width:120px;">
          <el-option label="认证" value="auth" />
          <el-option label="用户" value="user" />
          <el-option label="模型" value="model" />
          <el-option label="知识库" value="knowledge" />
          <el-option label="流程" value="workflow" />
          <el-option label="应用" value="app" />
        </el-select>
        <el-select v-model="filters.status" placeholder="状态" clearable style="width:100px;">
          <el-option label="成功" value="success" />
          <el-option label="失败" value="failed" />
        </el-select>
        <el-date-picker
          v-model="dateRange"
          type="datetimerange"
          range-separator="至"
          start-placeholder="开始时间"
          end-placeholder="结束时间"
          style="width:360px;"
        />
        <el-button type="primary" @click="loadData">查询</el-button>
        <el-button @click="resetFilters">重置</el-button>
      </div>

    <el-card shadow="never" class="audit-card">
      <el-table :data="logs" v-loading="loading" stripe size="small">
        <el-table-column prop="username" label="用户" width="140" />
        <el-table-column prop="action" label="操作"  />
        <el-table-column prop="module" label="模块" width="80">
          <template #default="{ row }">
            <el-tag size="small">{{ row.module }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="resource_type" label="资源类型" width="120" />
        <el-table-column prop="resource_id" label="资源ID" width="100" />
        <el-table-column prop="ip_address" label="IP" width="120" />
        <el-table-column label="状态" width="70">
          <template #default="{ row }">
            <el-tag :type="row.status === 'success' ? 'success' : 'danger'" size="small">
              {{ row.status === 'success' ? '成功' : '失败' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="耗时" width="120">
          <template #default="{ row }">
            {{ row.duration_ms ? row.duration_ms + 'ms' : '-' }}
          </template>
        </el-table-column>
        <el-table-column label="时间" width="255">
          <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="详情" width="100">
          <template #default="{ row }">
            <el-button size="small" link @click="showDetail(row)">查看</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div style="margin-top:16px;text-align:right;" v-if="total > pageSize">
        <el-pagination
          v-model:current-page="page"
          :page-size="pageSize"
          :total="total"
          layout="total, prev, pager, next"
          @current-change="loadData"
        />
      </div>
    </el-card>

    <!-- Detail Dialog -->
    <el-dialog v-model="detailVisible" title="日志详情" width="500px">
      <el-descriptions :column="1" border size="small" v-if="detailLog">
        <el-descriptions-item label="用户">{{ detailLog.username }}</el-descriptions-item>
        <el-descriptions-item label="操作">{{ detailLog.action }}</el-descriptions-item>
        <el-descriptions-item label="模块">{{ detailLog.module }}</el-descriptions-item>
        <el-descriptions-item label="IP">{{ detailLog.ip_address }}</el-descriptions-item>
        <el-descriptions-item label="状态">{{ detailLog.status }}</el-descriptions-item>
        <el-descriptions-item label="错误信息" v-if="detailLog.error_msg">{{ detailLog.error_msg }}</el-descriptions-item>
        <el-descriptions-item label="时间">{{ formatDate(detailLog.created_at) }}</el-descriptions-item>
        <el-descriptions-item label="详细数据" v-if="detailLog.detail">
          <pre style="font-size:12px;max-height:200px;overflow:auto;">{{ JSON.stringify(detailLog.detail, null, 2) }}</pre>
        </el-descriptions-item>
      </el-descriptions>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { auditApi } from '@/api'
import type { AuditLog } from '@/types'
import dayjs from 'dayjs'

const logs = ref<AuditLog[]>([])
const loading = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = 50
const dateRange = ref<[Date, Date] | null>(null)

const filters = reactive({
  username: '',
  module: '',
  status: '',
})

const detailVisible = ref(false)
const detailLog = ref<AuditLog | null>(null)

function formatDate(d: string) { return dayjs(d).format('YYYY-MM-DD HH:mm:ss') }

function showDetail(log: AuditLog) {
  detailLog.value = log
  detailVisible.value = true
}

function resetFilters() {
  filters.username = ''
  filters.module = ''
  filters.status = ''
  dateRange.value = null
  page.value = 1
  loadData()
}

async function loadData() {
  loading.value = true
  try {
    const params: any = {
      page: page.value,
      page_size: pageSize,
      username: filters.username || undefined,
      module: filters.module || undefined,
      status: filters.status || undefined,
    }
    if (dateRange.value) {
      params.start_time = dateRange.value[0].toISOString()
      params.end_time = dateRange.value[1].toISOString()
    }
    const res = await auditApi.list(params)
    logs.value = res.items
    total.value = res.total
  } finally {
    loading.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.audit-page {
  padding: 24px;
  background: #fff;
  height: calc(100vh - 56px);
  display: flex;
  flex-direction: column;
  box-sizing: border-box;
  overflow: hidden;
}
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; flex-shrink: 0; }
.page-header h2 { font-size: 20px; font-weight: 700; color: #0d0d0d; }
.toolbar { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; flex-shrink: 0; }
.audit-card {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.audit-card :deep(.el-card__body) {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 16px 20px !important;
  overflow: hidden;
}
.audit-card :deep(.el-table) {
  flex: 1;
  min-height: 0;
}
.audit-card :deep(.el-table .el-table__body-wrapper) {
  overflow-y: auto;
}
</style>
