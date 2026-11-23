// Job polling: GET /api/v1/jobs/{id} every POLL_MS until the job has succeeded or failed, then
// onDone(job). A request that fails is tried again; an ended session stops the polling
// (`guard` returns true for it and has told the person).

export const POLL_MS = 2000;
const ENDED = new Set(["succeeded", "failed"]);

export function pollJob(jobId, onDone, { fetchImpl = globalThis.fetch, clock = globalThis, guard }) {
  const again = () => clock.setTimeout(() => pollJob(jobId, onDone, { fetchImpl, clock, guard }), POLL_MS);
  return fetchImpl("/api/v1/jobs/" + encodeURIComponent(jobId))
    .then((response) => (guard(response) ? null : response.json()))
    .then((job) => {
      if (!job) return;
      if (ENDED.has(job.status)) onDone(job);
      else again();
    })
    .catch(again);
}
