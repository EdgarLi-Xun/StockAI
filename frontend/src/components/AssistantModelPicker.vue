<script setup lang="ts">
import { computed } from 'vue'
import { Settings2 } from 'lucide-vue-next'
const props=defineProps<{modelValue:string,profiles:any[]}>()
const emit=defineEmits<{'update:modelValue':[value:string],configure:[]}>()
const current=computed(()=>props.profiles.find(profile=>props.modelValue?profile.id===props.modelValue:profile.is_default))
</script>

<template>
  <div class="assistant-model-picker">
    <label>个人模型<select :value="modelValue" @change="emit('update:modelValue',($event.target as HTMLSelectElement).value)"><option value="">使用本人默认配置</option><option v-for="profile in profiles.filter(profile=>profile.active)" :key="profile.id" :value="profile.id">{{ profile.name }} · {{ profile.model }}</option></select></label>
    <button class="icon-button" type="button" aria-label="模型配置与授权" @click="emit('configure')"><Settings2 :size="16"/></button>
    <small>{{ current ? current.model : '尚未选择可用配置，可先查看项目帮助' }}</small>
  </div>
</template>
