/**
 * 路由(design.md 6.1):Home(创建/加入)与 Room(房间页)。
 */

import { createRouter, createWebHistory } from 'vue-router'
import Home from '../views/Home.vue'
import Room from '../views/Room.vue'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'home', component: Home },
    { path: '/rooms/:roomId', name: 'room', component: Room, props: true },
  ],
})
