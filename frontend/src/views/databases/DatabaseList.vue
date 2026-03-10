<template>
  <div class="page-container">
    <div class="page-header">
      <h2>数据库管理</h2>
      <el-button type="primary" :icon="Plus" @click="openDialog()">添加连接</el-button>
    </div>

    <el-row :gutter="16">
      <el-col :span="8" v-for="conn in connections" :key="conn.id">
        <el-card shadow="hover" class="conn-card">
          <div class="conn-header">
            <div class="conn-icon">
              <el-icon size="28" :color="dbIconColor(conn.db_type)">
                <Coin />
              </el-icon>
            </div>
            <div class="conn-info">
              <div class="conn-name">{{ conn.name }}</div>
              <el-tag size="small" :type="conn.db_type === 'mysql' ? 'primary' : conn.db_type === 'postgresql' ? 'success' : 'warning'">
                {{ conn.db_type.toUpperCase() }}
              </el-tag>
            </div>
            <el-tag v-if="conn.last_tested_at" :type="conn.last_test_ok ? 'success' : 'danger'" size="small" effect="light">
              {{ conn.last_test_ok ? '已连通' : '连接失败' }}
            </el-tag>
          </div>
          <div class="conn-detail">
            <div><el-icon><Location /></el-icon> {{ conn.host }}:{{ conn.port }}</div>
            <div><el-icon><Box /></el-icon> {{ conn.database }}</div>
            <div v-if="conn.description" class="conn-desc">{{ conn.description }}</div>
          </div>
          <div class="conn-actions">
            <el-button size="small" :icon="Connection" @click="testConn(conn)" :loading="testingId === conn.id">测试连接</el-button>
            <el-button size="small" :icon="Search" @click="$router.push(`/databases/${conn.id}/query`)">查询</el-button>
            <el-button size="small" :icon="Edit" @click="openDialog(conn)">编辑</el-button>
            <el-button size="small" type="danger" :icon="Delete" @click="deleteConn(conn)">删除</el-button>
          </div>
        </el-card>
      </el-col>

      <el-col :span="8">
        <div class="conn-add" @click="openDialog()">
          <el-icon size="36" color="#c0c4cc"><Plus /></el-icon>
          <span>添加数据库连接</span>
        </div>
      </el-col>
    </el-row>

    <!-- 添加/编辑对话框 -->
    <el-dialog v-model="dialogVisible" :title="editingConn ? '编辑连接' : '添加连接'" width="520px" destroy-on-close>
      <el-form :model="form" label-width="90px" :rules="rules" ref="formRef">
        <el-form-item label="连接名称" prop="name">
          <el-input v-model="form.name" placeholder="如: 生产数据库" />
        </el-form-item>
        <el-form-item label="数据库类型" prop="db_type">
          <el-select v-model="form.db_type" @change="onDbTypeChange">
            <el-option label="MySQL" value="mysql" />
            <el-option label="PostgreSQL" value="postgresql" />
            <el-option label="SQLite" value="sqlite" />
          </el-select>
        </el-form-item>
        <el-form-item label="主机地址" prop="host" v-if="form.db_type !== 'sqlite'">
          <el-input v-model="form.host" placeholder="localhost" />
        </el-form-item>
        <el-form-item label="端口" prop="port" v-if="form.db_type !== 'sqlite'">
          <el-input-number v-model="form.port" :min="1" :max="65535" />
        </el-form-item>
        <el-form-item :label="form.db_type === 'sqlite' ? '文件路径' : '数据库名'" prop="database">
          <el-input v-model="form.database" :placeholder="form.db_type === 'sqlite' ? '/path/to/db.sqlite' : 'my_database'" />
        </el-form-item>
        <el-form-item label="用户名" v-if="form.db_type !== 'sqlite'">
          <el-input v-model="form.username" placeholder="root" />
        </el-form-item>
        <el-form-item label="密码" v-if="form.db_type !== 'sqlite'">
          <el-input v-model="form.password" type="password" show-password placeholder="密码" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" type="textarea" :rows="2" placeholder="可选：描述此连接用途" />
        </el-form-item>
        <el-form-item label="额外参数">
          <el-input v-model="form.extra_params" placeholder="charset=utf8mb4&connect_timeout=10" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="submitForm" :loading="submitting">{{ editingConn ? '保存' : '创建' }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { Plus, Edit, Delete, Search, Connection, Location, Box, Coin } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { databaseApi } from '@/api'

const connections = ref<any[]>([])
const dialogVisible = ref(false)
const editingConn = ref<any>(null)
const testingId = ref<number | null>(null)
const submitting = ref(false)
const formRef = ref()

const defaultForm = () => ({
  name: '',
  db_type: 'mysql',
  host: 'localhost',
  port: 3306,
  database: '',
  username: 'root',
  password: '',
  description: '',
  extra_params: '',
})
const form = ref(defaultForm())

const rules = {
  name: [{ required: true, message: '请输入连接名称', trigger: 'blur' }],
  db_type: [{ required: true, message: '请选择数据库类型', trigger: 'change' }],
  database: [{ required: true, message: '请输入数据库名', trigger: 'blur' }],
}

function dbIconColor(type: string) {
  return type === 'mysql' ? '#4479A1' : type === 'postgresql' ? '#336791' : '#003B57'
}

function onDbTypeChange(val: string) {
  if (val === 'mysql') form.value.port = 3306
  else if (val === 'postgresql') form.value.port = 5432
}

async function loadConnections() {
  try {
    const res = await databaseApi.list()
    connections.value = res.items || []
  } catch { /* ignore */ }
}

function openDialog(conn?: any) {
  if (conn) {
    editingConn.value = conn
    form.value = { ...conn }
  } else {
    editingConn.value = null
    form.value = defaultForm()
  }
  dialogVisible.value = true
}

async function submitForm() {
  try {
    await formRef.value?.validate()
  } catch { return }

  submitting.value = true
  try {
    if (editingConn.value) {
      await databaseApi.update(editingConn.value.id, form.value)
      ElMessage.success('已更新')
    } else {
      await databaseApi.create(form.value)
      ElMessage.success('已创建')
    }
    dialogVisible.value = false
    await loadConnections()
  } catch (e: any) {
    ElMessage.error(e?.message || '操作失败')
  } finally {
    submitting.value = false
  }
}

async function testConn(conn: any) {
  testingId.value = conn.id
  try {
    const res = await databaseApi.test(conn.id)
    if (res.ok) {
      ElMessage.success('连接成功')
    } else {
      ElMessage.error(res.message || '连接失败')
    }
    await loadConnections()
  } catch (e: any) {
    ElMessage.error(e?.message || '测试失败')
  } finally {
    testingId.value = null
  }
}

async function deleteConn(conn: any) {
  try {
    await ElMessageBox.confirm(`确定删除连接「${conn.name}」？`, '确认', { type: 'warning' })
    await databaseApi.delete(conn.id)
    ElMessage.success('已删除')
    await loadConnections()
  } catch { /* cancel */ }
}

onMounted(loadConnections)
</script>

<style scoped>
.page-container { padding: 20px; }
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
.page-header h2 { margin: 0; font-size: 20px; }

.conn-card { margin-bottom: 16px; }
.conn-header { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.conn-icon { width: 40px; height: 40px; border-radius: 8px; background: #f5f7fa; display: flex; align-items: center; justify-content: center; }
.conn-info { flex: 1; }
.conn-name { font-weight: 600; font-size: 15px; margin-bottom: 4px; }
.conn-detail { font-size: 13px; color: #666; line-height: 1.8; margin-bottom: 12px; }
.conn-detail .el-icon { margin-right: 4px; vertical-align: middle; }
.conn-desc { color: #999; font-size: 12px; margin-top: 4px; }
.conn-actions { display: flex; flex-wrap: wrap; gap: 6px; }

.conn-add {
  height: 200px;
  border: 2px dashed #dcdfe6;
  border-radius: 8px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: all 0.3s;
  gap: 8px;
  color: #c0c4cc;
  margin-bottom: 16px;
}
.conn-add:hover { border-color: #409eff; color: #409eff; }
</style>
