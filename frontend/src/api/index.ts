import request from '@/utils/request'
import type {
  User, ModelProvider, ModelConfig, KnowledgeBase, KnowledgeDocument,
  Workflow, WorkflowExecution, AppTemplate, AppInstance, AuditLog, PageResponse, ChatMessage
} from '@/types'

// ─── Auth ─────────────────────────────────────────────────────────────────────
export const authApi = {
  login: (data: { username: string; password: string }) =>
    request.post<any, any>('/auth/login', data),
  getMe: () => request.get<any, User>('/auth/me'),
  changePassword: (data: { old_password: string; new_password: string }) =>
    request.post('/auth/change-password', data),
  refresh: (refresh_token: string) =>
    request.post<any, any>('/auth/refresh', null, { params: { refresh_token_str: refresh_token } }),
}

// ─── Users ────────────────────────────────────────────────────────────────────
export const userApi = {
  list: (params?: any) => request.get<any, PageResponse<User>>('/users', { params }),
  create: (data: any) => request.post<any, User>('/users', data),
  update: (id: number, data: any) => request.put<any, User>(`/users/${id}`, data),
  delete: (id: number) => request.delete(`/users/${id}`),
  assignRole: (userId: number, roleId: number) =>
    request.post(`/users/${userId}/roles/${roleId}`),
  removeRole: (userId: number, roleId: number) =>
    request.delete(`/users/${userId}/roles/${roleId}`),
}

// ─── Models ───────────────────────────────────────────────────────────────────
export const modelApi = {
  listProviders: () => request.get<any, ModelProvider[]>('/models/providers'),
  createProvider: (data: any) => request.post<any, ModelProvider>('/models/providers', data),
  updateProvider: (id: number, data: any) => request.put<any, ModelProvider>(`/models/providers/${id}`, data),
  deleteProvider: (id: number) => request.delete(`/models/providers/${id}`),
  testProvider: (id: number) => request.post<any, any>(`/models/providers/${id}/test`),

  listConfigs: (params?: any) => request.get<any, ModelConfig[]>('/models/configs', { params }),
  createConfig: (data: any) => request.post<any, ModelConfig>('/models/configs', data),
  updateConfig: (id: number, data: any) => request.put<any, ModelConfig>(`/models/configs/${id}`, data),
  deleteConfig: (id: number) => request.delete(`/models/configs/${id}`),

  chat: (data: any) => request.post<any, any>('/models/chat', data),
}

// ─── Knowledge ────────────────────────────────────────────────────────────────
export const knowledgeApi = {
  listBases: (params?: any) => request.get<any, PageResponse<KnowledgeBase>>('/knowledge/bases', { params }),
  createBase: (data: any) => request.post<any, KnowledgeBase>('/knowledge/bases', data),
  getBase: (id: number) => request.get<any, KnowledgeBase>(`/knowledge/bases/${id}`),
  updateBase: (id: number, data: any) => request.put<any, KnowledgeBase>(`/knowledge/bases/${id}`, data),
  deleteBase: (id: number) => request.delete(`/knowledge/bases/${id}`),

  listDocuments: (kbId: number, params?: any) =>
    request.get<any, PageResponse<KnowledgeDocument>>(`/knowledge/bases/${kbId}/documents`, { params }),
  uploadDocument: (kbId: number, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request.post<any, KnowledgeDocument>(`/knowledge/bases/${kbId}/documents`, form, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  deleteDocument: (kbId: number, docId: number) =>
    request.delete(`/knowledge/bases/${kbId}/documents/${docId}`),

  search: (data: any) => request.post<any, any>('/knowledge/search', data),
  crawlWebpage: (kbId: number, data: { url: string; title?: string }) =>
    request.post<any, KnowledgeDocument>(`/knowledge/bases/${kbId}/crawl`, data),
}

// ─── Workflows ────────────────────────────────────────────────────────────────
export const workflowApi = {
  list: (params?: any) => request.get<any, PageResponse<Workflow>>('/workflows', { params }),
  create: (data: any) => request.post<any, Workflow>('/workflows', data),
  get: (id: number) => request.get<any, Workflow>(`/workflows/${id}`),
  update: (id: number, data: any) => request.put<any, Workflow>(`/workflows/${id}`, data),
  delete: (id: number) => request.delete(`/workflows/${id}`),
  execute: (id: number, data?: any) => request.post<any, WorkflowExecution>(`/workflows/${id}/execute`, data || {}),
  listExecutions: (id: number, params?: any) =>
    request.get<any, PageResponse<WorkflowExecution>>(`/workflows/${id}/executions`, { params }),
  exportDoc: (id: number, data: { content: string; title?: string; filename?: string }) =>
    request.post(`/workflows/${id}/export-doc`, data, { responseType: 'blob' }),
}

// ─── App Market ───────────────────────────────────────────────────────────────
export const appApi = {
  listTemplates: (params?: any) => request.get<any, PageResponse<AppTemplate>>('/apps/templates', { params }),
  getTemplate: (id: number) => request.get<any, AppTemplate>(`/apps/templates/${id}`),
  listInstances: (params?: any) => request.get<any, PageResponse<AppInstance>>('/apps/instances', { params }),
  createInstance: (data: any) => request.post<any, AppInstance>('/apps/instances', data),
  updateInstance: (id: number, data: any) => request.put<any, AppInstance>(`/apps/instances/${id}`, data),
  deleteInstance: (id: number) => request.delete(`/apps/instances/${id}`),
}

// ─── Audit ────────────────────────────────────────────────────────────────────
export const auditApi = {
  list: (params?: any) => request.get<any, PageResponse<AuditLog>>('/audit', { params }),
}

// ─── Agents ───────────────────────────────────────────────────────────────────
export const agentApi = {
  list: (params?: any) => request.get<any, any>('/agents', { params }),
  create: (data: any) => request.post<any, any>('/agents', data),
  get: (id: number) => request.get<any, any>(`/agents/${id}`),
  update: (id: number, data: any) => request.put<any, any>(`/agents/${id}`, data),
  delete: (id: number) => request.delete(`/agents/${id}`),
  listTools: () => request.get<any, any>('/agents/tools'),
  listSessions: (agentId: number) => request.get<any, any>(`/agents/${agentId}/sessions`),
  createSession: (agentId: number) => request.post<any, any>(`/agents/${agentId}/sessions`),
  deleteSession: (agentId: number, sessionId: number) =>
    request.delete(`/agents/${agentId}/sessions/${sessionId}`),
  getSessionMessages: (agentId: number, sessionId: number) =>
    request.get<any, any>(`/agents/${agentId}/sessions/${sessionId}/messages`),
  chat: (agentId: number, data: any) => request.post<any, any>(`/agents/${agentId}/chat`, data),
  listMemories: (params?: { agent_id?: number }) => request.get<any, any>('/agents/memories', { params }),
  addMemory: (data: { content: string; agent_id?: number; importance?: number; tags?: string }) =>
    request.post<any, any>('/agents/memories', data),
  deleteMemory: (id: number) => request.delete(`/agents/memories/${id}`),
  clearMemories: (params?: { agent_id?: number }) => request.delete('/agents/memories', { params }),
}

// ─── Database Connections ─────────────────────────────────────────────────────
export const databaseApi = {
  list: () => request.get<any, any>('/databases'),
  create: (data: any) => request.post<any, any>('/databases', data),
  get: (id: number) => request.get<any, any>(`/databases/${id}`),
  update: (id: number, data: any) => request.put<any, any>(`/databases/${id}`, data),
  delete: (id: number) => request.delete(`/databases/${id}`),
  test: (id: number) => request.post<any, any>(`/databases/${id}/test`),
  getSchema: (id: number, table?: string) =>
    request.get<any, any>(`/databases/${id}/schema`, { params: table ? { table } : {} }),
  query: (id: number, data: { sql: string; limit?: number }) =>
    request.post<any, any>(`/databases/${id}/query`, data),
}

// ─── Prompt Templates ─────────────────────────────────────────────────────────
export const promptApi = {
  list: (params?: any) => request.get<any, any>('/prompts', { params }),
  create: (data: any) => request.post<any, any>('/prompts', data),
  get: (id: number) => request.get<any, any>(`/prompts/${id}`),
  update: (id: number, data: any) => request.put<any, any>(`/prompts/${id}`, data),
  delete: (id: number) => request.delete(`/prompts/${id}`),
  listVersions: (id: number) => request.get<any, any[]>(`/prompts/${id}/versions`),
  render: (data: { content: string; variables: Record<string, string> }) =>
    request.post<any, any>('/prompts/render', data),
  debug: (data: any) => request.post<any, any>('/prompts/debug', data),
}
