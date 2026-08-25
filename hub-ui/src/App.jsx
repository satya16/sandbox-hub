import { useEffect, useState, useCallback } from 'react'
import { Layout, Typography, Row, Col, Spin, Alert, Button, Tag, Empty } from 'antd'
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import InstanceCard from './InstanceCard'
import NewInstanceModal from './NewInstanceModal'
import { listInstances, getKinds, getOauthProviderStatus } from './api'

const { Header, Content } = Layout
const { Title, Text } = Typography

function App() {
  const [instances, setInstances] = useState(null)
  const [kinds, setKinds] = useState([])
  const [oauthStatus, setOauthStatus] = useState(null)
  const [error, setError] = useState(null)
  const [modalOpen, setModalOpen] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const [inst, oauth] = await Promise.all([listInstances(), getOauthProviderStatus()])
      setInstances(inst)
      setOauthStatus(oauth)
      setError(null)
    } catch (err) {
      setError(err.message)
    }
  }, [])

  useEffect(() => {
    getKinds().then((d) => setKinds(d.kinds))
    refresh()
    const interval = setInterval(refresh, 5000)
    return () => clearInterval(interval)
  }, [refresh])

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
        <Title level={3} style={{ color: 'white', margin: 0 }}>
          sandbox-hub
        </Title>
        <Text style={{ color: 'rgba(255,255,255,0.65)' }}>local test resources, on demand</Text>
        {oauthStatus?.state === 'running' && (
          <Tag color="purple">oauth-provider running at {oauthStatus.url}</Tag>
        )}
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>
          <Button icon={<PlusOutlined />} type="primary" onClick={() => setModalOpen(true)}>
            New
          </Button>
          <Button type="text" icon={<ReloadOutlined />} style={{ color: 'white' }} onClick={refresh} />
        </div>
      </Header>
      <Content style={{ padding: 24, maxWidth: 1100, margin: '0 auto', width: '100%' }}>
        {error && <Alert type="error" message={error} style={{ marginBottom: 16 }} showIcon />}
        {!instances && !error && <Spin />}
        {instances && instances.length === 0 && (
          <Empty description="Nothing running yet. Click New to start a REST API or MCP server." style={{ marginTop: 64 }} />
        )}
        <Row gutter={16}>
          {(instances || []).map((inst) => (
            <Col xs={24} md={12} key={inst.id}>
              <InstanceCard instance={inst} onChanged={refresh} />
            </Col>
          ))}
        </Row>
      </Content>
      <NewInstanceModal
        open={modalOpen}
        kinds={kinds}
        onClose={() => setModalOpen(false)}
        onCreated={refresh}
      />
    </Layout>
  )
}

export default App
