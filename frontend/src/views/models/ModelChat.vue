<template>
  <div class="chat-page">
    <!-- Config Panel -->
    <div class="chat-config">
      <el-card shadow="never">
        <template #header><span class="card-title">对话配置</span></template>
        <el-form label-width="80px" size="small">
          <el-form-item label="选择模型">
            <el-select v-model="selectedModelId" placeholder="请选择模型" style="width:100%">
              <el-option-group
                v-for="p in providersWithModels"
                :key="p.id"
                :label="p.display_name"
              >
                <el-option
                  v-for="m in chatModels.filter(c => c.provider_id === p.id)"
                  :key="m.id"
                  :label="m.display_name"
                  :value="m.id"
                />
              </el-option-group>
            </el-select>
          </el-form-item>
          <el-form-item label="知识库">
            <el-select v-model="selectedKbId" placeholder="不使用知识库" clearable style="width:100%">
              <el-option v-for="kb in knowledgeBases" :key="kb.id" :label="kb.name" :value="kb.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="提示词模板">
            <el-select v-model="selectedTemplateId" clearable placeholder="从模板库选择" style="width:100%" @change="applyTemplate">
              <el-option v-for="t in promptTemplates" :key="t.id" :label="t.name" :value="t.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="系统提示">
            <el-input v-model="systemPrompt" type="textarea" :rows="3" placeholder="设置 AI 角色和行为..." />
          </el-form-item>
          <el-form-item label="温度">
            <el-slider v-model="temperature" :min="0" :max="2" :step="0.1" show-input input-size="small" />
          </el-form-item>
          <el-button size="small" @click="clearMessages" style="width:100%">清空对话</el-button>
        </el-form>
      </el-card>
    </div>

    <!-- Chat Area -->
    <div class="chat-main">
      <el-card shadow="never" class="chat-card">
        <div class="messages-container" ref="messagesRef">
          <div v-if="messages.length === 0" class="empty-chat">
            <el-icon size="48" color="#c0c4cc"><ChatDotRound /></el-icon>
            <p>选择模型，开始对话</p>
          </div>
          <div v-for="(msg, idx) in messages" :key="idx" class="message-item" :class="msg.role">
            <div class="message-avatar">
              <el-avatar :size="32" :style="{ background: msg.role === 'user' ? '#409eff' : '#67c23a' }">
                {{ msg.role === 'user' ? 'U' : 'AI' }}
              </el-avatar>
            </div>
            <div class="message-content">
              <div class="message-role">{{ msg.role === 'user' ? '我' : 'AI 助手' }}</div>
              <div class="message-body markdown-body" v-html="renderMarkdown(msg.content)" />
            </div>
          </div>
          <div v-if="streaming" class="message-item assistant">
            <div class="message-avatar">
              <el-avatar :size="32" style="background:#67c23a">AI</el-avatar>
            </div>
            <div class="message-content">
              <div class="message-role">AI 助手</div>
              <div class="message-body markdown-body streaming-body" v-html="renderStreamingMarkdown(streamingContent)" />
              <span class="typing-cursor">○</span>
            </div>
          </div>
        </div>

        <div class="input-area">
          <!-- 图片预览区 -->
          <div v-if="pendingImages.length > 0" class="image-preview-bar">
            <div v-for="(img, i) in pendingImages" :key="i" class="preview-item">
              <img :src="img.url" class="preview-thumb" />
              <el-icon class="preview-remove" @click="removeImage(i)"><Close /></el-icon>
            </div>
          </div>
          <el-input
            v-model="inputText"
            type="textarea"
            :rows="3"
            placeholder="输入消息，Ctrl+Enter 发送..."
            @keydown.ctrl.enter="sendMessage"
            resize="none"
          />
          <div class="input-actions">
            <div class="input-left-actions">
              <!-- <input ref="imageInputRef" type="file" accept="image/*" style="display:none" @change="handleImageUpload" />
              <el-button size="small" :icon="Picture" @click="imageInputRef?.click()" title="上传图片（多模态）">图片</el-button> -->
            </div>
            <div class="input-right-actions">
              <span class="hint">Ctrl+Enter 发送</span>
              <el-button type="primary" :loading="streaming" @click="sendMessage" :disabled="!selectedModelId">
                发送
              </el-button>
            </div>
          </div>
        </div>
      </el-card>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { Picture, Close } from '@element-plus/icons-vue'
import { marked } from 'marked'
import { modelApi, knowledgeApi, promptApi } from '@/api'
import type { ModelProvider, ModelConfig, KnowledgeBase, ChatMessage } from '@/types'

const providers = ref<ModelProvider[]>([])
const allConfigs = ref<ModelConfig[]>([])
const knowledgeBases = ref<KnowledgeBase[]>([])

const selectedModelId = ref<number | null>(null)
const selectedKbId = ref<number | null>(null)
const systemPrompt = ref('')
const promptTemplates = ref<any[]>([])
const selectedTemplateId = ref<number | null>(null)
const temperature = ref(0.7)
const inputText = ref('')
const messages = ref<ChatMessage[]>([])
const streaming = ref(false)
const streamingContent = ref('')
const messagesRef = ref<HTMLElement>()
const imageInputRef = ref<HTMLInputElement>()
const pendingImages = ref<{ url: string; apiUrl: string }[]>([])
const uploadingImage = ref(false)

const chatModels = computed(() => allConfigs.value.filter(c => c.model_type === 'chat' && c.is_active))
const providersWithModels = computed(() =>
  providers.value.filter(p => chatModels.value.some(m => m.provider_id === p.id))
)

marked.use({
  breaks: true,
  gfm: true,
  renderer: {
    code(code: string, lang: string | undefined) {
      const langClass = lang ? ` class="language-${lang}"` : ''
      const escaped = code.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      return `<div class="code-block-wrapper"><button class="copy-btn" onclick="(function(btn){var code=btn.nextElementSibling.querySelector('code');navigator.clipboard.writeText(code.innerText).then(function(){btn.textContent='已复制';setTimeout(function(){btn.textContent='复制'},1500)}).catch(function(){btn.textContent='失败'});})(this)">复制</button><pre><code${langClass}>${escaped}</code></pre></div>`
    },
  } as any,
})

function renderMarkdown(text: string): string {
  if (!text) return ''
  return marked(text) as string
}

// 流式渲染：将已接收的文本按段落分割，完整段落渲染 Markdown，末尾不完整行保持纯文本
function renderStreamingMarkdown(text: string): string {
  if (!text) return ''
  // 按双换行分段，最后一段可能不完整，保持纯文本
  const parts = text.split(/\n\n/)
  if (parts.length <= 1) {
    // 单段：直接转义显示，避免未闭合 Markdown 语法乱码
    return `<p style="white-space:pre-wrap">${text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}</p>`
  }
  // 完整段落渲染 Markdown，最后一段纯文本
  const completeParts = parts.slice(0, -1).join('\n\n')
  const lastPart = parts[parts.length - 1]
  const renderedComplete = marked(completeParts) as string
  const escapedLast = lastPart.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  return renderedComplete + `<p style="white-space:pre-wrap">${escapedLast}</p>`
}

async function scrollToBottom() {
  await nextTick()
  if (messagesRef.value) {
    messagesRef.value.scrollTop = messagesRef.value.scrollHeight
  }
}

function clearMessages() {
  messages.value = []
  streamingContent.value = ''
}

function applyTemplate(id: number | null) {
  if (!id) return
  const tpl = promptTemplates.value.find(t => t.id === id)
  if (tpl) {
    systemPrompt.value = tpl.content
    ElMessage.success(`已应用模板「${tpl.name}」`)
  }
}

async function handleImageUpload(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  uploadingImage.value = true
  try {
    const base64 = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(reader.result as string)
      reader.onerror = reject
      reader.readAsDataURL(file)
    })
    pendingImages.value.push({ url: base64, apiUrl: base64 })
    ElMessage.success('图片已添加')
  } catch (e: any) {
    ElMessage.error('图片读取失败: ' + e.message)
  } finally {
    uploadingImage.value = false
    if (imageInputRef.value) imageInputRef.value.value = ''
  }
}

function removeImage(index: number) {
  pendingImages.value.splice(index, 1)
}

async function sendMessage() {
  if (!inputText.value.trim() && pendingImages.value.length === 0) return
  if (!selectedModelId.value) {
    ElMessage.warning('请先选择模型')
    return
  }

  const imageUrls = pendingImages.value.map(img => img.apiUrl)
  const userMsg: ChatMessage = { role: 'user', content: inputText.value.trim() || '[图片]', timestamp: Date.now() }
  messages.value.push(userMsg)
  inputText.value = ''
  pendingImages.value = []
  await scrollToBottom()

  streaming.value = true
  streamingContent.value = ''

  try {
    const payload: any = {
      model_config_id: selectedModelId.value,
      messages: messages.value.map((m, idx) => {
        const msg: any = { role: m.role, content: m.content }
        // 最后一条用户消息附带图片
        if (idx === messages.value.length - 1 && imageUrls.length > 0) {
          msg.image_urls = imageUrls
        }
        return msg
      }),
      temperature: temperature.value,
      stream: true,
    }
    if (systemPrompt.value) payload.system_prompt = systemPrompt.value
    if (selectedKbId.value) payload.knowledge_base_id = selectedKbId.value

    const response = await fetch('/api/v1/models/chat', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream',
        'Cache-Control': 'no-cache',
        Authorization: `Bearer ${localStorage.getItem('access_token')}`,
      },
      body: JSON.stringify(payload),
    })

    if (!response.ok) {
      const errText = await response.text()
      throw new Error(`HTTP ${response.status}: ${errText}`)
    }

    const reader = response.body!.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let done = false

    while (!done) {
      const { done: readerDone, value } = await reader.read()
      done = readerDone
      if (value) buffer += decoder.decode(value, { stream: true })

      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''

      for (const line of lines) {
        const trimmed = line.trim()
        if (!trimmed.startsWith('data: ')) continue
        const data = trimmed.slice(6)
        if (data === '[DONE]') { done = true; break }
        try {
          const parsed = JSON.parse(data)
          if (parsed.type === 'content' && parsed.content) {
            streamingContent.value += parsed.content
            await scrollToBottom()
          } else if (parsed.type === 'error') {
            ElMessage.error('模型返回错误: ' + parsed.content)
            done = true
            break
          }
        } catch {}
      }
    }

    messages.value.push({
      role: 'assistant',
      content: streamingContent.value,
      timestamp: Date.now(),
    })
    streamingContent.value = ''
  } catch (e: any) {
    ElMessage.error('对话失败: ' + e.message)
  } finally {
    streaming.value = false
    await scrollToBottom()
  }
}

onMounted(async () => {
  const [ps, cs, kbs, tpls] = await Promise.all([
    modelApi.listProviders(),
    modelApi.listConfigs(),
    knowledgeApi.listBases({ page_size: 100 }),
    promptApi.list({ page_size: 100, include_public: true }),
  ])
  providers.value = ps
  allConfigs.value = cs
  knowledgeBases.value = kbs.items
  promptTemplates.value = tpls.items || []
  if (chatModels.value.length > 0) {
    selectedModelId.value = chatModels.value[0].id
  }
})
</script>

<style scoped>
.chat-page {
  display: flex;
  gap: 0;
  height: calc(100vh - 56px);
  background: #fff;
  padding: 0;
}

/* 左侧配置面板 */
.chat-config {
  width: 350px;
  flex-shrink: 0;
  overflow-y: auto;
  background: #f9f9f9;
  border-right: 1px solid #e5e5e5;
  padding: 12px 14px;
}
.chat-config::-webkit-scrollbar { width: 3px; }
.chat-config::-webkit-scrollbar-thumb { background: #e5e5e5; border-radius: 2px; }
.chat-config :deep(.el-card) {
  border: none !important;
  box-shadow: none !important;
  background: transparent !important;
}
.chat-config :deep(.el-card__header) {
  padding: 0 0 12px;
  border-bottom: 1px solid #e5e5e5;
  margin-bottom: 12px;
}
.card-title { font-weight: 600; font-size: 14px; color: #0d0d0d; }
.chat-config :deep(.el-form-item__label) { color: #374151; font-size: 13px; }
.chat-config :deep(.el-form-item) { margin-bottom: 16px; }
.chat-config :deep(.el-slider) { padding: 0 4px; }
.chat-config :deep(.el-slider__input) { width: 90px !important; }
.chat-config :deep(.el-input__inner),
.chat-config :deep(.el-textarea__inner),
.chat-config :deep(.el-select .el-input__inner) {
  border-color: #e5e5e5 !important;
  border-radius: 8px !important;
  font-size: 13px !important;
}
.chat-config :deep(.el-input__inner:focus),
.chat-config :deep(.el-textarea__inner:focus) {
  border-color: #10a37f !important;
}
.chat-config :deep(.el-button) {
  border-radius: 8px !important;
  font-size: 13px !important;
}

/* 右侧对话区 */
.chat-main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.chat-card {
  height: 100%;
  display: flex;
  flex-direction: column;
  border: none !important;
  box-shadow: none !important;
  border-radius: 0 !important;
}
.chat-card :deep(.el-card__body) {
  flex: 1; display: flex; flex-direction: column; padding: 0; overflow: hidden;
}

.messages-container {
  flex: 1; overflow-y: auto; padding: 20px 0;
  display: flex; flex-direction: column; gap: 4px;
}
.messages-container::-webkit-scrollbar { width: 5px; }
.messages-container::-webkit-scrollbar-thumb { background: #e5e5e5; border-radius: 3px; }

.empty-chat {
  flex: 1; display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  color: #9ca3af; gap: 12px;
}

/* 消息条目 */
.message-item { display: flex; gap: 12px; padding: 4px 24px; }
.message-item.user { flex-direction: row-reverse; }
.message-content { max-width: 72%; }
.message-item.user .message-content { align-items: flex-end; display: flex; flex-direction: column; }
.message-role { font-size: 11px; color: #9ca3af; margin-bottom: 4px; }
.message-body {
  background: #f3f4f6;
  border-radius: 16px 16px 4px 16px;
  padding: 11px 16px;
  font-size: 14px; line-height: 1.7; word-break: break-word;
  color: #0d0d0d;
}
.message-item.user .message-body {
  background: #f3f4f6;
  border-radius: 16px 16px 4px 16px;
}
.streaming-body { border-radius: 16px 16px 4px 16px; }

.typing-cursor {
  display: inline-block; width: 2px; height: 1em;
  background: #9ca3af; margin-left: 1px;
  vertical-align: text-bottom; animation: blink 0.8s infinite;
}
@keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }

/* 输入区 */
.input-area { border-top: 1px solid #e5e5e5; padding: 12px 20px 16px; background: #fff; }
.input-area :deep(.el-textarea__inner) {
  border-color: #d1d5db !important;
  border-radius: 10px !important;
  font-size: 14px !important;
  resize: none !important;
  box-shadow: 0 1px 3px rgba(0,0,0,0.05) !important;
}
.input-area :deep(.el-textarea__inner:focus) {
  border-color: #10a37f !important;
  box-shadow: 0 0 0 3px rgba(16,163,127,0.1) !important;
}
.input-actions { display: flex; justify-content: space-between; align-items: center; margin-top: 8px; }
.input-left-actions { display: flex; gap: 6px; }
.input-right-actions { display: flex; align-items: center; gap: 10px; }
.hint { font-size: 12px; color: #d1d5db; }

/* 图片预览条 */
.image-preview-bar {
  display: flex; gap: 8px; flex-wrap: wrap;
  padding: 8px 0 6px; border-bottom: 1px solid #f3f4f6; margin-bottom: 8px;
}
.preview-item { position: relative; display: inline-block; }
.preview-thumb {
  width: 60px; height: 60px; object-fit: cover;
  border-radius: 8px; border: 1px solid #e5e5e5; display: block;
}
.preview-remove {
  position: absolute; top: -6px; right: -6px;
  width: 18px; height: 18px; border-radius: 50%;
  background: #ef4444; color: #fff; font-size: 10px;
  display: flex; align-items: center; justify-content: center;
  cursor: pointer; box-shadow: 0 1px 3px rgba(0,0,0,0.2);
}
.input-actions :deep(.el-button--primary) {
  background: #10a37f !important;
  border-color: #10a37f !important;
  border-radius: 8px !important;
}
.input-actions :deep(.el-button--primary:hover) {
  background: #0d8f6f !important;
  border-color: #0d8f6f !important;
}

/* 代码块复制按钮 */
.message-body :deep(.code-block-wrapper) {
  position: relative;
  margin: 10px 0;
}
.message-body :deep(.copy-btn) {
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
.message-body :deep(.copy-btn:hover) {
  background: rgba(255,255,255,0.22);
  color: #fff;
}
.message-body :deep(pre) {
  background: #1e1e1e;
  border-radius: 8px;
  padding: 14px 16px;
  overflow-x: auto;
  margin: 0;
  font-size: 13px;
}
.message-body :deep(pre code) {
  background: none;
  padding: 0;
  color: #d4d4d4;
  font-family: 'Consolas', 'Monaco', monospace;
}
</style>
