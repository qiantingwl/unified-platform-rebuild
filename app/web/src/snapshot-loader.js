// All refresh triggers share one download. Login invalidates older requests,
// including responses that finish after aborting the network request.
export function createSnapshotLoader({
  fetchSnapshot,
  onSuccess,
  onError,
  timeoutMs = 30000,
}) {
  let active = null;
  function cancel() {
    const previous = active;
    active = null;
    if (previous) {
      clearTimeout(previous.timer);
      previous.controller.abort();
    }
  }
  function refresh() {
    if (active) return active.promise;
    const request = { controller: new AbortController() };
    active = request;
    request.timer = setTimeout(() => request.controller.abort(), timeoutMs);
    request.promise = Promise.resolve()
      .then(() => fetchSnapshot(request.controller.signal))
      .then((data) => {
        if (active === request) onSuccess(data);
      })
      .catch((error) => {
        if (active === request) onError(error);
      })
      .finally(() => {
        clearTimeout(request.timer);
        if (active === request) active = null;
      });
    return request.promise;
  }
  return { refresh, cancel };
}
