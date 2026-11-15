<template>
  <div class="card">
    <h2>在线成员({{ members.length }})</h2>
    <div v-for="m in members" :key="m.user_id" class="member-row">
      <span class="badge-host" v-if="m.is_host">房主</span>
      <span>{{ m.nickname }}</span>
    </div>
    <p class="muted-text">昵称可重复,以进入时间为移交顺序(需求 3.1.5)。</p>
  </div>
</template>

<script setup lang="ts">
import type { MemberInfo } from '../stores/room'

defineProps<{ members: MemberInfo[] }>()
</script>
