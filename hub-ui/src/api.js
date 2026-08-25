const BASE = '/api'

async function request(path, options) {
  const resp = await fetch(`${BASE}${path}`, options)
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}))
    throw new Error(body.detail || `${resp.status} ${resp.statusText}`)
  }
  return resp.json()
}

export const listResources = () => request('/resources')
export const startResource = (id) => request(`/resources/${id}/start`, { method: 'POST' })
export const stopResource = (id) => request(`/resources/${id}/stop`, { method: 'POST' })
export const rotateResource = (id) => request(`/resources/${id}/rotate`, { method: 'POST' })
export const fetchLogs = (id) => request(`/resources/${id}/logs`)

export function logsSocketUrl(id) {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}${BASE}/resources/${id}/logs/stream`
}
