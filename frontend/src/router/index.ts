import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'Login',
      component: () => import('@/views/Login.vue'),
      meta: { requiresAuth: false },
    },
    {
      path: '/',
      component: () => import('@/layouts/MainLayout.vue'),
      meta: { requiresAuth: true },
      children: [
        {
          path: '',
          redirect: '/dashboard',
        },
        {
          path: 'dashboard',
          name: 'Dashboard',
          component: () => import('@/views/Dashboard.vue'),
          meta: { title: '工作台', icon: 'Odometer' },
        },
        {
          path: 'models',
          name: 'Models',
          component: () => import('@/views/models/ModelList.vue'),
          meta: { title: '模型管理', icon: 'Cpu' },
        },
        {
          path: 'models/chat',
          name: 'ModelChat',
          component: () => import('@/views/models/ModelChat.vue'),
          meta: { title: 'AI 对话', icon: 'ChatDotRound' },
        },
        {
          path: 'knowledge',
          name: 'Knowledge',
          component: () => import('@/views/knowledge/KnowledgeList.vue'),
          meta: { title: '知识库', icon: 'Collection' },
        },
        {
          path: 'knowledge/:id',
          name: 'KnowledgeDetail',
          component: () => import('@/views/knowledge/KnowledgeDetail.vue'),
          meta: { title: '知识库详情' },
        },
        {
          path: 'workflows',
          name: 'Workflows',
          component: () => import('@/views/workflows/WorkflowList.vue'),
          meta: { title: '流程编排', icon: 'Share' },
        },
        {
          path: 'workflows/:id/editor',
          name: 'WorkflowEditor',
          component: () => import('@/views/workflows/WorkflowEditor.vue'),
          meta: { title: '流程编辑器' },
        },
        {
          path: 'workflows/:id/run',
          name: 'WorkflowRun',
          component: () => import('@/views/workflows/WorkflowRun.vue'),
          meta: { title: '运行工作流' },
        },
        {
          path: 'agents',
          name: 'AgentList',
          component: () => import('@/views/agents/AgentList.vue'),
          meta: { title: 'Agent 管理', icon: 'Promotion' },
        },
        {
          path: 'agents/:id/chat',
          name: 'AgentChat',
          component: () => import('@/views/agents/AgentChat.vue'),
          meta: { title: 'Agent 对话' },
        },
        {
          path: 'prompts',
          name: 'PromptList',
          component: () => import('@/views/prompts/PromptList.vue'),
          meta: { title: '提示词库', icon: 'EditPen' },
        },
        {
          path: 'prompts/debugger',
          name: 'PromptDebugger',
          component: () => import('@/views/prompts/PromptDebugger.vue'),
          meta: { title: '提示词调试器' },
        },
        {
          path: 'system/users',
          name: 'SystemUsers',
          component: () => import('@/views/system/UserManage.vue'),
          meta: { title: '用户管理', icon: 'User', requiresSuperuser: true },
        },
        {
          path: 'system/audit',
          name: 'SystemAudit',
          component: () => import('@/views/system/AuditLog.vue'),
          meta: { title: '审计日志', icon: 'Document', requiresSuperuser: true },
        },
      ],
    },
    {
      path: '/:pathMatch(.*)*',
      redirect: '/',
    },
  ],
})

router.beforeEach(async (to, _from, next) => {
  const authStore = useAuthStore()

  if (to.meta.requiresAuth === false) {
    if (authStore.isLoggedIn && to.path === '/login') {
      return next('/')
    }
    return next()
  }

  if (!authStore.isLoggedIn) {
    return next('/login')
  }

  if (!authStore.user) {
    await authStore.fetchMe()
  }

  if (to.meta.requiresSuperuser && !authStore.isSuperuser) {
    return next('/dashboard')
  }

  next()
})

export default router
