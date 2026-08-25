import { useEffect, useRef, useState } from 'react'
import { Card, Tag, Typography, Button, Space, Collapse, message, Popconfirm } from 'antd'
import { ReloadOutlined, DeleteOutlined } from '@ant-design/icons'
import { deleteInstance, rotateInstance, logsSocketUrl } from './api'

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
  if (auth.mode === 'apikey') return `curl -H "${auth.header}: ${auth.api_key}" ${target}`
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

const KIND_LABEL = { 'rest-api': 'REST API', 'mcp-server': 'MCP Server' }

export default function InstanceCard({ instance, onChanged }) {
  const [busy, setBusy] = useState(false)
  const [logsOpen, setLogsOpen] = useState(false)

  const canRotate = instance.auth?.mode !== 'none' || instance.openapi?.protected

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
          <Tag>{instance.auth?.mode}</Tag>
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
          onChange={(keys) => setLogsOpen(keys.length > 0)}
          items={[{ key: 'logs', label: 'Logs', children: <LogsPanel id={instance.id} active={logsOpen} /> }]}
        />
      </Space>
    </Card>
  )
}
