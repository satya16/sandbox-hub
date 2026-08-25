import { useEffect, useState, useCallback } from 'react'
import { Layout, Typography, Row, Col, Spin, Alert, Button } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import ResourceCard from './ResourceCard'
import { listResources } from './api'

const { Header, Content } = Layout
const { Title, Text } = Typography

const CATEGORY_LABEL = {
  infra: 'Infrastructure',
  'rest-api': 'REST API',
  'mcp-server': 'MCP Server',
}
const CATEGORY_ORDER = ['rest-api', 'mcp-server', 'infra']

function App() {
  const [resources, setResources] = useState(null)
  const [error, setError] = useState(null)

  const refresh = useCallback(async () => {
    try {
      const data = await listResources()
      setResources(data)
      setError(null)
    } catch (err) {
      setError(err.message)
    }
  }, [])

  useEffect(() => {
    refresh()
    const interval = setInterval(refresh, 5000)
    return () => clearInterval(interval)
  }, [refresh])

  const grouped = {}
  ;(resources || []).forEach((r) => {
    grouped[r.category] = grouped[r.category] || []
    grouped[r.category].push(r)
  })

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
        <Title level={3} style={{ color: 'white', margin: 0 }}>
          sandbox-hub
        </Title>
        <Text style={{ color: 'rgba(255,255,255,0.65)' }}>local test resources, on demand</Text>
        <Button
          type="text"
          icon={<ReloadOutlined />}
          style={{ marginLeft: 'auto', color: 'white' }}
          onClick={refresh}
        />
      </Header>
      <Content style={{ padding: 24, maxWidth: 1100, margin: '0 auto', width: '100%' }}>
        {error && <Alert type="error" message={error} style={{ marginBottom: 16 }} showIcon />}
        {!resources && !error && <Spin />}
        {CATEGORY_ORDER.map(
          (cat) =>
            grouped[cat] && (
              <div key={cat} style={{ marginBottom: 32 }}>
                <Title level={4}>{CATEGORY_LABEL[cat]}</Title>
                <Row gutter={16}>
                  {grouped[cat].map((r) => (
                    <Col xs={24} md={12} key={r.id}>
                      <ResourceCard resource={r} onChanged={refresh} />
                    </Col>
                  ))}
                </Row>
              </div>
            ),
        )}
      </Content>
    </Layout>
  )
}

export default App
