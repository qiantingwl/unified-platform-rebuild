// A revision arriving during a slow download requests one follow-up read; it
// must not invalidate the only response capable of populating the page.
export function createRecordLoader({
  fetchRecords,
  onSuccess,
  onError,
  delayMs = 2000,
  timeoutMs = 30000,
}) {
  let active = null,
    pending = false,
    timer = null,
    stopped = false;
  function refresh() {
    if (stopped) return;
    if (active || timer) {
      pending = true;
      return;
    }
    const controller = new AbortController();
    active = controller;
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    Promise.resolve()
      .then(() => fetchRecords(controller.signal))
      .then((value) => {
        if (!stopped) onSuccess(value);
      })
      .catch((error) => {
        if (!stopped) onError(error);
      })
      .finally(() => {
        clearTimeout(timeout);
        active = null;
        if (!stopped && pending) {
          pending = false;
          timer = setTimeout(() => {
            timer = null;
            pending = false;
            refresh();
          }, delayMs);
        }
      });
  }
  function stop() {
    stopped = true;
    clearTimeout(timer);
    active?.abort();
  }
  return { refresh, stop };
}
