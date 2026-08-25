import { useEffect, useRef, useState } from 'react'
import { Card, Switch, Tag, Typography, Button, Space, Collapse, message } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { startResource, stopResource, rotateResource, logsSocketUrl } from './api'

const { Text, Paragraph } = Typography

const STATE_COLOR = {
  running: 'green',
  stopped: 'default',
  exited: 'default',
  created: 'gold',
}

function buildSnippet(resource) {
  const { id, category, auth, url } = resource
  if (!auth) return null

  const mcpPath = category === 'mcp-server' ? '/mcp' : ''
  const target = `${url}${mcpPath}`

  if (auth.mode === 'none') {
    return `curl ${target}${category === 'rest-api' ? '/items' : ''}`
  }
  if (auth.mode === 'apikey') {
    return `curl -H "${auth.header}: ${auth.api_key}" ${target}${category === 'rest-api' ? '/items' : ''}`
  }
  if (auth.mode === 'oauth') {
    const tokenCmd = `TOKEN=$(curl -s -X POST ${auth.token_endpoint} \\\n  -d "grant_type=client_credentials&client_id=${auth.client_id}&client_secret=${auth.client_secret}" \\\n  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")`
    const useCmd = `curl -H "Authorization: Bearer $TOKEN" ${target}${category === 'rest-api' ? '/items' : ''}`
    return `${tokenCmd}\n${useCmd}`
  }
  return null
}

function AuthDetails({ resource }) {
  const { auth } = resource
  if (!auth) return null

  return (
    <div style={{ marginTop: 8 }}>
      {auth.mode === 'apikey' && (
        <Paragraph>
          <Text type="secondary">Header:</Text> <Text code>{auth.header}</Text>
          <br />
          <Text type="secondary">Key:</Text>{' '}
          <Text code copyable={{ text: auth.api_key }}>
            {auth.api_key}
          </Text>
        </Paragraph>
      )}
      {auth.mode === 'oauth' && (
        <Paragraph>
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
      {resource.url && (
        <Paragraph>
          <Text type="secondary">URL:</Text> <Text code copyable>{resource.url}{resource.category === 'mcp-server' ? '/mcp' : ''}</Text>
        </Paragraph>
      )}
      {buildSnippet(resource) && (
        <Paragraph>
          <Text type="secondary">Try it:</Text>
          <pre style={{ background: '#f5f5f5', padding: 8, borderRadius: 4, overflowX: 'auto', fontSize: 12 }}>
            {buildSnippet(resource)}
          </pre>
        </Paragraph>
      )}
    </div>
  )
}

function LogsPanel({ id, active }) {
  const [lines, setLines] = useState([])
  const wsRef = useRef(null)
  const boxRef = useRef(null)

  useEffect(() => {
    if (!active) return
    const ws = new WebSocket(logsSocketUrl(id))
    wsRef.current = ws
    ws.onmessage = (evt) => {
      setLines((prev) => [...prev.slice(-500), evt.data])
    }
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

export default function ResourceCard({ resource, onChanged }) {
  const [loading, setLoading] = useState(false)
  const [logsOpen, setLogsOpen] = useState(false)
  const running = resource.state === 'running'

  const toggle = async (checked) => {
    setLoading(true)
    try {
      if (checked) {
        await startResource(resource.id)
      } else {
        await stopResource(resource.id)
      }
      await onChanged()
    } catch (err) {
      message.error(err.message)
    } finally {
      setLoading(false)
    }
  }

  const rotate = async () => {
    setLoading(true)
    try {
      await rotateResource(resource.id)
      await onChanged()
      message.success('credentials rotated')
    } catch (err) {
      message.error(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <Card
      title={resource.name}
      extra={<Switch checked={running} loading={loading} onChange={toggle} />}
      style={{ marginBottom: 16 }}
    >
      <Space orientation="vertical" style={{ width: '100%' }}>
        <div>
          <Tag color={STATE_COLOR[resource.state] || 'default'}>{resource.state}</Tag>
          {resource.auth_mode !== 'n/a' && <Tag>{resource.auth_mode}</Tag>}
          {resource.requires?.length > 0 && <Tag color="blue">needs: {resource.requires.join(', ')}</Tag>}
        </div>
        <Text type="secondary">{resource.description}</Text>

        {running && (
          <>
            <AuthDetails resource={resource} />
            {(resource.auth_mode === 'apikey' || resource.auth_mode === 'oauth') && (
              <Button size="small" icon={<ReloadOutlined />} onClick={rotate} loading={loading}>
                Rotate credentials
              </Button>
            )}
            <Collapse
              size="small"
              onChange={(keys) => setLogsOpen(keys.length > 0)}
              items={[
                {
                  key: 'logs',
                  label: 'Logs',
                  children: <LogsPanel id={resource.id} active={logsOpen} />,
                },
              ]}
            />
          </>
        )}
      </Space>
    </Card>
  )
}
