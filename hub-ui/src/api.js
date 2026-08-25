const BASE = '/api'

async function request(path, options) {
  const resp = await fetch(`${BASE}${path}`, options)
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}))
    throw new Error(body.detail || `${resp.status} ${resp.statusText}`)
  }
  return resp.json()
}

export const getKinds = () => request('/kinds')
export const listInstances = () => request('/instances')
export const createInstance = (payload) =>
  request('/instances', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
export const deleteInstance = (id) => request(`/instances/${id}`, { method: 'DELETE' })
export const rotateInstance = (id) => request(`/instances/${id}/rotate`, { method: 'POST' })
export const getOauthProviderStatus = () => request('/oauth-provider')

export function logsSocketUrl(id) {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}${BASE}/instances/${id}/logs/stream`
}
