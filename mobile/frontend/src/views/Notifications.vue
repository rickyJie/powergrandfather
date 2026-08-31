<script setup lang="ts">
import { onMounted, ref, computed } from "vue";
import { useRouter } from "vue-router";
import { showConfirmDialog, showToast } from "vant";
import { useNotificationsStore } from "@/stores/notifications";
import NotificationRow from "@/components/notification/NotificationRow.vue";
import { notificationsApi, type NotificationItem } from "@/api/notifications";

const store = useNotificationsStore();
const router = useRouter();

const tab = ref<"all" | "unread" | "read">("all");

const displayed = computed<NotificationItem[]>(() => {
  if (tab.value === "unread") return store.unread;
  if (tab.value === "read") return store.readOnly;
  return store.items;
});

async function clearAll() {
  try {
    await showConfirmDialog({
      title: "Clear all notifications?",
      message: "This removes every notification from the mobile list.",
      confirmButtonText: "Clear",
    });
  } catch {
    return;
  }
  try {
    await notificationsApi.clearAll();
    await store.refresh();
    showToast({ message: "Cleared", type: "success" });
  } catch {
    showToast({ message: "Clear failed", type: "fail" });
  }
}

function openItem(n: NotificationItem) {
  store.markRead(n.id);
  // Session-only scope: deep-link into the related session. Mission-only
  // notifications have no mobile target (missions live on the desktop console),
  // so they just mark read without navigating.
  if (n.session_id) {
    router.push(`/s/${n.session_id}`);
  }
}

// The realtime WS + polling are owned app-wide by App.vue — this view must NOT
// connect/disconnect them (doing so on unmount killed the global stream after a
// single visit). Just pull the freshest list on open.
onMounted(() => {
  store.refresh();
});
</script>

<template>
  <div class="wrap">
    <van-nav-bar
      title="Notifications"
      left-arrow
      fixed
      placeholder
      @click-left="router.push('/')"
    >
      <template #right>
        <van-icon name="delete-o" size="18" @click="clearAll" />
      </template>
    </van-nav-bar>
    <van-tabs v-model:active="tab" swipeable>
      <van-tab name="all" :title="`All (${store.items.length})`" />
      <van-tab name="unread" :title="`Unread (${store.unread.length})`" />
      <van-tab name="read" title="Read" />
    </van-tabs>

    <van-pull-refresh :model-value="store.loading" @refresh="store.refresh">
      <div v-if="store.loading && displayed.length === 0" class="loading">
        <van-loading />
      </div>
      <van-empty v-else-if="displayed.length === 0" description="No notifications" />
      <van-swipe-cell v-for="n in displayed" :key="n.id">
        <NotificationRow :item="n" @click="openItem(n)" />
        <template #right>
          <van-button
            v-if="!n.read"
            square
            type="primary"
            text="Read"
            class="swipe-btn"
            @click="store.markRead(n.id)"
          />
          <van-button
            square
            type="danger"
            text="Dismiss"
            class="swipe-btn"
            @click="store.dismiss(n.id)"
          />
        </template>
      </van-swipe-cell>
    </van-pull-refresh>
  </div>
</template>

<style scoped>
.wrap {
  min-height: 100%;
}
.loading {
  padding: 40px;
  text-align: center;
}
.swipe-btn {
  height: 100%;
}
</style>
