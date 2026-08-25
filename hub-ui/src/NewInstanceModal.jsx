import { useState } from 'react'
import { Modal, Form, Select, Input, Switch, Button, message, Typography } from 'antd'
import { createInstance } from './api'

const { Text } = Typography

const AUTH_LABELS = {
  none: 'No auth',
  apikey: 'Static API key',
  basic: 'HTTP Basic',
  jwt: 'Self-contained JWT',
  session: 'Cookie / session login',
  oauth: 'OAuth2',
}

export default function NewInstanceModal({ open, kinds, authModes, onClose, onCreated }) {
  const [form] = Form.useForm()
  const [submitting, setSubmitting] = useState(false)
  const kindId = Form.useWatch('kind', form)
  const kind = kinds.find((k) => k.id === kindId)

  const submit = async () => {
    try {
      const values = await form.validateFields()
      setSubmitting(true)
      await createInstance(values)
      message.success('started')
      form.resetFields()
      onCreated()
      onClose()
    } catch (err) {
      if (err?.errorFields) return // antd validation error, already shown inline
      message.error(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal
      title="New resource"
      open={open}
      onCancel={onClose}
      footer={[
        <Button key="cancel" onClick={onClose}>
          Cancel
        </Button>,
        <Button key="start" type="primary" loading={submitting} onClick={submit}>
          Start
        </Button>,
      ]}
      destroyOnHidden
    >
      <Form
        form={form}
        layout="vertical"
        initialValues={{ auth_mode: 'none', openapi_version: '3.1', openapi_protect: false }}
      >
        <Form.Item name="kind" label="Kind" rules={[{ required: true, message: 'pick a kind' }]}>
          <Select placeholder="Select what to run">
            {kinds.map((k) => (
              <Select.Option key={k.id} value={k.id}>
                {k.label}
              </Select.Option>
            ))}
          </Select>
        </Form.Item>

        {kind && (
          <>
            <Text type="secondary" style={{ display: 'block', marginTop: -12, marginBottom: 16 }}>
              {kind.description}
              {(kind.supports_routes || kind.supports_chaos_config) &&
                ' Further settings are configured on the card after you start it.'}
            </Text>

            <Form.Item name="name" label="Name (optional)">
              <Input placeholder={`e.g. "${kind.label} for testing my client"`} />
            </Form.Item>

            {kind.supports_auth && (
              <Form.Item name="auth_mode" label="Auth">
                <Select>
                  {authModes.map((m) => (
                    <Select.Option key={m} value={m}>
                      {AUTH_LABELS[m] || m}
                    </Select.Option>
                  ))}
                </Select>
              </Form.Item>
            )}

            {kind.supports_openapi && (
              <>
                <Form.Item name="openapi_version" label="OpenAPI spec version">
                  <Select>
                    <Select.Option value="3.1">3.1</Select.Option>
                    <Select.Option value="3.0">3.0</Select.Option>
                  </Select>
                </Form.Item>
                <Form.Item
                  name="openapi_protect"
                  label="Protect the OpenAPI spec / docs behind a static token"
                  valuePropName="checked"
                >
                  <Switch />
                </Form.Item>
              </>
            )}
          </>
        )}
      </Form>
    </Modal>
  )
}
