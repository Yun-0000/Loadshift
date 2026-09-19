// Each response can update the signed home session. Keep requests in order,
// including across tabs, so a late poll cannot overwrite a newer action.
let sessionQueue = Promise.resolve();
function sessionFetch(url, options = {}) {
  const send = async () => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(url, {...options, signal: controller.signal});
      // Finish reading before releasing the session lock, even on slow links.
      await response.clone().arrayBuffer();
      return response;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('Connection timed out. Please try again.');
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  };
  const run = () => navigator.locks
    ? navigator.locks.request('loadshift-session', send)
    : send();
  const result = sessionQueue.then(run);
  sessionQueue = result.catch(() => {});
  return result;
}
