<template>
  <el-container class="main-layout">
    <!-- Sidebar -->
    <el-aside :width="isCollapsed ? '64px' : '220px'" class="sidebar">
      <div class="logo" :class="{ collapsed: isCollapsed }">
        <el-icon size="24" color="#409eff"><Cpu /></el-icon>
        <span v-if="!isCollapsed" class="logo-text">AI 通用能力大平台</span>
      </div>
      <el-menu
        :default-active="activeMenu"
        :collapse="isCollapsed"
        background-color="#f9f9f9"
        text-color="#374151"
        active-text-color="#0d0d0d"
        class="sidebar-menu"
        @select="handleMenuSelect"
      >
        <!-- 工作台 -->
        <el-menu-item index="/dashboard">
          <el-icon><Odometer /></el-icon>
          <template #title>工作台</template>
        </el-menu-item>

        <!-- 分组：AI 能力 -->
        <div v-if="!isCollapsed" class="menu-group-label">AI 能力</div>
        <div v-else class="menu-divider" />

        <el-menu-item index="/models/chat">
          <el-icon><ChatDotRound /></el-icon>
          <template #title>AI 对话</template>
        </el-menu-item>

        <el-sub-menu index="prompts">
          <template #title>
            <el-icon><EditPen /></el-icon>
            <span>提示词</span>
          </template>
          <el-menu-item index="/prompts">模板库</el-menu-item>
          <el-menu-item index="/prompts/debugger">调试器</el-menu-item>
        </el-sub-menu>

        <el-menu-item index="/models">
          <el-icon><Cpu /></el-icon>
          <template #title>模型配置</template>
        </el-menu-item>

        <!-- 分组：知识与智能体 -->
        <div v-if="!isCollapsed" class="menu-group-label">知识与智能体</div>
        <div v-else class="menu-divider" />

        <el-menu-item index="/knowledge">
          <el-icon><Collection /></el-icon>
          <template #title>知识库</template>
        </el-menu-item>

        <el-menu-item index="/agents">
          <el-icon><Promotion /></el-icon>
          <template #title>Agent 智能体</template>
        </el-menu-item>

        <el-menu-item index="/databases">
          <el-icon><Coin /></el-icon>
          <template #title>数据库管理</template>
        </el-menu-item>

        <!-- 分组：自动化 -->
        <div v-if="!isCollapsed" class="menu-group-label">自动化</div>
        <div v-else class="menu-divider" />

        <el-menu-item index="/workflows">
          <el-icon><Share /></el-icon>
          <template #title>流程编排</template>
        </el-menu-item>

        <!-- 系统管理（仅超管可见） -->
        <template v-if="authStore.isSuperuser">
          <div v-if="!isCollapsed" class="menu-group-label">系统管理</div>
          <div v-else class="menu-divider" />
          <el-menu-item index="/system/users">
            <el-icon><User /></el-icon>
            <template #title>用户管理</template>
          </el-menu-item>
          <el-menu-item index="/system/audit">
            <el-icon><Document /></el-icon>
            <template #title>审计日志</template>
          </el-menu-item>
        </template>
      </el-menu>
    </el-aside>

    <el-container>
      <!-- Header -->
      <el-header class="header">
        <div class="header-left">
          <el-icon class="collapse-btn" @click="isCollapsed = !isCollapsed" size="20">
            <Fold v-if="!isCollapsed" /><Expand v-else />
          </el-icon>
          <el-breadcrumb separator="/">
            <el-breadcrumb-item :to="{ path: '/' }">首页</el-breadcrumb-item>
            <el-breadcrumb-item v-if="currentTitle">{{ currentTitle }}</el-breadcrumb-item>
          </el-breadcrumb>
        </div>
        <div class="header-right">
          <el-dropdown @command="handleCommand">
            <div class="user-info">
              <el-avatar :size="32" :src="authStore.user?.avatar">
                {{ authStore.user?.username?.charAt(0).toUpperCase() }}
              </el-avatar>
              <span class="username">{{ authStore.user?.full_name || authStore.user?.username }}</span>
              <el-icon><ArrowDown /></el-icon>
            </div>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="profile">个人信息</el-dropdown-item>
                <el-dropdown-item command="password">修改密码</el-dropdown-item>
                <el-dropdown-item divided command="logout">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>

      <!-- Main Content -->
      <el-main class="main-content">
        <router-view />
      </el-main>
    </el-container>
  </el-container>

  <!-- Change Password Dialog -->
  <el-dialog v-model="showPasswordDialog" title="修改密码" width="400px">
    <el-form :model="pwdForm" label-width="80px">
      <el-form-item label="原密码">
        <el-input v-model="pwdForm.old_password" type="password" show-password />
      </el-form-item>
      <el-form-item label="新密码">
        <el-input v-model="pwdForm.new_password" type="password" show-password />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="showPasswordDialog = false">取消</el-button>
      <el-button type="primary" @click="submitPassword">确认修改</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { authApi } from '@/api'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

const isCollapsed = ref(false)
const showPasswordDialog = ref(false)
const pwdForm = ref({ old_password: '', new_password: '' })

const activeMenu = computed(() => route.path)
const currentTitle = computed(() => route.meta?.title as string || '')

function handleMenuSelect(index: string) {
  if (index.startsWith('/') && route.path !== index) {
    router.push(index)
  }
}

async function handleCommand(cmd: string) {
  if (cmd === 'logout') {
    await ElMessageBox.confirm('确认退出登录？', '提示', { type: 'warning' })
    authStore.logout()
    router.push('/login')
  } else if (cmd === 'password') {
    showPasswordDialog.value = true
  }
}

async function submitPassword() {
  if (!pwdForm.value.old_password || !pwdForm.value.new_password) {
    return ElMessage.warning('请填写完整')
  }
  await authApi.changePassword(pwdForm.value)
  ElMessage.success('密码修改成功，请重新登录')
  showPasswordDialog.value = false
  authStore.logout()
  router.push('/login')
}
</script>

<style scoped>
.main-layout {
  height: 100vh;
  overflow: hidden;
}

/* ── 侧边栏：OpenAI 亮色风格 ── */
.sidebar {
  background: #f9f9f9;
  border-right: 1px solid #e5e5e5;
  transition: width 0.25s ease;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.logo {
  height: 56px;
  display: flex;
  align-items: center;
  padding: 0 18px;
  gap: 10px;
  border-bottom: 1px solid #e5e5e5;
  flex-shrink: 0;
}
.logo.collapsed {
  padding: 0;
  justify-content: center;
}
.logo-text {
  color: #0d0d0d;
  font-size: 15px;
  font-weight: 700;
  white-space: nowrap;
  letter-spacing: -0.2px;
}

.sidebar-menu {
  border-right: none !important;
  background: #f9f9f9 !important;
  flex: 1;
  overflow-y: auto;
  padding: 6px 8px;
}
.sidebar-menu::-webkit-scrollbar { width: 3px; }
.sidebar-menu::-webkit-scrollbar-thumb { background: #e5e5e5; border-radius: 2px; }

.menu-group-label {
  padding: 14px 12px 4px;
  font-size: 11px;
  font-weight: 600;
  color: #9ca3af;
  letter-spacing: 0.6px;
  text-transform: uppercase;
  user-select: none;
}
.menu-divider {
  height: 1px;
  background: #e5e5e5;
  margin: 8px 10px;
}

/* 覆盖 Element Plus 菜单样式 */
.sidebar-menu :deep(.el-menu-item),
.sidebar-menu :deep(.el-sub-menu__title) {
  border-radius: 8px !important;
  margin-bottom: 2px !important;
  height: 40px !important;
  line-height: 40px !important;
  font-size: 14px !important;
  color: #374151 !important;
  padding: 0 12px !important;
}
.sidebar-menu :deep(.el-menu-item:hover),
.sidebar-menu :deep(.el-sub-menu__title:hover) {
  background: #efefef !important;
  color: #0d0d0d !important;
}
.sidebar-menu :deep(.el-menu-item.is-active) {
  background: #efefef !important;
  color: #0d0d0d !important;
  font-weight: 600 !important;
}
.sidebar-menu :deep(.el-sub-menu .el-menu-item) {
  padding-left: 36px !important;
  height: 36px !important;
  line-height: 36px !important;
  font-size: 13px !important;
  border-radius: 8px !important;
}
.sidebar-menu :deep(.el-menu--inline) {
  background: #f9f9f9 !important;
}
.sidebar-menu :deep(.el-menu-item .el-icon),
.sidebar-menu :deep(.el-sub-menu__title .el-icon) {
  color: #6b7280 !important;
}
.sidebar-menu :deep(.el-menu-item.is-active .el-icon) {
  color: #10a37f !important;
}

/* ── 顶栏 ── */
.header {
  height: 56px;
  background: #ffffff;
  border-bottom: 1px solid #e5e5e5;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
  flex-shrink: 0;
}
.header-left {
  display: flex;
  align-items: center;
  gap: 14px;
}
.collapse-btn {
  cursor: pointer;
  color: #9ca3af;
  transition: color 0.15s;
}
.collapse-btn:hover { color: #374151; }

/* 面包屑颜色 */
.header :deep(.el-breadcrumb__inner) { color: #6b7280 !important; font-size: 13px; }
.header :deep(.el-breadcrumb__inner.is-link:hover) { color: #10a37f !important; }
.header :deep(.el-breadcrumb__separator) { color: #d1d5db !important; }

.header-right { display: flex; align-items: center; }
.user-info {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  padding: 5px 10px;
  border-radius: 8px;
  transition: background 0.15s;
}
.user-info:hover { background: #f3f4f6; }
.username { font-size: 13px; color: #374151; font-weight: 500; }
.user-info :deep(.el-icon) { color: #9ca3af; font-size: 12px; }

/* 用户头像 */
.user-info :deep(.el-avatar) {
  background: #10a37f !important;
  font-size: 13px;
  font-weight: 600;
}

/* ── 主内容区 ── */
.main-content {
  background: #ffffff;
  overflow-y: auto;
  padding: 0;
}
</style>
