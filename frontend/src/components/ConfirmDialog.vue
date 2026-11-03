<script setup lang="ts">
// 可访问的异步确认弹窗，管理焦点恢复、键盘选择和取消行为。
import { nextTick, ref, watch } from 'vue'
// 通过消息是否为空控制弹窗显示，选择结果由事件通知调用方。
const props = defineProps<{ message: string }>()
defineEmits<{ choose: [answer: boolean] }>()
const cancelButton = ref<HTMLButtonElement | null>(null), acceptButton = ref<HTMLButtonElement | null>(null)
let previousFocus: HTMLElement | null = null
// 打开时记住原焦点并优先聚焦取消；关闭后将焦点还给触发元素。
watch(() => props.message, async message => { if (message) { previousFocus = document.activeElement as HTMLElement; await nextTick(); cancelButton.value?.focus() } else if (previousFocus?.isConnected) previousFocus.focus() })
// 将 Tab 与 Shift+Tab 的焦点限制在取消和确认按钮之间。
function trap(event: KeyboardEvent) { if (event.shiftKey) (document.activeElement === cancelButton.value ? acceptButton : cancelButton).value?.focus(); else (document.activeElement === acceptButton.value ? cancelButton : acceptButton).value?.focus() }
</script>
<template>
<!-- 弹窗挂载到页面根部，遮罩、Esc 和取消按钮都返回否定结果。 -->
  <Teleport to="body"><div v-if="message" class="modal-scrim" @click.self="$emit('choose', false)"><section class="panel confirm-card" role="dialog" aria-modal="true" aria-labelledby="stockai-confirm-title" @keydown.esc="$emit('choose', false)" @keydown.tab.prevent="trap"><h2 id="stockai-confirm-title">请确认本次操作</h2><p>{{ message }}</p><div class="button-row"><button ref="cancelButton" class="secondary" @click="$emit('choose', false)">取消</button><button ref="acceptButton" class="primary" @click="$emit('choose', true)">确认继续</button></div></section></div></Teleport>
</template>
<style scoped>
/* 限定弹窗宽度并保留足够的正文行距与操作区间隔。 */
.confirm-card{width:min(460px,calc(100vw - 32px));padding:27px;box-shadow:0 16px 50px #18352924}.confirm-card h2{font-size:17px}.confirm-card p{font-size:13px;color:#738278;line-height:1.95;margin:20px 0 25px;white-space:pre-line}.confirm-card .button-row{justify-content:flex-end}
</style>
