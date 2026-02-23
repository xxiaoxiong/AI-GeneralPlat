<template>
  <div class="page-container">
    <div class="page-header">
      <h2>模型管理</h2>
      <el-button type="primary" :icon="Plus" @click="openProviderDialog()">添加供应商</el-button>
    </div>

    <!-- Providers -->
    <el-card shadow="never" style="margin-bottom: 16px;">
      <template #header>
        <span class="card-title">模型供应商</span>
      </template>
      <el-row :gutter="16">
        <el-col :span="6" v-for="p in providers" :key="p.id">
          <div class="provider-card" :class="{ inactive: !p.is_active }">
            <div class="provider-header">
              <div class="provider-name">{{ p.display_name }}</div>
              <el-tag :type="p.is_active ? 'success' : 'info'" size="small">
                {{ p.is_active ? '启用' : '停用' }}
              </el-tag>
            </div>
            <div class="provider-type">{{ providerTypeLabel(p.provider_type) }}</div>
            <div class="provider-url text-ellipsis">{{ p.base_url }}</div>
            <div class="provider-actions">
              <el-button size="small" @click="testProvider(p)">连接测试</el-button>
              <el-button size="small" @click="openProviderDialog(p)">编辑</el-button>
              <el-button size="small" type="danger" @click="deleteProvider(p)">删除</el-button>
            </div>
          </div>
        </el-col>
        <el-col :span="6">
          <div class="provider-add" @click="openProviderDialog()">
            <el-icon size="32" color="#c0c4cc"><Plus /></el-icon>
            <span>添加供应商</span>
          </div>
        </el-col>
      </el-row>
    </el-card>

    <!-- Model Configs -->
    <el-card shadow="never">
      <template #header>
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <span class="card-title">模型配置</span>
          <div style="display:flex;gap:8px;">
            <el-select v-model="filterProviderId" placeholder="按供应商筛选" clearable style="width:160px;">
              <el-option v-for="p in providers" :key="p.id" :label="p.display_name" :value="p.id" />
            </el-select>
            <el-select v-model="filterModelType" placeholder="按类型筛选" clearable style="width:140px;">
              <el-option label="对话" value="chat" />
              <el-option label="嵌入" value="embedding" />
              <el-option label="图像" value="image" />
            </el-select>
            <el-button type="primary" :icon="Plus" @click="openConfigDialog()">添加模型</el-button>
          </div>
        </div>
      </template>
      <el-table :data="filteredConfigs" stripe>
        <el-table-column prop="display_name" label="模型名称" min-width="140" />
        <el-table-column prop="name" label="模型ID" min-width="160" />
        <el-table-column label="供应商" width="120">
          <template #default="{ row }">
            {{ providerName(row.provider_id) }}
          </template>
        </el-table-column>
        <el-table-column label="类型" width="80">
          <template #default="{ row }">
            <el-tag size="small">{{ row.model_type }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="context_length" label="上下文" width="90" />
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'info'" size="small">
              {{ row.is_active ? '启用' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="160" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="openConfigDialog(row)">编辑</el-button>
            <el-button size="small" type="danger" @click="deleteConfig(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- Provider Dialog -->
    <el-dialog v-model="providerDialogVisible" :title="editingProvider ? '编辑供应商' : '添加供应商'" width="520px">
      <el-form :model="providerForm" label-width="90px">
        <el-form-item label="名称标识"><el-input v-model="providerForm.name" :disabled="!!editingProvider" /></el-form-item>
        <el-form-item label="显示名称"><el-input v-model="providerForm.display_name" /></el-form-item>
        <el-form-item label="类型">
          <el-select v-model="providerForm.provider_type" style="width:100%">
            <el-option label="OpenAI 兼容" value="openai_compatible" />
            <el-option label="Ollama 本地" value="ollama" />
            <el-option label="自定义" value="custom" />
          </el-select>
        </el-form-item>
        <el-form-item label="API 地址"><el-input v-model="providerForm.base_url" placeholder="https://api.example.com/v1" /></el-form-item>
        <el-form-item label="API Key"><el-input v-model="providerForm.api_key" type="password" show-password placeholder="可选" /></el-form-item>
        <el-form-item label="描述"><el-input v-model="providerForm.description" type="textarea" :rows="2" /></el-form-item>
        <el-form-item v-if="editingProvider" label="状态">
          <el-switch v-model="providerForm.is_active" active-text="启用" inactive-text="停用" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="providerDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveProvider">保存</el-button>
      </template>
    </el-dialog>

    <!-- Config Dialog -->
    <el-dialog v-model="configDialogVisible" :title="editingConfig ? '编辑模型' : '添加模型'" width="520px">
      <el-form :model="configForm" label-width="90px">
        <el-form-item label="供应商">
          <el-select v-model="configForm.provider_id" style="width:100%">
            <el-option v-for="p in providers" :key="p.id" :label="p.display_name" :value="p.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="模型ID"><el-input v-model="configForm.name" placeholder="如 gpt-4o / qwen-max" /></el-form-item>
        <el-form-item label="显示名称"><el-input v-model="configForm.display_name" /></el-form-item>
        <el-form-item label="类型">
          <el-select v-model="configForm.model_type" style="width:100%">
            <el-option label="对话 (chat)" value="chat" />
            <el-option label="嵌入 (embedding)" value="embedding" />
            <el-option label="图像 (image)" value="image" />
          </el-select>
        </el-form-item>
        <el-form-item label="上下文长度"><el-input-number v-model="configForm.context_length" :min="512" :max="1000000" /></el-form-item>
        <el-form-item label="最大输出"><el-input-number v-model="configForm.max_tokens" :min="256" :max="32768" /></el-form-item>
        <el-form-item label="流式输出"><el-switch v-model="configForm.supports_streaming" /></el-form-item>
        <el-form-item v-if="editingConfig" label="状态"><el-switch v-model="configForm.is_active" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="configDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveConfig">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, reactive } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { modelApi } from '@/api'
import type { ModelProvider, ModelConfig } from '@/types'

const providers = ref<ModelProvider[]>([])
const configs = ref<ModelConfig[]>([])
const filterProviderId = ref<number | null>(null)
const filterModelType = ref('')
const saving = ref(false)

const providerDialogVisible = ref(false)
const editingProvider = ref<ModelProvider | null>(null)
const providerForm = reactive<any>({})

const configDialogVisible = ref(false)
const editingConfig = ref<ModelConfig | null>(null)
const configForm = reactive<any>({})

const filteredConfigs = computed(() => {
  return configs.value.filter(c => {
    if (filterProviderId.value && c.provider_id !== filterProviderId.value) return false
    if (filterModelType.value && c.model_type !== filterModelType.value) return false
    return true
  })
})

function providerName(id: number) {
  return providers.value.find(p => p.id === id)?.display_name || '-'
}

function providerTypeLabel(type: string) {
  const map: Record<string, string> = { openai_compatible: 'OpenAI 兼容', ollama: 'Ollama 本地', custom: '自定义' }
  return map[type] || type
}

async function loadData() {
  const [ps, cs] = await Promise.all([modelApi.listProviders(), modelApi.listConfigs()])
  providers.value = ps
  configs.value = cs
}

function openProviderDialog(p?: ModelProvider) {
  editingProvider.value = p || null
  Object.assign(providerForm, p ? { ...p } : {
    name: '', display_name: '', provider_type: 'openai_compatible',
    base_url: '', api_key: '', description: '', is_active: true,
  })
  providerDialogVisible.value = true
}

async function saveProvider() {
  saving.value = true
  try {
    if (editingProvider.value) {
      await modelApi.updateProvider(editingProvider.value.id, providerForm)
      ElMessage.success('更新成功')
    } else {
      await modelApi.createProvider(providerForm)
      ElMessage.success('添加成功')
    }
    providerDialogVisible.value = false
    await loadData()
  } finally {
    saving.value = false
  }
}

async function deleteProvider(p: ModelProvider) {
  await ElMessageBox.confirm(`确认删除供应商「${p.display_name}」？`, '警告', { type: 'warning' })
  await modelApi.deleteProvider(p.id)
  ElMessage.success('删除成功')
  await loadData()
}

async function testProvider(p: ModelProvider) {
  const res = await modelApi.testProvider(p.id)
  if (res.success) ElMessage.success(res.message)
  else ElMessage.error(res.message)
}

function openConfigDialog(c?: ModelConfig) {
  editingConfig.value = c || null
  Object.assign(configForm, c ? { ...c } : {
    provider_id: providers.value[0]?.id || null,
    name: '', display_name: '', model_type: 'chat',
    context_length: 4096, max_tokens: 2048,
    supports_streaming: true, is_active: true,
  })
  configDialogVisible.value = true
}

async function saveConfig() {
  saving.value = true
  try {
    if (editingConfig.value) {
      await modelApi.updateConfig(editingConfig.value.id, configForm)
      ElMessage.success('更新成功')
    } else {
      await modelApi.createConfig(configForm)
      ElMessage.success('添加成功')
    }
    configDialogVisible.value = false
    await loadData()
  } finally {
    saving.value = false
  }
}

async function deleteConfig(c: ModelConfig) {
  await ElMessageBox.confirm(`确认删除模型「${c.display_name}」？`, '警告', { type: 'warning' })
  await modelApi.deleteConfig(c.id)
  ElMessage.success('删除成功')
  await loadData()
}

onMounted(loadData)
</script>

<style scoped>
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
.page-header h2 { font-size: 20px; font-weight: 700; color: #0d0d0d; }
.card-title { font-weight: 600; font-size: 15px; color: #0d0d0d; }
.provider-card {
  border: 1px solid #e5e5e5 !important; border-radius: 12px; padding: 16px;
  transition: all 0.18s; cursor: default;
  box-shadow: 0 1px 3px rgba(0,0,0,0.04) !important;
}
.provider-card:hover {
  border-color: #10a37f !important;
  box-shadow: 0 4px 12px rgba(16,163,127,0.1) !important;
}
.provider-card.inactive { opacity: 0.55; }
.provider-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
.provider-name { font-weight: 600; font-size: 15px; color: #0d0d0d; }
.provider-type { font-size: 12px; color: #9ca3af; margin-bottom: 4px; }
.provider-url { font-size: 12px; color: #10a37f; margin-bottom: 12px; }
.provider-actions { display: flex; gap: 6px; flex-wrap: wrap; }
.provider-add {
  border: 2px dashed #e5e5e5; border-radius: 12px; padding: 16px;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  min-height: 140px; cursor: pointer; color: #d1d5db; gap: 8px;
  transition: all 0.18s;
}
.provider-add:hover { border-color: #10a37f; color: #10a37f; background: #f0fdf9; }
</style>
