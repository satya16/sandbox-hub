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

export function logsSocketUrl(id) {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}${BASE}/instances/${id}/logs/stream`
}
