import { useEffect, useRef, useState } from 'react'
import {
  Card,
  Tag,
  Typography,
  Button,
  Space,
  Collapse,
  message,
  Popconfirm,
  Select,
  Input,
  InputNumber,
  Slider,
  Form,
  List,
  Empty,
} from 'antd'
import { ReloadOutlined, DeleteOutlined, PlusOutlined } from '@ant-design/icons'
import {
  deleteInstance,
  rotateInstance,
  logsSocketUrl,
  setChaosConfig,
  listRoutes,
  createRoute,
  deleteRoute,
  listWebhookRequests,
  clearWebhookRequests,
} from './api'

const { Text, Paragraph } = Typography

const STATE_COLOR = {
  running: 'green',
  stopped: 'default',
  exited: 'default',
  created: 'gold',
}

const PATH_SUFFIX = { 'rest-api': '/items', 'mcp-server': '/mcp' }

function buildSnippet(instance) {
  const { kind, auth, url } = instance
  if (!auth || !url) return null
  const target = `${url}${PATH_SUFFIX[kind] || ''}`

  if (auth.mode === 'none') return `curl ${target}`
  if (auth.mode === 'apikey') return `curl -H "X-API-Key: ${auth.api_key}" ${target}`
  if (auth.mode === 'basic') return `curl -u ${auth.username}:${auth.password} ${target}`
  if (auth.mode === 'jwt') return `curl -H "Authorization: Bearer ${auth.token}" ${target}`
  if (auth.mode === 'session') {
    const loginCmd = `curl -s -c cookies.txt -X POST ${auth.login_url} \\\n  -H "Content-Type: application/json" -d '{"username":"${auth.username}","password":"${auth.password}"}'`
    const useCmd = `curl -b cookies.txt ${target}`
    return `${loginCmd}\n${useCmd}`
  }
  if (auth.mode === 'oauth') {
    const tokenCmd = `TOKEN=$(curl -s -X POST ${auth.token_endpoint} \\\n  -d "grant_type=client_credentials&client_id=${auth.client_id}&client_secret=${auth.client_secret}" \\\n  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")`
    const useCmd = `curl -H "Authorization: Bearer $TOKEN" ${target}`
    return `${tokenCmd}\n${useCmd}`
  }
  return null
}

function AuthDetails({ instance }) {
  const { auth } = instance
  if (!auth) return null

  return (
    <div>
      {auth.mode === 'apikey' && (
        <Paragraph style={{ marginBottom: 4 }}>
          <Text type="secondary">Header:</Text> <Text code>{auth.header}</Text>
          <br />
          <Text type="secondary">Key:</Text>{' '}
          <Text code copyable={{ text: auth.api_key }}>
            {auth.api_key}
          </Text>
        </Paragraph>
      )}
      {auth.mode === 'basic' && (
        <Paragraph style={{ marginBottom: 4 }}>
          <Text type="secondary">Username:</Text> <Text code copyable>{auth.username}</Text>
          <br />
          <Text type="secondary">Password:</Text>{' '}
          <Text code copyable={{ text: auth.password }}>
            {auth.password}
          </Text>
        </Paragraph>
      )}
      {auth.mode === 'jwt' && (
        <Paragraph style={{ marginBottom: 4 }}>
          <Text type="secondary">Token (self-contained, verified locally, no external calls):</Text>
          <br />
          <Text code copyable={{ text: auth.token }} style={{ wordBreak: 'break-all', fontSize: 11 }}>
            {auth.token}
          </Text>
          <br />
          <Text type="secondary">Get a fresh one anytime:</Text> <Text code copyable>{auth.debug_token_url}</Text>
        </Paragraph>
      )}
      {auth.mode === 'session' && (
        <Paragraph style={{ marginBottom: 4 }}>
          <Text type="secondary">Login:</Text> <Text code copyable>{auth.login_url}</Text>
          <br />
          <Text type="secondary">Username:</Text> <Text code copyable>{auth.username}</Text>
          <br />
          <Text type="secondary">Password:</Text>{' '}
          <Text code copyable={{ text: auth.password }}>
            {auth.password}
          </Text>
          <br />
          <Text type="secondary">Logout:</Text> <Text code copyable>{auth.logout_url}</Text>
        </Paragraph>
      )}
      {auth.mode === 'oauth' && (
        <Paragraph style={{ marginBottom: 4 }}>
          <Text type="secondary">Token endpoint:</Text> <Text code copyable>{auth.token_endpoint}</Text>
          <br />
          <Text type="secondary">Client ID:</Text> <Text code copyable>{auth.client_id}</Text>
          <br />
          <Text type="secondary">Client secret:</Text>{' '}
          <Text code copyable={{ text: auth.client_secret }}>
            {auth.client_secret}
          </Text>
        </Paragraph>
      )}
      {instance.url && (
        <Paragraph style={{ marginBottom: 4 }}>
          <Text type="secondary">URL:</Text>{' '}
          <Text code copyable>
            {instance.url}
            {PATH_SUFFIX[instance.kind] || ''}
          </Text>
        </Paragraph>
      )}
      {buildSnippet(instance) && (
        <Paragraph style={{ marginBottom: 4 }}>
          <Text type="secondary">Try it:</Text>
          <pre style={{ background: '#f5f5f5', padding: 8, borderRadius: 4, overflowX: 'auto', fontSize: 12 }}>
            {buildSnippet(instance)}
          </pre>
        </Paragraph>
      )}
    </div>
  )
}

function OpenApiDetails({ instance }) {
  const openapi = instance.openapi
  if (!openapi) return null
  return (
    <div style={{ marginTop: 8, paddingTop: 8, borderTop: '1px solid #f0f0f0' }}>
      <Text strong>OpenAPI ({openapi.version})</Text>
      <Paragraph style={{ marginBottom: 4, marginTop: 4 }}>
        <Text type="secondary">Spec:</Text> <Text code copyable>{openapi.spec_url}</Text>
        <br />
        <Text type="secondary">Swagger UI:</Text> <Text code copyable>{openapi.docs_url}</Text>
        <br />
        <Text type="secondary">ReDoc:</Text> <Text code copyable>{openapi.redoc_url}</Text>
      </Paragraph>
      {openapi.protected ? (
        <Paragraph style={{ marginBottom: 4 }}>
          <Tag color="orange">spec protected</Tag>
          <br />
          <Text type="secondary">Header:</Text> <Text code>{openapi.auth.header}</Text>
          <br />
          <Text type="secondary">Token:</Text>{' '}
          <Text code copyable={{ text: openapi.auth.token }}>
            {openapi.auth.token}
          </Text>
          <br />
          <pre style={{ background: '#f5f5f5', padding: 8, borderRadius: 4, overflowX: 'auto', fontSize: 12 }}>
            {`curl -H "${openapi.auth.header}: ${openapi.auth.token}" ${openapi.spec_url}`}
          </pre>
        </Paragraph>
      ) : (
        <Tag>spec open</Tag>
      )}
    </div>
  )
}

function ChaosPanel({ instance, onChanged }) {
  const [form] = Form.useForm()
  const [saving, setSaving] = useState(false)
  const chaos = instance.chaos
  const mode = Form.useWatch('mode', form) ?? chaos?.mode ?? 'normal'

  const save = async () => {
    try {
      const values = await form.validateFields()
      setSaving(true)
      await setChaosConfig(instance.id, values)
      await onChanged()
      message.success('config updated')
    } catch (err) {
      if (err?.errorFields) return
      message.error(err.message)
    } finally {
      setSaving(false)
    }
  }

  if (!chaos) return <Text type="secondary">loading config…</Text>

  return (
    <Form
      form={form}
      layout="vertical"
      size="small"
      initialValues={{
        mode: chaos.mode,
        rate_limit: chaos.rate_limit,
        chaos: chaos.chaos,
      }}
    >
      <Paragraph type="secondary" style={{ marginBottom: 8 }}>
        Test endpoint: <Text code copyable>{instance.url}/test</Text>
      </Paragraph>
      <Form.Item name="mode" label="Mode">
        <Select
          options={[
            { value: 'normal', label: 'Normal (always 200)' },
            { value: 'rate_limit', label: 'Rate limit' },
            { value: 'chaos', label: 'Chaos (custom status/body/latency)' },
          ]}
        />
      </Form.Item>

      {mode === 'rate_limit' && (
        <Space>
          <Form.Item name={['rate_limit', 'limit']} label="Requests allowed">
            <InputNumber min={1} />
          </Form.Item>
          <Form.Item name={['rate_limit', 'window_seconds']} label="Per (seconds)">
            <InputNumber min={1} />
          </Form.Item>
        </Space>
      )}

      {mode === 'chaos' && (
        <>
          <Space>
            <Form.Item name={['chaos', 'status_code']} label="Status code">
              <InputNumber min={100} max={599} />
            </Form.Item>
            <Form.Item name={['chaos', 'latency_ms']} label="Latency (ms)">
              <InputNumber min={0} />
            </Form.Item>
          </Space>
          <Form.Item name={['chaos', 'body']} label="Response body (JSON)" getValueProps={(v) => ({ value: typeof v === 'string' ? v : JSON.stringify(v) })} normalize={(v) => { try { return JSON.parse(v) } catch { return v } }}>
            <Input.TextArea rows={2} />
          </Form.Item>
          <Form.Item
            name={['chaos', 'failure_rate']}
            label="Random failure rate"
            tooltip="Chance (0-1) of returning a random 5xx instead of the configured response"
          >
            <Slider min={0} max={1} step={0.05} />
          </Form.Item>
        </>
      )}

      <Button size="small" type="primary" loading={saving} onClick={save}>
        Save
      </Button>
    </Form>
  )
}

function MockRoutesPanel({ instanceId, active, onChanged }) {
  const [routes, setRoutes] = useState([])
  const [form] = Form.useForm()
  const [busy, setBusy] = useState(false)

  const load = async () => {
    try {
      setRoutes(await listRoutes(instanceId))
    } catch (err) {
      message.error(err.message)
    }
  }

  useEffect(() => {
    if (active) load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active])

  const addRoute = async () => {
    try {
      const values = await form.validateFields()
      setBusy(true)
      let response_body
      try {
        response_body = JSON.parse(values.response_body)
      } catch {
        message.error('response body must be valid JSON')
        setBusy(false)
        return
      }
      const required_fields = values.required_fields
        ? values.required_fields.split(',').map((s) => s.trim()).filter(Boolean)
        : []
      await createRoute(instanceId, {
        method: values.method,
        path: values.path,
        status_code: values.status_code,
        response_body,
        required_fields,
      })
      form.resetFields()
      await load()
      await onChanged()
    } catch (err) {
      if (err?.errorFields) return
      message.error(err.message)
    } finally {
      setBusy(false)
    }
  }

  const remove = async (routeId) => {
    await deleteRoute(instanceId, routeId)
    await load()
    await onChanged()
  }

  return (
    <div>
      <List
        size="small"
        dataSource={routes}
        locale={{ emptyText: 'No routes yet -- add one below.' }}
        renderItem={(r) => (
          <List.Item
            actions={[
              <Button key="del" size="small" danger type="text" icon={<DeleteOutlined />} onClick={() => remove(r.id)} />,
            ]}
          >
            <Text code>
              {r.method} {r.path}
            </Text>{' '}
            <Tag>{r.status_code}</Tag>
          </List.Item>
        )}
      />
      <Form form={form} layout="vertical" size="small" style={{ marginTop: 12 }} initialValues={{ method: '*', path: '*', status_code: 200, response_body: '{"ok": true}' }}>
        <Space wrap>
          <Form.Item name="method" label="Method" style={{ marginBottom: 8 }}>
            <Select style={{ width: 100 }}>
              {['*', 'GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map((m) => (
                <Select.Option key={m} value={m}>
                  {m}
                </Select.Option>
              ))}
            </Select>
          </Form.Item>
          <Form.Item name="path" label="Path (or *)" style={{ marginBottom: 8 }}>
            <Input style={{ width: 160 }} placeholder="/users or *" />
          </Form.Item>
          <Form.Item name="status_code" label="Status" style={{ marginBottom: 8 }}>
            <InputNumber min={100} max={599} style={{ width: 90 }} />
          </Form.Item>
        </Space>
        <Form.Item name="response_body" label="Response body (JSON, supports {{request.body.x}}, {{uuid}}, {{now}})">
          <Input.TextArea rows={3} />
        </Form.Item>
        <Form.Item name="required_fields" label="Required request body fields (comma-separated, optional)">
          <Input placeholder="name, email" />
        </Form.Item>
        <Button size="small" type="primary" icon={<PlusOutlined />} loading={busy} onClick={addRoute}>
          Add route
        </Button>
      </Form>
    </div>
  )
}

function WebhookRequestsPanel({ instanceId, active }) {
  const [requests, setRequests] = useState([])

  const load = async () => {
    try {
      setRequests(await listWebhookRequests(instanceId))
    } catch (err) {
      message.error(err.message)
    }
  }

  useEffect(() => {
    if (!active) return
    load()
    const interval = setInterval(load, 3000)
    return () => clearInterval(interval)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active])

  const clear = async () => {
    await clearWebhookRequests(instanceId)
    await load()
  }

  return (
    <div>
      <Button size="small" onClick={clear} style={{ marginBottom: 8 }}>
        Clear
      </Button>
      {requests.length === 0 ? (
        <Empty description="No requests received yet" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : (
        <List
          size="small"
          dataSource={[...requests].reverse()}
          renderItem={(r) => (
            <List.Item>
              <div style={{ width: '100%' }}>
                <Text code>
                  {r.method} {r.path}
                </Text>{' '}
                <Text type="secondary" style={{ fontSize: 11 }}>
                  {r.received_at}
                </Text>
                <pre style={{ background: '#f5f5f5', padding: 6, borderRadius: 4, fontSize: 11, margin: '4px 0 0' }}>
                  {JSON.stringify(r.body_json ?? r.body_text, null, 2)}
                </pre>
              </div>
            </List.Item>
          )}
        />
      )}
    </div>
  )
}

function LogsPanel({ id, active }) {
  const [lines, setLines] = useState([])
  const boxRef = useRef(null)

  useEffect(() => {
    if (!active) return
    const ws = new WebSocket(logsSocketUrl(id))
    ws.onmessage = (evt) => setLines((prev) => [...prev.slice(-500), evt.data])
    return () => ws.close()
  }, [id, active])

  useEffect(() => {
    if (boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight
  }, [lines])

  return (
    <div
      ref={boxRef}
      style={{
        background: '#111',
        color: '#0f0',
        fontFamily: 'monospace',
        fontSize: 12,
        padding: 8,
        height: 200,
        overflowY: 'auto',
        borderRadius: 4,
        whiteSpace: 'pre-wrap',
      }}
    >
      {lines.length === 0 ? 'waiting for log output…' : lines.join('')}
    </div>
  )
}

const KIND_LABEL = {
  'rest-api': 'REST API',
  'mcp-server': 'MCP Server',
  'mock-api': 'Mock API',
  'webhook-receiver': 'Webhook Receiver',
  'chaos-api': 'Rate Limit / Chaos API',
}

export default function InstanceCard({ instance, onChanged }) {
  const [busy, setBusy] = useState(false)
  const [openPanel, setOpenPanel] = useState(null)

  const canRotate = ['apikey', 'basic', 'jwt', 'session', 'oauth'].includes(instance.auth?.mode) || instance.openapi?.protected

  const rotate = async () => {
    setBusy(true)
    try {
      await rotateInstance(instance.id)
      await onChanged()
      message.success('credentials rotated')
    } catch (err) {
      message.error(err.message)
    } finally {
      setBusy(false)
    }
  }

  const remove = async () => {
    setBusy(true)
    try {
      await deleteInstance(instance.id)
      await onChanged()
    } catch (err) {
      message.error(err.message)
      setBusy(false)
    }
  }

  const items = []
  if (instance.kind === 'chaos-api') {
    items.push({ key: 'chaos', label: 'Configure', children: <ChaosPanel instance={instance} onChanged={onChanged} /> })
  }
  if (instance.kind === 'mock-api') {
    items.push({
      key: 'routes',
      label: `Routes (${instance.mock_routes?.length ?? 0})`,
      children: <MockRoutesPanel instanceId={instance.id} active={openPanel === 'routes'} onChanged={onChanged} />,
    })
  }
  if (instance.kind === 'webhook-receiver') {
    items.push({
      key: 'requests',
      label: 'Received requests',
      children: <WebhookRequestsPanel instanceId={instance.id} active={openPanel === 'requests'} />,
    })
  }
  items.push({ key: 'logs', label: 'Logs', children: <LogsPanel id={instance.id} active={openPanel === 'logs'} /> })

  return (
    <Card
      title={instance.name}
      extra={
        <Popconfirm title="Stop and remove this instance?" onConfirm={remove}>
          <Button size="small" danger icon={<DeleteOutlined />} loading={busy} />
        </Popconfirm>
      }
      style={{ marginBottom: 16 }}
    >
      <Space orientation="vertical" style={{ width: '100%' }}>
        <div>
          <Tag color={STATE_COLOR[instance.state] || 'default'}>{instance.state}</Tag>
          <Tag color="blue">{KIND_LABEL[instance.kind] || instance.kind}</Tag>
          {instance.auth && <Tag>{instance.auth.mode}</Tag>}
          <Text type="secondary" style={{ fontSize: 12 }}>
            {instance.id}
          </Text>
        </div>

        <AuthDetails instance={instance} />
        <OpenApiDetails instance={instance} />

        {canRotate && (
          <Button size="small" icon={<ReloadOutlined />} onClick={rotate} loading={busy}>
            Rotate credentials
          </Button>
        )}

        <Collapse
          size="small"
          onChange={(keys) => setOpenPanel(keys[0] || null)}
          items={items}
        />
      </Space>
    </Card>
  )
}
