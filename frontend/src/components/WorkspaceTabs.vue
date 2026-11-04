<script setup lang="ts">
import { computed } from 'vue'
import { workspaceGroup, workspaceTabs } from '../workspaceNavigation'
const props = defineProps<{ current: string }>()
const emit = defineEmits<{ navigate: [route: string] }>()
const tabs = computed(() => workspaceTabs(props.current))
</script>

<template>
  <nav v-if="tabs.length" class="workspace-tabs" :aria-label="workspaceGroup(current)?.label + '子页面'">
    <button v-for="tab in tabs" :key="tab.id" type="button" :aria-current="current === tab.id ? 'page' : undefined" @click="emit('navigate', tab.id)">{{ tab.label }}</button>
  </nav>
</template>
