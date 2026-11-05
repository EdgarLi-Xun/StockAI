<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { Check, Mail } from 'lucide-vue-next'
import { api } from '../api'

interface Provider { id: string, label: string, host: string, port: number, starttls: boolean, note: string }
const busy = ref(false), error = ref(''), notice = ref(''), loaded = ref(false)
const password = ref(''), providers = ref<Provider[]>([])
const saved = ref(false), source = ref('none'), configured = ref(false), passwordSet = ref(false), passwordHint = ref('')
const form = reactive({ provider: 'custom', host: '', port: 465, starttls: false, username: '', sender: '', enabled: false })
const snapshot = () => JSON.stringify({ provider: form.provider, host: form.host, port: form.port, starttls: form.starttls, username: form.username, sender: form.sender, enabled: form.enabled })
const baseline = ref('')
const dirty = computed(() => loaded.value && (password.value.length > 0 || snapshot() !== baseline.value))
const selected = computed(() => providers.value.find(item => item.id === form.provider))
const statusKind = computed(() => configured.value && form.enabled ? 'green' : 'amber')
const statusLabel = computed(() => {
  if (configured.value && form.enabled) return '正在发信'
  if (saved.value && !form.enabled) return '已保存，未启用'
  if (!saved.value && source.value === 'environment') return '使用环境变量'
  return '尚未配置'
})
const statusText = computed(() => {
  if (saved.value && !form.enabled) return '已保存，但邮件发送未启用。不会改用部署环境中的 SMTP。'
  if (!saved.value && source.value === 'environment') return '当前发信使用部署环境变量。保存本页后改为使用这里的账号，授权码需要重新填写。'
  if (configured.value) return '已启用。验证邮件和条件提醒会使用这组账号。'
  return '尚未配置。验证邮件和条件提醒不会发出。'
})
function apply(value: any) {
  providers.value = value.providers
  form.provider = value.provider
  form.host = value.host
  form.port = value.port
  form.starttls = value.starttls
  form.username = value.username
  form.sender = value.sender
  form.enabled = value.enabled
  saved.value = value.saved
  source.value = value.source
  configured.value = value.configured
  passwordSet.value = value.password_set
  passwordHint.value = value.password_hint || ''
  password.value = ''
  baseline.value = snapshot()
}
function applyProvider() {
  const preset = selected.value
  if (!preset?.host) return
  form.host = preset.host
  form.port = preset.port
  form.starttls = preset.starttls
}
async function load() {
  apply(await api('/admin/mail'))
  loaded.value = true
}
async function save() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    apply(await api('/admin/mail', { method: 'PUT', body: JSON.stringify({ ...form, password: password.value }) }))
    notice.value = '邮件发送配置已保存。'
  } catch (e: any) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}
function beforeUnload(event: BeforeUnloadEvent) { if (dirty.value) { event.preventDefault(); event.returnValue = '' } }
onMounted(() => { window.addEventListener('beforeunload', beforeUnload); void load().catch((e: any) => { error.value = e.message }) })
onBeforeUnmount(() => { window.removeEventListener('beforeunload', beforeUnload); password.value = '' })
defineExpose({ dirty })
</script>

<template>
  <form class="panel personal-form mail-settings" @submit.prevent="save">
    <div class="panel-heading">
      <div><h2><Mail :size="18" />发信账号</h2><p>验证邮件和条件提醒使用这里的 SMTP。授权码只保存在本站点。</p></div>
      <span v-if="loaded" :class="['tag', statusKind]">{{ statusLabel }}</span>
    </div>
    <p v-if="error" class="public-message error mail-alert" role="alert">{{ error }}</p>
    <p v-if="notice" class="public-message mail-alert" role="status">{{ notice }}</p>
    <label class="mail-switch">
      <input v-model="form.enabled" type="checkbox" />
      <span><strong>启用邮件发送</strong><small>关闭后账号仍会保存，但验证邮件和条件提醒不会发出。</small></span>
    </label>
    <div class="mail-columns">
      <div class="form-grid">
        <p class="mail-caption span-two">服务器</p>
        <label class="span-two">邮箱服务
          <select v-model="form.provider" @change="applyProvider">
            <option v-for="item in providers" :key="item.id" :value="item.id">{{ item.label }}</option>
          </select>
        </label>
        <p v-if="selected?.note" class="mail-hint span-two">{{ selected.note }}</p>
        <label class="span-two">SMTP 服务器<input v-model="form.host" maxlength="200" autocomplete="off" required /></label>
        <label>端口<input v-model.number="form.port" type="number" min="1" max="65535" required /></label>
        <label>加密方式
          <select :value="form.starttls ? 'starttls' : 'ssl'" @change="form.starttls = ($event.target as HTMLSelectElement).value === 'starttls'">
            <option value="ssl">SSL</option>
            <option value="starttls">STARTTLS</option>
          </select>
        </label>
      </div>
      <div class="form-grid">
        <p class="mail-caption span-two">发信身份</p>
        <label class="span-two">发信账号<input v-model="form.username" type="email" maxlength="200" autocomplete="off" placeholder="you@example.com" required /></label>
        <label class="span-two">发件人<input v-model="form.sender" type="email" maxlength="200" autocomplete="off" placeholder="you@example.com" required /></label>
        <label class="span-two">授权码或密码<input v-model="password" type="password" maxlength="200" autocomplete="new-password" :placeholder="passwordSet && saved ? '已保存，留空则不修改' : '授权码或密码'" /></label>
        <p v-if="passwordSet && passwordHint" class="mail-hint span-two">已保存授权码的末四位是 {{ passwordHint }}。留空则不修改。</p>
      </div>
    </div>
    <p class="mail-hint mail-status">{{ statusText }}</p>
    <div class="form-actions"><button class="primary" :disabled="busy || !loaded"><Check :size="16" />保存邮件配置</button></div>
  </form>
</template>

<style scoped>
.mail-settings h2 { display: flex; align-items: center; gap: 8px; }
.mail-settings h2 svg { color: var(--green); }
.mail-alert { margin: 0 24px 16px; }
.mail-switch { display: flex; flex-direction: row; align-items: flex-start; gap: 12px; margin: 0 24px 8px; padding: 14px 16px; border: 1px solid var(--border); border-radius: 8px; background: color-mix(in srgb, var(--green) 6%, var(--surface, #fff)); }
.mail-switch input { width: 15px; height: 15px; margin-top: 3px; flex-shrink: 0; }
.mail-switch span { display: grid; gap: 4px; }
.mail-switch strong { color: var(--dark); font-size: 14px; font-weight: 600; }
.mail-switch small, .mail-hint { color: var(--muted); font-size: 12px; line-height: 1.7; }
.mail-columns { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 28px; align-items: start; padding: 8px 24px 0; }
.mail-settings .mail-columns .form-grid { padding: 0; }
.mail-caption { margin: 4px 0 2px; color: var(--dark); font-size: 13px; font-weight: 650; }
.mail-hint { margin: 0 0 14px; }
.mail-status { padding: 0 24px; }
@media (max-width: 860px) {
  .mail-columns, .mail-settings .mail-columns .form-grid { grid-template-columns: 1fr; }
  .mail-alert, .mail-switch, .mail-columns, .mail-status { margin-inline: 16px; padding-inline: 0; }
  .mail-switch { margin-inline: 16px; padding: 14px; }
}
</style>
