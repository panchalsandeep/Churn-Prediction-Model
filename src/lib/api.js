/* Thin fetch wrapper: auth header, JSON, readable errors, job polling, file download. */
import { state } from './ctx.js';

const BASE = '/api';

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
    this.code = body?.code;
  }
}

async function authHeaders() {
  const headers = {};
  if (state.workspaceId) headers['X-Workspace'] = state.workspaceId;
  if (state.user) {
    // The SDK caches the token and refreshes it shortly before expiry, so no forced round-trip.
    state.idToken = await state.user.getIdToken();
  }
  if (state.idToken) headers.Authorization = `Bearer ${state.idToken}`;
  return headers;
}

export async function request(path, { method = 'GET', json, form, signal } = {}) {
  const headers = await authHeaders();
  let body;
  if (json !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(json);
  } else if (form) {
    body = form;
  }
  let res;
  try {
    res = await fetch(`${BASE}${path}`, { method, headers, body, signal });
  } catch (err) {
    if (err.name === 'AbortError') throw err;
    throw new ApiError('Cannot reach the server. Check your connection.', 0);
  }
  if (res.status === 304) return null;
  let data = null;
  const text = await res.text();
  if (text) {
    try { data = JSON.parse(text); } catch { data = { error: text.slice(0, 200) }; }
  }
  if (!res.ok) {
    const retry = res.headers.get('Retry-After');
    const message = res.status === 429
      ? `Too many requests. Try again in ${retry || 'a few'} seconds.`
      : data?.error || `Request failed (${res.status})`;
    throw new ApiError(message, res.status, data);
  }
  return data;
}

export const get = (path, opts) => request(path, opts);
export const post = (path, json, opts) => request(path, { ...opts, method: 'POST', json });
export const patch = (path, json, opts) => request(path, { ...opts, method: 'PATCH', json });

/** Poll a background job until it finishes. Resolves with the job result or throws its error. */
export async function pollJob(jobId, { onProgress, signal, interval = 700 } = {}) {
  for (;;) {
    const job = await get(`/jobs/${jobId}`, { signal });
    onProgress?.(job);
    if (job.status === 'done') return job.result;
    if ((job.status === 'error' || job.status === 'cancelled')) throw new ApiError(job.error || 'Job failed', 500);
    await new Promise(r => setTimeout(r, interval));
  }
}

/** Start a job endpoint and wait for it (the server may already have finished it inline). */
export async function runJob(path, { form, json, onProgress, signal } = {}) {
  const res = await request(path, { method: 'POST', form, json, signal });
  if (res.job && res.job.status === 'done') return res.job.result;
  if (res.job && (res.job.status === 'error' || res.job.status === 'cancelled')) throw new ApiError(res.job.error || 'Job failed', 500);
  return pollJob(res.job_id, { onProgress, signal });
}

export async function download(path, filename, { method = 'GET', json } = {}) {
  const headers = await authHeaders();
  if (json !== undefined) headers['Content-Type'] = 'application/json';
  const res = await fetch(`${BASE}${path}`, { method, headers, body: json !== undefined ? JSON.stringify(json) : undefined });
  if (!res.ok) throw new ApiError((await res.json().catch(() => ({}))).error || 'Export failed', res.status);
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
