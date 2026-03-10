<template>
  <div class="page-container">
    <div class="page-header">
      <div class="header-left">
        <el-button :icon="ArrowLeft" @click="$router.push('/databases')">返回</el-button>
        <h2 v-if="connInfo">{{ connInfo.name }} — 数据库查询</h2>
      </div>
    </div>

    <el-row :gutter="16">
      <!-- 左侧：表结构 -->
      <el-col :span="6">
        <el-card shadow="never" class="schema-card">
          <template #header><span class="card-title">表结构</span></template>
          <div v-if="loadingSchema" style="text-align:center;padding:20px;"><el-icon class="is-loading"><Loading /></el-icon></div>
          <div v-else>
            <div v-for="t in tables" :key="t" class="table-item" :class="{ active: selectedTable === t }" @click="selectTable(t)">
              <el-icon><Grid /></el-icon> {{ t }}
            </div>
            <div v-if="!tables.length" style="color:#999;font-size:13px;padding:12px;">暂无表</div>
          </div>
          <!-- 列信息 -->
          <div v-if="selectedTable && columns.length" class="columns-panel">
            <div class="columns-title">{{ selectedTable }} 列信息</div>
            <div v-for="col in columns" :key="col.name" class="column-item">
              <span class="col-name">{{ col.name }}</span>
              <el-tag size="small" type="info">{{ col.type }}</el-tag>
              <el-tag v-if="col.key === 'PRI'" size="small" type="warning">PK</el-tag>
            </div>
          </div>
        </el-card>
      </el-col>

      <!-- 右侧：查询区域 -->
      <el-col :span="18">
        <el-card shadow="never" class="query-card">
          <template #header>
            <div style="display:flex;justify-content:space-between;align-items:center;">
              <span class="card-title">SQL 查询</span>
              <div style="display:flex;gap:8px;align-items:center;">
                <span style="font-size:12px;color:#999;">最大行数：</span>
                <el-input-number v-model="queryLimit" :min="1" :max="500" size="small" style="width:100px;" />
                <el-button type="primary" :icon="CaretRight" @click="runQuery" :loading="querying" :disabled="!sqlInput.trim()">执行查询</el-button>
              </div>
            </div>
          </template>
          <el-input v-model="sqlInput" type="textarea" :rows="4" placeholder="输入 SELECT 查询语句..." class="sql-input" @keydown.ctrl.enter="runQuery" />

          <div v-if="queryError" class="query-error">
            <el-alert :title="queryError" type="error" :closable="false" show-icon />
          </div>

          <!-- 结果表格 -->
          <div v-if="resultColumns.length" class="result-section">
            <div class="result-header">
              <span>查询结果（{{ resultRows.length }} 行）</span>
            </div>
            <el-table :data="resultRows" stripe border max-height="460" style="width:100%;" size="small">
              <el-table-column v-for="col in resultColumns" :key="col" :prop="col" :label="col" min-width="120" show-overflow-tooltip />
            </el-table>
          </div>
        </el-card>

        <!-- 自然语言查询 -->
        <el-card shadow="never" class="nl-card" style="margin-top:16px;">
          <template #header><span class="card-title">自然语言查询（AI）</span></template>
          <div class="nl-input-row">
            <el-input v-model="nlQuestion" placeholder="用自然语言描述你想查询的内容，如：查看最近7天注册的用户数量" @keydown.enter="runNLQuery" />
            <el-button type="success" @click="runNLQuery" :loading="nlQuerying" :disabled="!nlQuestion.trim()">AI 查询</el-button>
          </div>
          <div v-if="nlGeneratedSQL" class="nl-result">
            <div class="nl-sql-label">生成的 SQL：</div>
            <el-input v-model="nlGeneratedSQL" type="textarea" :rows="2" readonly />
            <el-button size="small" style="margin-top:8px;" @click="sqlInput = nlGeneratedSQL; runQuery()">执行此 SQL</el-button>
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { ArrowLeft, CaretRight, Grid, Loading } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { databaseApi } from '@/api'

const route = useRoute()
const connId = Number(route.params.id)

const connInfo = ref<any>(null)
const tables = ref<string[]>([])
const selectedTable = ref('')
const columns = ref<any[]>([])
const loadingSchema = ref(false)

const sqlInput = ref('')
const queryLimit = ref(100)
const querying = ref(false)
const queryError = ref('')
const resultColumns = ref<string[]>([])
const resultRows = ref<any[]>([])

const nlQuestion = ref('')
const nlQuerying = ref(false)
const nlGeneratedSQL = ref('')

async function loadConnInfo() {
  try {
    connInfo.value = await databaseApi.get(connId)
  } catch { /* ignore */ }
}

async function loadSchema() {
  loadingSchema.value = true
  try {
    const res = await databaseApi.getSchema(connId)
    tables.value = res.tables || []
  } catch (e: any) {
    ElMessage.error('加载表结构失败: ' + (e?.message || ''))
  } finally {
    loadingSchema.value = false
  }
}

async function selectTable(table: string) {
  selectedTable.value = table
  try {
    const res = await databaseApi.getSchema(connId, table)
    columns.value = res.columns || []
  } catch { columns.value = [] }
}

async function runQuery() {
  if (!sqlInput.value.trim()) return
  querying.value = true
  queryError.value = ''
  resultColumns.value = []
  resultRows.value = []
  try {
    const res = await databaseApi.query(connId, { sql: sqlInput.value, limit: queryLimit.value })
    resultColumns.value = res.columns || []
    resultRows.value = res.rows || []
  } catch (e: any) {
    queryError.value = e?.response?.data?.detail || e?.message || '查询失败'
  } finally {
    querying.value = false
  }
}

async function runNLQuery() {
  if (!nlQuestion.value.trim()) return
  nlQuerying.value = true
  nlGeneratedSQL.value = ''
  try {
    // 获取表结构作为上下文
    let schemaContext = ''
    if (tables.value.length) {
      const tableDetails = []
      for (const t of tables.value.slice(0, 10)) {
        try {
          const r = await databaseApi.getSchema(connId, t)
          const cols = (r.columns || []).map((c: any) => `${c.name}(${c.type})`).join(', ')
          tableDetails.push(`${t}: ${cols}`)
        } catch { tableDetails.push(t) }
      }
      schemaContext = tableDetails.join('\n')
    }
    // 简单本地 SQL 生成提示（后续可对接 Agent）
    ElMessage.info('自然语言查询功能需要在智能体对话中使用，请将此数据库连接配置到 Agent 工具中')
    nlGeneratedSQL.value = `-- 请在智能体对话中提问：${nlQuestion.value}\n-- 表结构：\n-- ${schemaContext.replace(/\n/g, '\n-- ')}`
  } catch (e: any) {
    ElMessage.error(e?.message || '生成失败')
  } finally {
    nlQuerying.value = false
  }
}

onMounted(() => {
  loadConnInfo()
  loadSchema()
})
</script>

<style scoped>
.page-container { padding: 20px; }
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
.header-left { display: flex; align-items: center; gap: 12px; }
.header-left h2 { margin: 0; font-size: 18px; }
.card-title { font-weight: 600; }

.schema-card { min-height: 400px; }
.table-item {
  padding: 8px 12px; cursor: pointer; border-radius: 6px; font-size: 13px;
  display: flex; align-items: center; gap: 6px; transition: all 0.2s;
}
.table-item:hover { background: #f5f7fa; }
.table-item.active { background: #ecf5ff; color: #409eff; font-weight: 500; }

.columns-panel { border-top: 1px solid #ebeef5; margin-top: 12px; padding-top: 12px; }
.columns-title { font-weight: 600; font-size: 13px; margin-bottom: 8px; }
.column-item { display: flex; align-items: center; gap: 6px; padding: 4px 0; font-size: 12px; }
.col-name { font-weight: 500; min-width: 80px; }

.sql-input :deep(.el-textarea__inner) { font-family: 'Consolas', 'Courier New', monospace; font-size: 13px; }

.query-error { margin-top: 12px; }

.result-section { margin-top: 16px; }
.result-header { font-size: 13px; color: #666; margin-bottom: 8px; }

.nl-input-row { display: flex; gap: 8px; }
.nl-result { margin-top: 12px; }
.nl-sql-label { font-size: 13px; color: #666; margin-bottom: 4px; }
</style>
