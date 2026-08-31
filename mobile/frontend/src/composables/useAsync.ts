import { ref, type Ref } from "vue";

/**
 * Single-source async loader with uniform loading / error state, so every
 * data view distinguishes "failed" from "empty" and can offer a retry
 * (pair with <ErrorRetry>). Store-backed views keep their store's own
 * loading/error; this is for view-local one-shot fetches.
 *
 *   const { data: items, loading, error, run } = useAsync(() => api.list(), [])
 *   onMounted(run)
 */
export function useAsync<T>(loader: () => Promise<T>, initial: T) {
  const data = ref(initial) as Ref<T>;
  const loading = ref(false);
  const error = ref(false);

  async function run() {
    loading.value = true;
    try {
      data.value = await loader();
      error.value = false;
    } catch {
      error.value = true;
    } finally {
      loading.value = false;
    }
  }

  return { data, loading, error, run };
}
