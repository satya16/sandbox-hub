const BASE = '/api'

async function request(path, options) {
  const resp = await fetch(`${BASE}${path}`, options)
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}))
    throw new Error(body.detail || `${resp.status} ${resp.statusText}`)
  }
  return resp.json()
}

const jsonBody = (payload) => ({
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(payload),
})

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
export const updatePort = (id, port) => request(`/instances/${id}/port`, { method: 'PUT', ...jsonBody({ port }) })
export const getOauthProviderStatus = () => request('/oauth-provider')

export const setChaosConfig = (id, payload) =>
  request(`/instances/${id}/chaos-config`, { method: 'PUT', ...jsonBody(payload) })

export const listRoutes = (id) => request(`/instances/${id}/routes`)
export const createRoute = (id, payload) =>
  request(`/instances/${id}/routes`, { method: 'POST', ...jsonBody(payload) })
export const deleteRoute = (id, routeId) => request(`/instances/${id}/routes/${routeId}`, { method: 'DELETE' })
export const importOpenapiRoutes = (id, payload) =>
  request(`/instances/${id}/routes/import-openapi`, { method: 'POST', ...jsonBody(payload) })

export const getGraphqlSchema = (id) => request(`/instances/${id}/graphql/schema`)
export const setGraphqlSchema = (id, sdl) =>
  request(`/instances/${id}/graphql/schema`, { method: 'PUT', ...jsonBody({ sdl }) })
export const listGraphqlResolvers = (id) => request(`/instances/${id}/graphql/resolvers`)
export const setGraphqlResolver = (id, payload) =>
  request(`/instances/${id}/graphql/resolvers`, { method: 'POST', ...jsonBody(payload) })
export const deleteGraphqlResolver = (id, type, field) =>
  request(`/instances/${id}/graphql/resolvers/${type}/${field}`, { method: 'DELETE' })

export const listWebhookRequests = (id) => request(`/instances/${id}/webhook-requests`)
export const clearWebhookRequests = (id) => request(`/instances/${id}/webhook-requests`, { method: 'DELETE' })

export const exportInstanceScenario = (id) => request(`/instances/${id}/scenario`)
export const exportAllScenario = () => request('/scenario')
export const importScenario = (payload) =>
  request('/scenario/import', { method: 'POST', ...jsonBody(payload) })

// Triggers a browser "Save file" for JSON data that's already in hand --
// scenario export responses aren't file downloads on the wire, just JSON,
// so this is what turns one into a .json file the user actually gets.
export function downloadJson(filename, data) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

export function logsSocketUrl(id) {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}${BASE}/instances/${id}/logs/stream`
}
