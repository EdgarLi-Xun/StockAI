<script setup lang="ts">
defineProps<{ action: any, disabled: boolean }>()
defineEmits<{ apply: [] }>()
const value = (v: any) => typeof v === 'boolean' ? (v ? '开启' : '关闭') : String(v)
</script>

<template>
  <section class="assistant-action" :aria-label="action.title">
    <strong>{{ action.title }}</strong>
    <dl v-if="action.kind === 'preferences'" class="assistant-action-changes">
      <template v-for="change in action.changes" :key="change.field">
        <dt>{{ change.label }}</dt><dd>{{ value(change.before) }} → <b>{{ value(change.after) }}</b></dd>
      </template>
    </dl>
    <template v-else-if="action.kind === 'add_stock'">
      <p>{{ action.name }} · {{ action.market }}:{{ action.symbol }}</p>
      <p>自选分组：{{ action.group_name }}</p>
      <small v-if="action.already_exists">该股票已在自选中，保留现有分组和置顶设置。</small>
    </template>
    <p v-if="action.changes?.some((c: any) => c.field === 'automatic_ai' && c.after)" class="warning-text">开启后，已启用的研究计划可能消耗个人模型额度。</p>
    <small v-if="action.applied_at" role="status">已由你手动应用</small>
    <template v-else>
      <small>方案尚未执行。核对后点击按钮应用。</small>
      <button class="secondary small" type="button" :disabled="disabled || action.already_exists" @click="$emit('apply')">{{ action.kind === 'preferences' ? '应用配置' : action.already_exists ? '已在自选中' : '添加到自选' }}</button>
    </template>
  </section>
</template>

<style scoped>
.assistant-action{margin:12px 0;padding:13px;border:1px solid #cdded2;border-radius:8px;background:#f5f9f5;font-size:12px;line-height:1.7;overflow-wrap:anywhere}
.assistant-action>strong{font-size:13px}.assistant-action p{margin:7px 0}.assistant-action small{display:block;color:var(--muted);margin:7px 0}.assistant-action button{margin-top:5px}
.assistant-action-changes{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:8px;margin:12px 0}.assistant-action-changes dt{color:var(--muted)}.assistant-action-changes dd{margin:0}
</style>
