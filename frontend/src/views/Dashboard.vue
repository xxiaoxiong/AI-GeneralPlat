<template>
  <div class="dashboard">
    <!-- Welcome Banner -->
    <div class="welcome-banner">
      <div class="banner-left">
        <h2>欢迎回来，{{ authStore.user?.full_name || authStore.user?.username }} 👋</h2>
        <p>{{ currentDate }} · AI 通用能力大平台平台</p>
      </div>
      <div class="banner-right">
        <el-button type="primary" class="banner-btn" @click="router.push('/agents')">
          <el-icon><Promotion /></el-icon> 开始对话
        </el-button>
      </div>
    </div>

    <!-- Stats -->
    <el-row :gutter="16" class="stats-row">
      <el-col :span="6" v-for="stat in stats" :key="stat.label">
        <el-card class="stat-card" shadow="never" @click="router.push(stat.path)">
          <div class="stat-content">
            <div class="stat-icon" :style="{ background: stat.color + '18', color: stat.color }">
              <el-icon size="22"><component :is="stat.icon" /></el-icon>
            </div>
            <div class="stat-info">
              <div class="stat-value">{{ stat.value }}</div>
              <div class="stat-label">{{ stat.label }}</div>
            </div>
            <div class="stat-arrow">→</div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- Quick Actions + Chart -->
    <el-row :gutter="16">
      <!-- Quick Actions -->
      <el-col :span="16">
        <el-card shadow="never">
          <template #header>
            <div class="card-header-row">
              <span class="card-title">快速入口</span>
              <span class="card-subtitle">常用功能一键直达</span>
            </div>
          </template>
          <div class="quick-actions">
            <div
              v-for="action in quickActions"
              :key="action.label"
              class="action-item"
              @click="router.push(action.path)"
            >
              <div class="action-icon" :style="{ background: action.color + '15', color: action.color }">
                <el-icon size="22"><component :is="action.icon" /></el-icon>
              </div>
              <span class="action-label">{{ action.label }}</span>
              <span class="action-desc">{{ action.desc }}</span>
            </div>
          </div>
        </el-card>
      </el-col>

      <!-- Stats Chart -->
      <el-col :span="8">
        <el-card shadow="never" style="height:100%">
          <template #header>
            <div class="card-header-row">
              <span class="card-title">资源概览</span>
            </div>
          </template>
          <div class="chart-bars">
            <div v-for="stat in stats" :key="stat.label" class="chart-bar-row">
              <div class="chart-bar-label">{{ stat.label }}</div>
              <div class="chart-bar-track">
                <div
                  class="chart-bar-fill"
                  :style="{ width: barWidth(stat.value) + '%', background: stat.color }"
                ></div>
              </div>
              <div class="chart-bar-val" :style="{ color: stat.color }">{{ stat.value }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- Feature Overview -->
    <el-row :gutter="16">
      <el-col :span="24">
        <el-card shadow="never">
          <template #header><span class="card-title">平台能力</span></template>
          <div class="feature-grid">
            <div v-for="f in features" :key="f.name" class="feature-card" @click="router.push(f.path)">
              <div class="feature-icon" :style="{ background: f.color + '15', color: f.color }">
                <el-icon size="20"><component :is="f.icon" /></el-icon>
              </div>
              <div class="feature-name">{{ f.name }}</div>
              <div class="feature-desc">{{ f.desc }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { modelApi, knowledgeApi, workflowApi, appApi, agentApi } from '@/api'
import { Promotion } from '@element-plus/icons-vue'
import dayjs from 'dayjs'

const router = useRouter()
const authStore = useAuthStore()
const currentDate = dayjs().format('YYYY年MM月DD日 dddd')

const stats = ref([
  { label: '模型供应商', value: 0, icon: 'Cpu', color: '#3b82f6', path: '/models' },
  { label: '知识库', value: 0, icon: 'Collection', color: '#10a37f', path: '/knowledge' },
  { label: '业务流程', value: 0, icon: 'Share', color: '#f59e0b', path: '/workflows' },
  { label: '应用实例', value: 0, icon: 'Grid', color: '#ef4444', path: '/apps/instances' },
])

const maxStatVal = computed(() => Math.max(...stats.value.map(s => s.value), 1))
function barWidth(val: number) {
  return Math.max(4, Math.round((val / maxStatVal.value) * 100))
}

const quickActions = [
  { label: 'AI 对话', desc: '与模型直接对话', path: '/models/chat', icon: 'ChatDotRound', color: '#3b82f6' },
  { label: 'Agent 智能体', desc: '自主规划执行任务', path: '/agents', icon: 'Promotion', color: '#10a37f' },
  { label: '流程编排', desc: '可视化工作流设计', path: '/workflows', icon: 'Share', color: '#f59e0b' },
  { label: '知识库', desc: '企业知识管理', path: '/knowledge', icon: 'Collection', color: '#8b5cf6' },
  { label: '提示词工程', desc: '模板管理与调试', path: '/prompts', icon: 'EditPen', color: '#ec4899' },
  { label: '应用市场', desc: '一键部署应用', path: '/apps', icon: 'Grid', color: '#ef4444' },
  { label: '模型管理', desc: '供应商与模型配置', path: '/models', icon: 'Cpu', color: '#6b7280' },
  { label: '我的应用', desc: '已部署的应用实例', path: '/apps/instances', icon: 'Star', color: '#f97316' },
]

const features = [
  { name: '多模型统一调度', desc: '支持国内外所有主流大模型，统一管控', icon: 'Cpu', color: '#3b82f6', path: '/models' },
  { name: 'Agent 智能体', desc: '自主规划、工具调用、多轮推理', icon: 'Promotion', color: '#10a37f', path: '/agents' },
  { name: '企业知识库', desc: '多格式文档上传，RAG 精准检索溯源', icon: 'Collection', color: '#8b5cf6', path: '/knowledge' },
  { name: '无代码流程编排', desc: '可视化拖拽，非技术人员即可上手', icon: 'Share', color: '#f59e0b', path: '/workflows' },
  // { name: '全场景应用市场', desc: '100+ 行业场景，一键启用', icon: 'Grid', color: '#ef4444', path: '/apps' },
  { name: '全链路数据安全', desc: '数据全程加密，审计留痕，合规可查', icon: 'Shield', color: '#6b7280', path: '/system/audit' },
]

onMounted(async () => {
  try {
    const [providers, kbs, workflows, instances] = await Promise.allSettled([
      modelApi.listProviders(),
      knowledgeApi.listBases({ page_size: 1 }),
      workflowApi.list({ page_size: 1 }),
      appApi.listInstances({ page_size: 1 }),
    ])
    if (providers.status === 'fulfilled') stats.value[0].value = providers.value.length
    if (kbs.status === 'fulfilled') stats.value[1].value = kbs.value.total
    if (workflows.status === 'fulfilled') stats.value[2].value = workflows.value.total
    if (instances.status === 'fulfilled') stats.value[3].value = instances.value.total
  } catch {}
})
</script>

<style scoped>
.dashboard { padding: 16px 20px; background: #f9f9f9; height: calc(100vh - 56px); overflow: hidden; display: flex; flex-direction: column; gap: 12px; box-sizing: border-box; }

/* Banner */
.welcome-banner {
  background: linear-gradient(135deg, #10a37f 0%, #0a7a62 100%);
  border-radius: 12px;
  padding: 14px 22px;
  color: #fff;
  flex-shrink: 0;
  box-shadow: 0 4px 16px rgba(16,163,127,0.25);
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.banner-left h2 { font-size: 20px; font-weight: 700; margin-bottom: 5px; }
.banner-left p { font-size: 13px; opacity: 0.82; }
.banner-btn {
  background: rgba(255,255,255,0.2) !important;
  border-color: rgba(255,255,255,0.4) !important;
  color: #fff !important;
  border-radius: 8px !important;
  font-size: 13px !important;
}
.banner-btn:hover { background: rgba(255,255,255,0.3) !important; }

/* Stats */
.stats-row { margin-bottom: 0; flex-shrink: 0; }
.stat-card {
  border-radius: 12px;
  border: 1px solid #e5e5e5 !important;
  box-shadow: 0 1px 3px rgba(0,0,0,0.05) !important;
  cursor: pointer;
  transition: all 0.18s;
  background: #fff !important;
}
.stat-card:hover { border-color: #10a37f !important; box-shadow: 0 4px 12px rgba(16,163,127,0.1) !important; transform: translateY(-2px); }
.stat-content { display: flex; align-items: center; gap: 14px; }
.stat-icon { width: 48px; height: 48px; border-radius: 12px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
.stat-info { flex: 1; }
.stat-value { font-size: 22px; font-weight: 700; color: #0d0d0d; line-height: 1; }
.stat-label { font-size: 12px; color: #9ca3af; margin-top: 4px; }
.stat-arrow { font-size: 16px; color: #d1d5db; }

/* Card header */
.card-title { font-weight: 600; font-size: 15px; color: #0d0d0d; }
.card-header-row { display: flex; align-items: baseline; gap: 10px; }
.card-subtitle { font-size: 12px; color: #9ca3af; }

/* Quick Actions */
.quick-actions {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
}
.action-item {
  display: flex; flex-direction: column; align-items: center; gap: 4px;
  padding: 10px 6px 8px; border-radius: 10px; cursor: pointer;
  transition: all 0.15s; border: 1px solid #f3f4f6;
  background: #fafafa;
}
.action-item:hover { background: #f0fdf9; border-color: #10a37f; }
.action-icon { width: 36px; height: 36px; border-radius: 10px; display: flex; align-items: center; justify-content: center; }
.action-label { font-size: 13px; font-weight: 500; color: #0d0d0d; }
.action-desc { font-size: 11px; color: #9ca3af; text-align: center; line-height: 1.3; }

/* Bar Chart */
.chart-bars { display: flex; flex-direction: column; gap: 10px; padding: 4px 0; }
.dashboard :deep(.el-card__body) { padding: 12px 16px !important; }
.dashboard :deep(.el-card__header) { padding: 10px 16px !important; }
.chart-bar-row { display: flex; align-items: center; gap: 10px; }
.chart-bar-label { font-size: 12px; color: #6b7280; width: 64px; flex-shrink: 0; }
.chart-bar-track {
  flex: 1; height: 8px; background: #f3f4f6; border-radius: 4px; overflow: hidden;
}
.chart-bar-fill {
  height: 100%; border-radius: 4px;
  transition: width 0.6s cubic-bezier(0.4,0,0.2,1);
}
.chart-bar-val { font-size: 13px; font-weight: 600; width: 24px; text-align: right; flex-shrink: 0; }

/* Feature Grid */
.feature-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 8px;
}
.feature-card {
  display: flex; flex-direction: column; align-items: center; gap: 6px;
  padding: 10px 6px; border-radius: 10px; cursor: pointer;
  transition: all 0.15s; border: 1px solid #f3f4f6; background: #fafafa;
  text-align: center;
}
.feature-card:hover { background: #f0fdf9; border-color: #10a37f; }
.feature-icon { width: 34px; height: 34px; border-radius: 10px; display: flex; align-items: center; justify-content: center; }
.feature-name { font-size: 13px; font-weight: 500; color: #0d0d0d; }
.feature-desc { font-size: 11px; color: #9ca3af; line-height: 1.4; }
</style>
