<template>
  <span class="conn-badge" :class="`conn-${roomStore.connection}`">{{ text }}</span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoomStore } from '../stores/room'

const roomStore = useRoomStore()
const text = computed(
  () =>
    ({
      connecting: '连接中…',
      online: '在线',
      reconnecting: '重连中…',
      closed: '已断开',
    })[roomStore.connection],
)
</script>
