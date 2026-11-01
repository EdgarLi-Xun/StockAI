<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
const props = defineProps<{ message: string }>()
defineEmits<{ choose: [answer: boolean] }>()
const cancelButton = ref<HTMLButtonElement | null>(null), acceptButton = ref<HTMLButtonElement | null>(null)
let previousFocus: HTMLElement | null = null
watch(() => props.message, async message => { if (message) { previousFocus = document.activeElement as HTMLElement; await nextTick(); cancelButton.value?.focus() } else if (previousFocus?.isConnected) previousFocus.focus() })
function trap(event: KeyboardEvent) { if (event.shiftKey) (document.activeElement === cancelButton.value ? acceptButton : cancelButton).value?.focus(); else (document.activeElement === acceptButton.value ? cancelButton : acceptButton).value?.focus() }
</script>
<template>
  <Teleport to="body"><div v-if="message" class="modal-scrim" @click.self="$emit('choose', false)"><section class="panel confirm-card" role="dialog" aria-modal="true" aria-labelledby="stockai-confirm-title" @keydown.esc="$emit('choose', false)" @keydown.tab.prevent="trap"><h2 id="stockai-confirm-title">请确认本次操作</h2><p>{{ message }}</p><div class="button-row"><button ref="cancelButton" class="secondary" @click="$emit('choose', false)">取消</button><button ref="acceptButton" class="primary" @click="$emit('choose', true)">确认继续</button></div></section></div></Teleport>
</template>
<style scoped>
.confirm-card{width:min(460px,calc(100vw - 32px));padding:27px;box-shadow:0 16px 50px #18352924}.confirm-card h2{font-size:17px}.confirm-card p{font-size:13px;color:#738278;line-height:1.95;margin:20px 0 25px;white-space:pre-line}.confirm-card .button-row{justify-content:flex-end}
</style>
