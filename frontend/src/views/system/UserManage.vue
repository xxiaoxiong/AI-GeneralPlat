<template>
  <div class="page-container">
    <div class="page-header">
      <h2>用户管理</h2>
      <el-button type="primary" :icon="Plus" @click="openDialog()">新建用户</el-button>
    </div>

    <el-card shadow="never">
      <div class="toolbar">
        <el-input v-model="keyword" placeholder="搜索用户名/邮箱..." clearable style="width:240px;" @input="loadData" />
        <el-select v-model="filterActive" placeholder="全部状态" clearable style="width:120px;" @change="loadData">
          <el-option label="启用" :value="true" />
          <el-option label="禁用" :value="false" />
        </el-select>
      </div>

      <el-table :data="users" v-loading="loading" stripe style="margin-top:12px;">
        <el-table-column label="用户" min-width="160">
          <template #default="{ row }">
            <div style="display:flex;align-items:center;gap:10px;">
              <el-avatar :size="32" style="background:#409eff;flex-shrink:0;">
                {{ row.username.charAt(0).toUpperCase() }}
              </el-avatar>
              <div>
                <div style="font-weight:500;">{{ row.full_name || row.username }}</div>
                <div style="font-size:12px;color:#888;">{{ row.username }}</div>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="email" label="邮箱" min-width="180" />
        <el-table-column prop="department" label="部门" width="120" />
        <el-table-column label="超管" width="70">
          <template #default="{ row }">
            <el-tag v-if="row.is_superuser" type="danger" size="small">超管</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'info'" size="small">
              {{ row.is_active ? '启用' : '禁用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="注册时间" width="150">
          <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <div class="action-btns">
              <el-button size="small" @click="openDialog(row)">编辑</el-button>
              <el-button size="small" :type="row.is_active ? 'warning' : 'success'" @click="toggleActive(row)">
                {{ row.is_active ? '禁用' : '启用' }}
              </el-button>
              <el-button size="small" type="danger" @click="deleteUser(row)">删除</el-button>
            </div>
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

    <!-- Dialog -->
    <el-dialog v-model="dialogVisible" :title="editing ? '编辑用户' : '新建用户'" width="480px">
      <el-form :model="form" label-width="80px">
        <el-form-item label="用户名" v-if="!editing">
          <el-input v-model="form.username" />
        </el-form-item>
        <el-form-item label="邮箱" v-if="!editing">
          <el-input v-model="form.email" type="email" />
        </el-form-item>
        <el-form-item label="密码" v-if="!editing">
          <el-input v-model="form.password" type="password" show-password />
        </el-form-item>
        <el-form-item label="姓名">
          <el-input v-model="form.full_name" />
        </el-form-item>
        <el-form-item label="部门">
          <el-input v-model="form.department" />
        </el-form-item>
        <el-form-item label="手机">
          <el-input v-model="form.phone" />
        </el-form-item>
        <el-form-item v-if="editing" label="状态">
          <el-switch v-model="form.is_active" active-text="启用" inactive-text="禁用" />
        </el-form-item>
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
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { userApi } from '@/api'
import type { User } from '@/types'
import dayjs from 'dayjs'

const users = ref<User[]>([])
const loading = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = 20
const keyword = ref('')
const filterActive = ref<boolean | null>(null)

const dialogVisible = ref(false)
const editing = ref<User | null>(null)
const saving = ref(false)
const form = reactive<any>({})

function formatDate(d: string) { return dayjs(d).format('YYYY-MM-DD HH:mm') }

async function loadData() {
  loading.value = true
  try {
    const res = await userApi.list({
      page: page.value, page_size: pageSize,
      keyword: keyword.value || undefined,
      is_active: filterActive.value ?? undefined,
    })
    users.value = res.items
    total.value = res.total
  } finally {
    loading.value = false
  }
}

function openDialog(user?: User) {
  editing.value = user || null
  Object.assign(form, user
    ? { full_name: user.full_name || '', department: user.department || '', phone: user.phone || '', is_active: user.is_active }
    : { username: '', email: '', password: '', full_name: '', department: '', phone: '' }
  )
  dialogVisible.value = true
}

async function save() {
  saving.value = true
  try {
    if (editing.value) {
      await userApi.update(editing.value.id, form)
      ElMessage.success('更新成功')
    } else {
      await userApi.create(form)
      ElMessage.success('创建成功')
    }
    dialogVisible.value = false
    await loadData()
  } finally {
    saving.value = false
  }
}

async function toggleActive(user: User) {
  await userApi.update(user.id, { is_active: !user.is_active })
  ElMessage.success(user.is_active ? '已禁用' : '已启用')
  await loadData()
}

async function deleteUser(user: User) {
  await ElMessageBox.confirm(`确认删除用户「${user.username}」？`, '警告', { type: 'warning' })
  await userApi.delete(user.id)
  ElMessage.success('删除成功')
  await loadData()
}

onMounted(loadData)
</script>

<style scoped>
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
.page-header h2 { font-size: 20px; font-weight: 700; color: #0d0d0d; }
.toolbar { display: flex; gap: 12px; }
.action-btns { display: flex; flex-wrap: nowrap; gap: 4px; white-space: nowrap; }
</style>
