export interface User {
  id: number
  username: string
  email: string
  full_name?: string
  avatar?: string
  is_active: boolean
  is_superuser: boolean
  department?: string
  phone?: string
  created_at: string
}

export interface Role {
  id: number
  name: string
  display_name: string
  description?: string
  is_system: boolean
}

export interface ModelProvider {
  id: number
  name: string
  display_name: string
  provider_type: string
  base_url: string
  is_active: boolean
  is_default: boolean
  description?: string
  logo_url?: string
  created_at: string
}

export interface ModelConfig {
  id: number
  provider_id: number
  name: string
  display_name: string
  model_type: string
  context_length: number
  max_tokens: number
  input_price: number
  output_price: number
  is_active: boolean
  supports_streaming: boolean
  supports_function_call: boolean
  description?: string
  created_at: string
}

export interface KnowledgeBase {
  id: number
  name: string
  description?: string
  embedding_model?: string
  chunk_size: number
  chunk_overlap: number
  is_active: boolean
  owner_id: number
  doc_count: number
  vector_count: number
  created_at: string
}

export interface KnowledgeDocument {
  id: number
  knowledge_base_id: number
  filename: string
  file_type: string
  file_size: number
  status: 'pending' | 'processing' | 'done' | 'failed'
  chunk_count: number
  error_msg?: string
  created_at: string
}

export interface Workflow {
  id: number
  name: string
  description?: string
  definition: Record<string, any>
  is_active: boolean
  is_published: boolean
  version: number
  owner_id: number
  trigger_type: string
  category?: string
  icon?: string
  created_at: string
  updated_at: string
}

export interface WorkflowExecution {
  id: number
  workflow_id: number
  status: 'pending' | 'running' | 'success' | 'failed'
  input_data?: Record<string, any>
  output_data?: Record<string, any>
  node_results?: Record<string, any>
  error_msg?: string
  duration_ms?: number
  created_at: string
}

export interface AppTemplate {
  id: number
  name: string
  display_name: string
  description?: string
  category: string
  icon?: string
  cover_image?: string
  app_type: string
  is_builtin: boolean
  is_active: boolean
  sort_order: number
  tags?: string
  usage_count: number
  created_at: string
}

export interface AppInstance {
  id: number
  template_id: number
  name: string
  description?: string
  config?: Record<string, any>
  knowledge_base_id?: number
  model_config_id?: number
  workflow_id?: number
  owner_id: number
  is_active: boolean
  is_public: boolean
  access_token?: string
  created_at: string
}

export interface AuditLog {
  id: number
  user_id?: number
  username?: string
  action: string
  module: string
  resource_type?: string
  resource_id?: string
  detail?: Record<string, any>
  ip_address?: string
  status: string
  error_msg?: string
  duration_ms?: number
  created_at: string
}

export interface PageResponse<T> {
  items: T[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'system'
  content: string
  timestamp?: number
}
