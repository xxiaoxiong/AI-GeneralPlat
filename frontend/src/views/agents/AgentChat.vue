<template>
  <div class="agent-chat-page">
    <!-- 左侧：会话列表 -->
    <div class="session-panel">
      <div class="session-header">
        <span class="agent-icon">{{ agent?.icon || '🤖' }}</span>
        <div class="agent-info">
          <div class="agent-name">{{ agent?.name }}</div>
          <div class="agent-tools-count">{{ enabledTools.length }} 个工具</div>
        </div>
      </div>

      <el-button class="new-session-btn" type="primary" plain @click="newSession">
        <el-icon><Plus /></el-icon> 新建对话
      </el-button>

      <div class="session-list">
        <div
          v-for="s in sessions"
          :key="s.id"
          class="session-item"
          :class="{ active: currentSession?.id === s.id }"
          @click="loadSession(s)"
        >
          <div class="session-title">{{ s.title || '新对话' }}</div>
          <div class="session-meta">{{ s.message_count }} 条消息</div>
          <el-icon class="session-del" @click.stop="deleteSession(s)"><Delete /></el-icon>
        </div>
      </div>

      <div class="panel-footer">
        <el-button size="small" text @click="$router.push('/agents')">
          <el-icon><ArrowLeft /></el-icon> 返回列表
        </el-button>
      </div>
    </div>

    <!-- 右侧：对话区 -->
    <div class="chat-panel">
      <!-- 顶部工具栏 -->
      <div class="chat-toolbar">
        <div class="toolbar-left">
          <span class="toolbar-title">{{ agent?.name }}</span>
          <!-- <el-tag v-for="t in enabledTools" :key="t" size="small" class="tool-tag">{{ t }}</el-tag> -->
        </div>
        <div class="toolbar-right">
          <el-tooltip v-if="lastTraceSummary" content="最近一次执行诊断">
            <el-tag size="small" type="info">{{ lastTraceSummary.total_iterations || 0 }}轮 / {{ lastTraceSummary.total_tool_calls || 0 }}工具</el-tag>
          </el-tooltip>
          <el-tooltip content="查看/管理记忆">
            <el-button size="small" :icon="Memo" @click="memoryDialogVisible = true">记忆</el-button>
          </el-tooltip>
          <el-tooltip content="展开/收起思考链">
            <el-switch v-model="showThinking" active-text="思考链" size="small" />
          </el-tooltip>
        </div>
      </div>

      <!-- 记忆管理对话框 -->
      <el-dialog v-model="memoryDialogVisible" title="跨会话记忆" width="600px">
        <div style="margin-bottom:12px;display:flex;gap:8px;">
          <el-input v-model="newMemoryContent" placeholder="手动添加记忆内容..." style="flex:1" />
          <el-button type="primary" @click="addMemory" :disabled="!newMemoryContent.trim()">添加</el-button>
          <el-button type="danger" plain @click="clearAllMemories">清空全部</el-button>
        </div>
        <el-table :data="memories" v-loading="memoriesLoading" max-height="360" stripe>
          <el-table-column label="记忆内容" min-width="280">
            <template #default="{ row }"><span style="font-size:13px">{{ row.content }}</span></template>
          </el-table-column>
          <el-table-column label="重要性" width="80" align="center">
            <template #default="{ row }">{{ row.importance }}</template>
          </el-table-column>
          <el-table-column label="时间" width="140">
            <template #default="{ row }">{{ row.created_at?.slice(0,16).replace('T',' ') }}</template>
          </el-table-column>
          <el-table-column label="操作" width="70" fixed="right">
            <template #default="{ row }">
              <el-button size="small" type="danger" @click="deleteMemory(row.id)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="memories.length === 0 && !memoriesLoading" style="text-align:center;color:#9ca3af;padding:24px">暂无记忆</div>
      </el-dialog>

      <!-- 消息列表 -->
      <div class="messages-container" ref="messagesRef">
        <div v-if="messages.length === 0" class="welcome-area">
          <div class="welcome-icon">{{ agent?.icon || '🤖' }}</div>
          <div class="welcome-title">{{ agent?.name }}</div>
          <div class="welcome-desc">{{ agent?.description || '我是一个智能 Agent，可以使用工具帮你解决问题。' }}</div>
          <div class="welcome-tools" v-if="enabledTools.length">
            <div class="tools-label">我可以使用的工具：</div>
            <div class="tools-chips">
              <el-tag v-for="t in enabledTools" :key="t" size="small" type="info">{{ t }}</el-tag>
            </div>
          </div>
          <div class="welcome-examples">
            <div class="examples-label">试试问我：</div>
            <div
              v-for="ex in examples"
              :key="ex"
              class="example-chip"
              @click="sendExample(ex)"
            >{{ ex }}</div>
          </div>
        </div>

        <template v-for="(msg, idx) in messages">
          <!-- 用户消息 -->
          <div v-if="msg.role === 'user'" :key="'u-' + idx" class="message user-message">
            <div class="message-bubble user-bubble">{{ msg.content }}</div>
            <div class="message-avatar user-avatar">我</div>
          </div>

          <!-- Assistant 消息（含 ReAct 步骤） -->
          <div v-else-if="msg.role === 'assistant'" :key="'a-' + idx" class="message assistant-message">
            <div class="message-avatar agent-avatar">{{ agent?.icon || '🤖' }}</div>
            <div class="message-content">
              <!-- ReAct 思考链 -->
              <div v-if="showThinking && msg.steps && msg.steps.length" class="react-chain">
                <el-collapse>
                  <el-collapse-item>
                    <template #title>
                      <span class="chain-title">
                        <el-icon><View /></el-icon>
                        ReAct 推理过程（{{ msg.steps.length }} 步）
                      </span>
                    </template>
                    <div class="chain-steps">
                      <div
                        v-for="(step, si) in msg.steps"
                        :key="si"
                        class="chain-step"
                        :class="'step-' + step.type"
                      >
                        <div class="step-header">
                          <span class="step-badge" :class="'badge-' + step.type">
                            {{ stepLabel(step.type) }}
                          </span>
                          <span class="step-iter" v-if="step.iteration">第 {{ step.iteration }} 轮</span>
                        </div>
                        <div class="step-body">
                          <template v-if="step.type === 'thought'">
                            <span class="step-text">{{ step.content }}</span>
                          </template>
                          <template v-else-if="step.type === 'action'">
                            <span class="step-tool">🔧 {{ step.tool }}</span>
                            <pre class="step-code">{{ JSON.stringify(step.input, null, 2) }}</pre>
                          </template>
                          <template v-else-if="step.type === 'observation'">
                            <pre class="step-obs">{{ step.content }}</pre>
                          </template>
                          <template v-else-if="step.type === 'error'">
                            <span class="step-error">{{ step.content }}</span>
                          </template>
                        </div>
                      </div>
                    </div>
                  </el-collapse-item>
                </el-collapse>
              </div>

              <!-- 错误信息（即使关闭思考链也显示） -->
              <div v-if="!showThinking && hasErrorSteps(msg.steps)" class="error-banner">
                <span v-for="(step, ei) in getErrorSteps(msg.steps)" :key="ei">
                  ⚠️ {{ step.content }}
                </span>
              </div>

              <!-- 最终回答 -->
              <div class="final-answer" v-html="renderMarkdown(msg.content)"></div>

              <!-- 追问快捷回复按钮 -->
              <div v-if="msg.clarify_suggestions && msg.clarify_suggestions.length" class="clarify-suggestions">
                <div class="clarify-label">快捷回复：</div>
                <div class="clarify-chips">
                  <span
                    v-for="(sug, si) in msg.clarify_suggestions"
                    :key="si"
                    class="clarify-chip"
                    @click="sendExample(sug)"
                  >{{ sug }}</span>
                </div>
              </div>

              <!-- 重新生成按钮 -->
              <div v-if="idx === messages.length - 1 && !streaming" class="regenerate-bar">
                <el-button size="small" text @click="regenerateMessage">
                  🔄 重新生成
                </el-button>
              </div>
            </div>
          </div>

          <!-- 流式进行中的步骤 -->
          <div v-else-if="msg.role === 'streaming'" :key="'s-' + idx" class="message assistant-message">
            <div class="message-avatar agent-avatar">{{ agent?.icon || '🤖' }}</div>
            <div class="message-content">
              <div class="streaming-steps" v-if="showThinking">
                <div
                  v-for="(step, si) in msg.steps"
                  :key="'ss-' + si"
                  class="chain-step"
                  :class="'step-' + step.type"
                >
                  <div class="step-header">
                    <span class="step-badge" :class="'badge-' + step.type">{{ stepLabel(step.type) }}</span>
                    <span class="step-iter" v-if="step.iteration">第 {{ step.iteration }} 轮</span>
                  </div>
                  <div class="step-body">
                    <template v-if="step.type === 'thought'">
                      <span class="step-text">{{ step.content }}</span>
                    </template>
                    <template v-else-if="step.type === 'action'">
                      <span class="step-tool">🔧 {{ step.tool }}</span>
                      <pre class="step-code">{{ JSON.stringify(step.input, null, 2) }}</pre>
                    </template>
                    <template v-else-if="step.type === 'observation'">
                      <pre class="step-obs">{{ step.content }}</pre>
                    </template>
                  </div>
                </div>
              </div>
              <!-- 正在流式输出的最终回答 -->
              <div v-if="msg.streamingAnswer" class="final-answer streaming-answer">
                <span v-html="renderMarkdown(msg.streamingAnswer)"></span>
                <span class="typing-cursor">○</span>
              </div>
              <!-- 工具调用中的状态指示器 -->
              <div v-else class="streaming-indicator">
                <span class="dot-pulse"><span></span><span></span><span></span></span>
                <span class="streaming-status">{{ streamingStatus }}</span>
              </div>
            </div>
          </div>
        </template>
      </div>

      <!-- 输入区 -->
      <div class="input-area">
        <el-input
          v-model="inputText"
          type="textarea"
          :rows="3"
          placeholder="输入问题，Agent 将自主思考并调用工具来回答..."
          :disabled="streaming"
          @keydown.enter.exact.prevent="sendMessage"
        />
        <div class="input-actions">
          <span class="input-hint">Enter 发送 · Shift+Enter 换行</span>
          <div class="input-buttons">
            <el-button
              v-if="streaming"
              type="danger"
              plain
              @click="stopGeneration"
            >
              ⏹ 停止生成
            </el-button>
            <el-button
              v-else
              type="primary"
              :disabled="!inputText.trim()"
              @click="sendMessage"
            >
              发送
            </el-button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Delete, ArrowLeft, View, Memo } from '@element-plus/icons-vue'
import { agentApi } from '@/api'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

const route = useRoute()
const router = useRouter()
const agentId = Number(route.params.id)

const agent = ref<any>(null)
const sessions = ref<any[]>([])
const currentSession = ref<any>(null)
const messages = ref<any[]>([])
const inputText = ref('')
const streaming = ref(false)
const streamingStatus = ref('思考中...')
const showThinking = ref(true)
const lastTraceSummary = ref<any>(null)
const messagesRef = ref<HTMLElement>()
let abortController: AbortController | null = null

const memoryDialogVisible = ref(false)
const memories = ref<any[]>([])
const memoriesLoading = ref(false)
const newMemoryContent = ref('')

watch(memoryDialogVisible, (val) => {
  if (val) loadMemories()
})

async function loadMemories() {
  memoriesLoading.value = true
  try {
    const res = await agentApi.listMemories({ agent_id: agentId })
    memories.value = res.items || []
  } finally {
    memoriesLoading.value = false
  }
}

async function addMemory() {
  if (!newMemoryContent.value.trim()) return
  try {
    await agentApi.addMemory({ content: newMemoryContent.value.trim(), agent_id: agentId, importance: 1.5, tags: 'manual' })
    newMemoryContent.value = ''
    ElMessage.success('记忆已添加')
    await loadMemories()
  } catch (e: any) {
    ElMessage.error('添加失败: ' + e.message)
  }
}

async function deleteMemory(id: number) {
  try {
    await agentApi.deleteMemory(id)
    memories.value = memories.value.filter(m => m.id !== id)
    ElMessage.success('已删除')
  } catch (e: any) {
    ElMessage.error('删除失败: ' + e.message)
  }
}

async function clearAllMemories() {
  try {
    await ElMessageBox.confirm('确认清空所有记忆？', '警告', { type: 'warning' })
    await agentApi.clearMemories({ agent_id: agentId })
    memories.value = []
    ElMessage.success('记忆已清空')
  } catch {}
}

const enabledTools = computed(() => {
  const tools = agent.value?.tools || []
  return tools.map((t: any) => typeof t === 'string' ? t : t.name).filter(Boolean)
})

const examples = computed(() => {
  const toolNames = enabledTools.value
  const exs: string[] = []
  if (toolNames.includes('datetime_tool')) exs.push('现在是几点？今天星期几？')
  if (toolNames.includes('calculator')) exs.push('计算 (1234 + 5678) * 0.15 等于多少？')
  if (toolNames.includes('knowledge_search')) exs.push('帮我查找知识库中关于产品的信息')
  if (toolNames.includes('http_request')) exs.push('调用 https://httpbin.org/get 获取数据')
  if (toolNames.includes('db_query')) exs.push('查询数据库中有多少个工作流？')
  if (exs.length === 0) exs.push('你好，介绍一下你自己', '你能做什么？')
  return exs.slice(0, 3)
})

function stepLabel(type: string): string {
  const map: Record<string, string> = {
    thought: '💭 思考',
    action: '⚡ 行动',
    observation: '👁 观察',
    final: '✅ 回答',
    error: '❌ 错误',
    verify: '🔍 校验',
    clarify: '❓ 追问',
  }
  return map[type] || type
}

function hasErrorSteps(steps: any[]): boolean {
  return Array.isArray(steps) && steps.some((step: any) => step?.type === 'error')
}

function getErrorSteps(steps: any[]): any[] {
  if (!Array.isArray(steps)) return []
  return steps.filter((step: any) => step?.type === 'error')
}

// 配置 marked v12：链接新标签页打开，图片/视频智能渲染，代码块复制按钮
marked.use({
  breaks: true,
  gfm: true,
  renderer: {
    code(code: string, lang: string | undefined) {
      const langClass = lang ? ` class="language-${lang}"` : ''
      const escaped = code.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      return `<div class="code-block-wrapper"><button class="copy-btn" type="button">复制</button><pre><code${langClass}>${escaped}</code></pre></div>`
    },
    link(href: string, title: string | null, text: string) {
      if (!href || !/^https?:\/\//i.test(href)) {
        return `<span class="md-link-warning">${text}（链接已拦截）</span>`
      }
      const titleAttr = title ? ` title="${title}"` : ''
      return `<a href="${href}" target="_blank" rel="noopener noreferrer"${titleAttr}>${text}</a>`
    },
    image(href: string, title: string | null, text: string) {
      const safeHref = href || ''
      if (!/^https?:\/\//i.test(safeHref)) {
        return `<span class="md-link-warning">[不安全媒体链接已拦截]</span>`
      }
      const isVideo    = /\.(mp4|webm|ogg)$/i.test(href)
      const isYoutube  = /youtube\.com\/watch|youtu\.be\//i.test(href)
      const isBilibili = /bilibili\.com\/video/i.test(href)
      if (isVideo) {
        return `<video controls style="max-width:100%;border-radius:8px;margin:8px 0" src="${href}"></video>`
      }
      if (isYoutube) {
        const vid = href.match(/(?:v=|youtu\.be\/)?([\w-]{11})/)?.[1]
        if (vid) return `<div class="video-embed"><iframe src="https://www.youtube.com/embed/${vid}" allowfullscreen frameborder="0"></iframe></div>`
      }
      if (isBilibili) {
        const bvid = href.match(/\/video\/(BV[\w]+)/i)?.[1]
        if (bvid) return `<div class="video-embed"><iframe src="https://player.bilibili.com/player.html?bvid=${bvid}&autoplay=0" allowfullscreen frameborder="0"></iframe></div>`
      }
      return `<img src="${href}" alt="${text}" title="${title || text}" class="md-img" />`
    },
  } as any,
})

function renderMarkdown(text: string): string {
  if (!text) return ''
  const raw = marked.parse(text) as string
  return DOMPurify.sanitize(raw, {
    ADD_TAGS: ['iframe', 'video'],
    ADD_ATTR: ['allowfullscreen', 'frameborder', 'controls', 'src', 'target', 'rel'],
    FORBID_ATTR: ['onclick', 'onerror', 'onload'],
  })
}

async function scrollToBottom() {
  await nextTick()
  if (messagesRef.value) {
    messagesRef.value.scrollTop = messagesRef.value.scrollHeight
  }
}

async function handleMarkdownAreaClick(evt: Event) {
  const target = evt.target as HTMLElement | null
  if (!target) return

  const copyBtn = target.closest('.copy-btn') as HTMLButtonElement | null
  if (copyBtn) {
    const codeEl = copyBtn.parentElement?.querySelector('code')
    const content = codeEl?.textContent || ''
    if (!content) return
    try {
      await navigator.clipboard.writeText(content)
      copyBtn.textContent = '已复制'
      setTimeout(() => { copyBtn.textContent = '复制' }, 1200)
    } catch {
      copyBtn.textContent = '失败'
      setTimeout(() => { copyBtn.textContent = '复制' }, 1200)
    }
    return
  }

  const img = target.closest('.md-img') as HTMLImageElement | null
  if (img && img.src) {
    window.open(img.src, '_blank', 'noopener,noreferrer')
  }
}

async function loadAgent() {
  try {
    agent.value = await agentApi.get(agentId)
  } catch {
    ElMessage.error('Agent 不存在')
    router.push('/agents')
  }
}

async function loadSessions() {
  try {
    const res = await agentApi.listSessions(agentId)
    sessions.value = res.items || []
  } catch { /* ignore */ }
}

async function newSession() {
  const s = await agentApi.createSession(agentId)
  sessions.value.unshift(s)
  currentSession.value = s
  messages.value = []
}

async function loadSession(s: any) {
  // 流式输出时禁止切换会话，防止数据串扰
  if (streaming.value) {
    ElMessage.warning('请等待当前回答完成后再切换会话')
    return
  }
  currentSession.value = s
  messages.value = []
  try {
    const res = await agentApi.getSessionMessages(agentId, s.id)
    const rawMessages: any[] = res.messages || []
    // 将后端存储的消息格式转为前端展示格式
    messages.value = rawMessages.map((m: any) => {
      if (m.role === 'user') {
        return { role: 'user', content: m.content }
      } else if (m.role === 'assistant') {
        return {
          role: 'assistant',
          content: m.content || '',
          steps: (m.steps || []).filter((s: any) => s.type !== 'done' && s.type !== 'final'),
          clarify_suggestions: m.clarify_suggestions || [],
        }
      }
      return m
    })
    await scrollToBottom()
  } catch { /* ignore */ }
}

async function deleteSession(s: any) {
  await ElMessageBox.confirm('确定删除此对话？', '确认', { type: 'warning' })
  await agentApi.deleteSession(agentId, s.id)
  sessions.value = sessions.value.filter(x => x.id !== s.id)
  if (currentSession.value?.id === s.id) {
    currentSession.value = null
    messages.value = []
  }
}

function sendExample(ex: string) {
  inputText.value = ex
  sendMessage()
}

async function sendMessage() {
  const text = inputText.value.trim()
  if (!text || streaming.value) return

  // 确保有会话
  if (!currentSession.value) {
    const s = await agentApi.createSession(agentId)
    currentSession.value = s
    sessions.value.unshift(s)
  }

  inputText.value = ''
  messages.value.push({ role: 'user', content: text })

  // 记住本次请求的会话ID，用于防止切换会话后数据串扰
  const sessionIdForThisRequest = currentSession.value.id

  // 添加流式占位消息（用 reactive index 更新以触发 Vue 响应式）
  const streamingIdx = messages.value.length
  messages.value.push({ role: 'streaming', steps: [] as any[], content: '', streamingAnswer: '' })
  streaming.value = true
  streamingStatus.value = '思考中...'
  await scrollToBottom()

  abortController = new AbortController()

  try {
    const response = await fetch(`/api/v1/agents/${agentId}/chat`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${localStorage.getItem('access_token')}`,
      },
      body: JSON.stringify({
        session_id: currentSession.value.id,
        message: text,
        stream: true,
      }),
      signal: abortController.signal,
    })

    if (!response.ok) throw new Error(`HTTP ${response.status}`)

    const reader = response.body!.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let finalContent = ''
    let outerDone = false
    let clarifySuggestions: string[] = []

    const parseSSEEvents = (rawBuffer: string) => {
      const events: any[] = []
      const blocks = rawBuffer.split('\n\n')
      const remain = blocks.pop() || ''

      for (const block of blocks) {
        const lines = block.split('\n').map(l => l.trimEnd())
        const dataLines = lines
          .filter(l => l.startsWith('data:'))
          .map(l => l.replace(/^data:\s?/, ''))

        if (!dataLines.length) continue
        const payload = dataLines.join('\n')
        try {
          events.push(JSON.parse(payload))
        } catch {
          // 忽略非 JSON 心跳
        }
      }

      return { events, remain }
    }

    while (!outerDone) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const parsedSSE = parseSSEEvents(buffer)
      buffer = parsedSSE.remain

      for (const event of parsedSSE.events) {
        try {

          if (event.type === 'done') { outerDone = true; break }

          const cur = messages.value[streamingIdx]

          if (event.type === 'thought') {
            streamingStatus.value = '💭 思考中...'
            messages.value[streamingIdx] = { ...cur, steps: [...cur.steps, event] }
          } else if (event.type === 'action') {
            streamingStatus.value = `⚡ 调用工具: ${event.tool}`
            messages.value[streamingIdx] = { ...cur, steps: [...cur.steps, event] }
          } else if (event.type === 'observation') {
            streamingStatus.value = '👁 获取结果...'
            messages.value[streamingIdx] = { ...cur, steps: [...cur.steps, event] }
          } else if (event.type === 'clarify') {
            streamingStatus.value = '❓ 追问中...'
            clarifySuggestions = event.suggestions || []
            messages.value[streamingIdx] = { ...cur, steps: [...cur.steps, event] }
          } else if (event.type === 'verify') {
            streamingStatus.value = '🔍 校验中...'
            messages.value[streamingIdx] = { ...cur, steps: [...cur.steps, event] }
          } else if (event.type === 'final_start') {
            streamingStatus.value = '✍️ 生成回答...'
            messages.value[streamingIdx] = { ...cur, streamingAnswer: '' }
          } else if (event.type === 'final_chunk') {
            messages.value[streamingIdx] = { ...cur, streamingAnswer: (cur.streamingAnswer || '') + event.content }
          } else if (event.type === 'final') {
            finalContent = event.content
            messages.value[streamingIdx] = { ...cur, steps: [...cur.steps, event] }
          } else if (event.type === 'error') {
            messages.value[streamingIdx] = { ...cur, steps: [...cur.steps, event] }
          } else if (event.type === 'trace') {
            lastTraceSummary.value = event.summary || null
          }

          await scrollToBottom()
        } catch { /* ignore parse errors */ }
      }
    }

    // 处理流结束时残留 buffer（某些代理层不会补齐空行）
    if (buffer.trim()) {
      const tailParsed = parseSSEEvents(`${buffer}\n\n`)
      for (const event of tailParsed.events) {
        if (event.type === 'final') {
          finalContent = event.content
        } else if (event.type === 'trace') {
          lastTraceSummary.value = event.summary || null
        }
      }
    }

    // 替换流式消息为最终消息（检查会话是否已切换）
    if (currentSession.value?.id === sessionIdForThisRequest) {
      const finalSteps = (messages.value[streamingIdx]?.steps || []).filter((s: any) => s.type !== 'final')
      messages.value[streamingIdx] = {
        role: 'assistant',
        content: finalContent,
        steps: finalSteps,
        clarify_suggestions: clarifySuggestions,
      }
    }

    // 更新会话列表（反映新的消息数）
    await loadSessions()

  } catch (e: any) {
    if (e.name === 'AbortError') {
      // 用户主动停止
      const cur = messages.value[streamingIdx]
      const partialContent = cur?.streamingAnswer || '…（已停止生成）'
      messages.value[streamingIdx] = {
        role: 'assistant',
        content: partialContent,
        steps: (cur?.steps || []).filter((s: any) => s.type !== 'final'),
      }
    } else {
      messages.value[streamingIdx] = {
        role: 'assistant',
        content: `执行出错: ${e.message}`,
        steps: [],
      }
      ElMessage.error('请求失败: ' + e.message)
    }
  } finally {
    streaming.value = false
    abortController = null
    await scrollToBottom()
  }
}

function stopGeneration() {
  if (abortController) {
    abortController.abort()
  }
}

async function regenerateMessage() {
  if (streaming.value || messages.value.length < 2) return
  // 找到最后一条 user 消息
  const lastAssistantIdx = messages.value.length - 1
  const lastUserIdx = lastAssistantIdx - 1
  if (messages.value[lastUserIdx]?.role !== 'user') return
  const lastUserText = messages.value[lastUserIdx].content
  // 移除最后一组问答
  messages.value.splice(lastUserIdx, 2)
  // 重新发送
  inputText.value = lastUserText
  await nextTick()
  sendMessage()
}

onMounted(async () => {
  messagesRef.value?.addEventListener('click', handleMarkdownAreaClick)
  await loadAgent()
  await loadSessions()
  if (sessions.value.length === 0) {
    await newSession()
  } else {
    // 自动加载最新会话的消息（修复 F6: 之前只设置了 currentSession 但没调用 loadSession）
    await loadSession(sessions.value[0])
  }
  await loadMemories()
})

onUnmounted(() => {
  messagesRef.value?.removeEventListener('click', handleMarkdownAreaClick)
})
</script>

<style scoped>
/* ── OpenAI 亮色风格 ── */
.agent-chat-page {
  display: flex;
  height: calc(100vh - 56px);
  background: #fff;
}

/* 左侧会话面板 */
.session-panel {
  width: 240px;
  background: #f9f9f9;
  border-right: 1px solid #e5e5e5;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}
.session-header {
  padding: 14px 16px 10px;
  display: flex;
  align-items: center;
  gap: 10px;
  border-bottom: 1px solid #e5e5e5;
}
.agent-icon { font-size: 22px; }
.agent-name { font-weight: 600; font-size: 14px; color: #0d0d0d; }
.agent-tools-count { font-size: 11px; color: #9ca3af; }

.new-session-btn {
  margin: 8px 10px;
  width: calc(100% - 20px);
  background: #fff !important;
  border: 1px solid #e5e5e5 !important;
  color: #374151 !important;
  border-radius: 8px;
  font-size: 13px;
  height: 34px;
  transition: all 0.15s;
}
.new-session-btn:hover {
  background: #f3f4f6 !important;
  border-color: #d1d5db !important;
}

.session-list { flex: 1; overflow-y: auto; padding: 4px 8px; }
.session-list::-webkit-scrollbar { width: 3px; }
.session-list::-webkit-scrollbar-thumb { background: #e5e5e5; border-radius: 2px; }

.session-item {
  padding: 9px 12px;
  border-radius: 8px;
  cursor: pointer;
  position: relative;
  margin-bottom: 2px;
  transition: background 0.12s;
}
.session-item:hover { background: #f3f4f6; }
.session-item.active { background: #efefef; }
.session-title { font-size: 13px; color: #0d0d0d; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.session-meta { font-size: 11px; color: #9ca3af; margin-top: 2px; }
.session-del {
  position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
  opacity: 0; color: #9ca3af; cursor: pointer; transition: color 0.15s;
}
.session-del:hover { color: #ef4444; }
.session-item:hover .session-del { opacity: 1; }

.panel-footer { padding: 10px 12px; border-top: 1px solid #e5e5e5; }
.panel-footer :deep(.el-button) { color: #9ca3af !important; font-size: 12px; }
.panel-footer :deep(.el-button:hover) { color: #374151 !important; }

/* 右侧对话区 */
.chat-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: #fff;
}
.chat-toolbar {
  height: 50px;
  background: #fff;
  border-bottom: 1px solid #e5e5e5;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
  flex-shrink: 0;
}
.toolbar-title { font-weight: 600; font-size: 15px; color: #0d0d0d; margin-right: 10px; }
.tool-tag {
  margin-right: 4px;
  background: #f3f4f6 !important;
  border-color: #e5e5e5 !important;
  color: #6b7280 !important;
  font-size: 11px;
}

.messages-container {
  flex: 1;
  overflow-y: auto;
  padding: 20px 0 8px;
  display: flex;
  flex-direction: column;
  background: #fff;
}
.messages-container::-webkit-scrollbar { width: 5px; }
.messages-container::-webkit-scrollbar-thumb { background: #e5e5e5; border-radius: 3px; }

/* 欢迎区 */
.welcome-area {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 60px 20px 40px;
  text-align: center;
  max-width: 640px;
  margin: 0 auto;
  width: 100%;
}
.welcome-icon { font-size: 44px; margin-bottom: 14px; }
.welcome-title { font-size: 24px; font-weight: 600; color: #0d0d0d; margin-bottom: 8px; }
.welcome-desc { font-size: 14px; color: #6b7280; max-width: 440px; margin-bottom: 28px; line-height: 1.6; }
.welcome-tools { margin-bottom: 20px; }
.tools-label, .examples-label {
  font-size: 11px; color: #9ca3af; margin-bottom: 10px;
  text-transform: uppercase; letter-spacing: 0.5px;
}
.tools-chips { display: flex; gap: 6px; flex-wrap: wrap; justify-content: center; }
.welcome-examples { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; }
.example-chip {
  padding: 10px 16px;
  background: #fff;
  border: 1px solid #e5e5e5;
  border-radius: 10px;
  font-size: 13px;
  color: #374151;
  cursor: pointer;
  transition: all 0.15s;
  max-width: 240px;
  text-align: left;
  line-height: 1.4;
  box-shadow: 0 1px 2px rgba(0,0,0,0.04);
}
.example-chip:hover { background: #f9f9f9; border-color: #10a37f; color: #10a37f; }

/* 消息行 */
.message { display: flex; align-items: flex-start; width: 100%; }

/* 用户消息：右对齐气泡 */
.user-message { justify-content: flex-end; padding: 4px 24px; }
.user-message .message-bubble {
  max-width: 68%;
  background: #f3f4f6;
  color: #0d0d0d;
  padding: 11px 16px;
  border-radius: 18px 18px 4px 18px;
  font-size: 14px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
}

/* 头像隐藏 */
.message-avatar { display: none; }
.user-avatar, .agent-avatar { display: none; }

/* Assistant 消息：居中宽内容 */
.assistant-message {
  flex-direction: column;
  padding: 4px 24px;
  max-width: 800px;
  margin: 0 auto;
  width: 100%;
  box-sizing: border-box;
}
.message-content { width: 100%; }

/* ReAct 思考链 */
.react-chain { margin-bottom: 10px; }
.chain-title { display: flex; align-items: center; gap: 6px; font-size: 12px; color: #9ca3af; }
.chain-steps { display: flex; flex-direction: column; gap: 5px; padding: 4px 0; }
.chain-step { border-radius: 6px; padding: 8px 12px; border-left: 2px solid #e5e5e5; font-size: 12px; }
.step-thought    { background: #fffbf0; border-color: #f59e0b; }
.step-action     { background: #f0f7ff; border-color: #3b82f6; }
.step-observation{ background: #f0fdf4; border-color: #22c55e; }
.step-error      { background: #fff5f5; border-color: #ef4444; }
.step-final      { background: #f0fdf4; border-color: #10a37f; }
.step-header { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
.step-badge { font-size: 11px; padding: 1px 7px; border-radius: 8px; font-weight: 500; }
.badge-thought    { background: #fef3c7; color: #92400e; }
.badge-action     { background: #dbeafe; color: #1d4ed8; }
.badge-observation{ background: #dcfce7; color: #166534; }
.badge-error      { background: #fee2e2; color: #991b1b; }
.badge-final      { background: #d1fae5; color: #065f46; }
.step-iter { font-size: 11px; color: #d1d5db; }
.step-text { color: #6b7280; line-height: 1.5; }
.step-tool { font-weight: 500; color: #3b82f6; display: block; margin-bottom: 4px; }
.step-code { background: #f9f9f9; color: #374151; padding: 6px 10px; border-radius: 4px; overflow-x: auto; margin: 0; border: 1px solid #e5e5e5; }
.step-obs  { background: #f9f9f9; color: #6b7280; padding: 6px 10px; border-radius: 4px; overflow-x: auto; margin: 0; max-height: 150px; overflow-y: auto; border: 1px solid #e5e5e5; }
.step-error { color: #ef4444; }

/* 最终回答 */
.final-answer {
  background: transparent;
  font-size: 15px;
  line-height: 1.75;
  color: #0d0d0d;
}
.final-answer :deep(h1) { font-size: 20px; font-weight: 600; color: #0d0d0d; margin: 16px 0 8px; }
.final-answer :deep(h2) { font-size: 17px; font-weight: 600; color: #0d0d0d; margin: 14px 0 6px; }
.final-answer :deep(h3) { font-size: 15px; font-weight: 600; color: #0d0d0d; margin: 12px 0 4px; }
.final-answer :deep(p) { margin: 8px 0; }
.final-answer :deep(strong) { color: #0d0d0d; font-weight: 600; }
.final-answer :deep(em) { color: #374151; }
.final-answer :deep(code) {
  background: #f3f4f6; color: #c7254e;
  padding: 2px 6px; border-radius: 4px; font-family: 'Consolas', monospace; font-size: 13px;
}
.final-answer :deep(.code-block-wrapper) {
  position: relative;
  margin: 12px 0;
}
.final-answer :deep(.copy-btn) {
  position: absolute;
  top: 8px;
  right: 10px;
  background: rgba(255,255,255,0.12);
  color: #d4d4d4;
  border: 1px solid rgba(255,255,255,0.2);
  border-radius: 4px;
  padding: 2px 10px;
  font-size: 12px;
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
  z-index: 1;
  line-height: 1.6;
}
.final-answer :deep(.copy-btn:hover) {
  background: rgba(255,255,255,0.22);
  color: #fff;
}
.final-answer :deep(pre) {
  background: #1e1e1e; border-radius: 8px;
  padding: 14px 16px; overflow-x: auto; margin: 0; font-size: 13px;
}
.final-answer :deep(pre code) { background: none; padding: 0; color: #d4d4d4; font-family: 'Consolas', 'Monaco', monospace; }
.final-answer :deep(ul), .final-answer :deep(ol) { padding-left: 22px; margin: 8px 0; }
.final-answer :deep(li) { margin: 4px 0; color: #0d0d0d; }
.final-answer :deep(blockquote) {
  border-left: 3px solid #10a37f; margin: 10px 0; padding: 6px 14px;
  color: #6b7280; background: #f0fdf9; border-radius: 0 6px 6px 0;
}
.final-answer :deep(table) {
  border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 13px;
  border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden;
}
.final-answer :deep(th), .final-answer :deep(td) {
  border: 1px solid #e2e8f0; padding: 10px 14px; color: #0d0d0d;
  text-align: left;
}
.final-answer :deep(th) {
  background: #f1f5f9; font-weight: 600; color: #334155;
  border-bottom: 2px solid #cbd5e1;
}
.final-answer :deep(tr:nth-child(even)) { background: #f8fafc; }
.final-answer :deep(tr:hover) { background: #f0fdf9; transition: background 0.15s; }
.final-answer :deep(a) { color: #10a37f; text-decoration: none; word-break: break-all; }
.final-answer :deep(a:hover) { text-decoration: underline; }
.final-answer :deep(img) {
  max-width: 100%; border-radius: 8px; margin: 8px 0; display: block;
  box-shadow: 0 2px 12px rgba(0,0,0,0.1); cursor: pointer;
}
.final-answer :deep(.img-broken) {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 120px; min-height: 80px; max-width: 100%;
  background: #f3f4f6; border: 1px dashed #d1d5db; border-radius: 8px;
  color: #9ca3af; font-size: 12px; padding: 12px 16px;
  cursor: default; box-shadow: none;
  content: attr(alt);
}
.final-answer :deep(.video-embed) {
  position: relative; padding-bottom: 56.25%; height: 0;
  margin: 12px 0; border-radius: 10px; overflow: hidden; border: 1px solid #e5e5e5;
}
.final-answer :deep(.video-embed iframe) { position: absolute; top: 0; left: 0; width: 100%; height: 100%; border: none; }
.final-answer :deep(video) { max-width: 100%; border-radius: 8px; margin: 8px 0; display: block; }
.final-answer :deep(hr) { border: none; border-top: 1px solid #e5e5e5; margin: 14px 0; }

/* 错误横幅（关闭思考链时也可见） */
.error-banner {
  display: flex; flex-direction: column; gap: 4px;
  padding: 8px 12px; margin-bottom: 8px;
  background: #fff5f5; border: 1px solid #fed7d7; border-radius: 8px;
  font-size: 13px; color: #c53030;
}

/* 追问快捷回复 */
.clarify-suggestions { margin-top: 12px; }
.clarify-label { font-size: 11px; color: #9ca3af; margin-bottom: 6px; }
.clarify-chips { display: flex; flex-wrap: wrap; gap: 8px; }
.clarify-chip {
  padding: 6px 14px; background: #f0fdf9; border: 1px solid #86efac;
  border-radius: 16px; font-size: 13px; color: #065f46;
  cursor: pointer; transition: all 0.15s;
}
.clarify-chip:hover {
  background: #d1fae5; border-color: #10a37f; color: #047857;
}

/* 重新生成按钮 */
.regenerate-bar {
  margin-top: 8px; padding-top: 4px;
}
.regenerate-bar :deep(.el-button) {
  color: #9ca3af !important; font-size: 12px;
}
.regenerate-bar :deep(.el-button:hover) {
  color: #10a37f !important;
}

/* 流式状态 */
.streaming-steps { margin-bottom: 8px; }
.streaming-indicator {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 0; font-size: 13px; color: #9ca3af;
}
.dot-pulse { display: flex; gap: 5px; align-items: center; }
.dot-pulse span {
  width: 5px; height: 5px; border-radius: 50%; background: #d1d5db;
  animation: dotBounce 1.4s infinite ease-in-out;
}
.dot-pulse span:nth-child(2) { animation-delay: 0.2s; }
.dot-pulse span:nth-child(3) { animation-delay: 0.4s; }
@keyframes dotBounce { 0%, 80%, 100% { opacity: 0.3; transform: scale(0.8); } 40% { opacity: 1; transform: scale(1); } }

.typing-cursor {
  display: inline-block; width: 2px; height: 1.1em;
  background: #9ca3af; margin-left: 1px;
  vertical-align: text-bottom; animation: blink 0.8s infinite;
}
@keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }
.streaming-answer { position: relative; }

/* 输入区 */
.input-area {
  padding: 12px 24px 16px;
  background: #fff;
  border-top: 1px solid #e5e5e5;
  flex-shrink: 0;
}
.input-area :deep(.el-textarea__inner) {
  background: #fff !important;
  border: 1px solid #d1d5db !important;
  border-radius: 12px !important;
  color: #0d0d0d !important;
  font-size: 14px !important;
  padding: 12px 16px !important;
  resize: none !important;
  box-shadow: 0 1px 3px rgba(0,0,0,0.05) !important;
  transition: border-color 0.15s, box-shadow 0.15s !important;
}
.input-area :deep(.el-textarea__inner:focus) {
  border-color: #10a37f !important;
  box-shadow: 0 0 0 3px rgba(16,163,127,0.1) !important;
}
.input-area :deep(.el-textarea__inner)::placeholder { color: #9ca3af !important; }
.input-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 8px;
}
.input-hint { font-size: 12px; color: #d1d5db; }
.input-buttons { display: flex; gap: 8px; }
.input-actions :deep(.el-button--primary) {
  background: #10a37f !important;
  border-color: #10a37f !important;
  color: #fff !important;
  border-radius: 8px !important;
  font-weight: 500;
  padding: 8px 18px;
}
.input-actions :deep(.el-button--primary:hover) { background: #0d8f6f !important; border-color: #0d8f6f !important; }
.input-actions :deep(.el-button--primary.is-disabled) {
  background: #e5e5e5 !important;
  border-color: #e5e5e5 !important;
  color: #9ca3af !important;
}
.input-actions :deep(.el-button--danger) {
  border-radius: 8px !important;
  font-weight: 500;
  padding: 8px 18px;
}
</style>
