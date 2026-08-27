import { useEffect, useRef, useState } from 'react'
import {
  Card,
  CardHeader,
  CardContent,
  Typography,
  Chip,
  Button,
  IconButton,
  Stack,
  Box,
  Accordion,
  AccordionSummary,
  AccordionDetails,
  Select,
  MenuItem,
  TextField,
  Slider,
  List,
  ListItem,
  ListItemText,
  Popover,
} from '@mui/material'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import ContentCopyIcon from '@mui/icons-material/ContentCopy'
import DeleteIcon from '@mui/icons-material/Delete'
import RefreshIcon from '@mui/icons-material/Refresh'
import AddIcon from '@mui/icons-material/Add'
import OpenInNewIcon from '@mui/icons-material/OpenInNew'
import {
  deleteInstance,
  rotateInstance,
  updatePort,
  logsSocketUrl,
  setChaosConfig,
  listRoutes,
  createRoute,
  deleteRoute,
  listWebhookRequests,
  clearWebhookRequests,
} from './api'
import { toast } from './toast'

const STATE_COLOR = {
  running: 'success',
  stopped: 'default',
  exited: 'default',
  created: 'warning',
}

const PATH_SUFFIX = { 'rest-api': '/items', 'mcp-server': '/mcp' }

const KIND_LABEL = {
  'rest-api': 'REST API',
  'mcp-server': 'MCP Server',
  'mock-api': 'Mock API',
  'webhook-receiver': 'Webhook Receiver',
  'chaos-api': 'Rate Limit / Chaos API',
  'api-tester': 'API Tester',
}

function Code({ text, sx }) {
  const copy = () => {
    navigator.clipboard.writeText(text)
    toast.success('copied')
  }
  return (
    <Box component="span" sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.3, verticalAlign: 'middle', ...sx }}>
      <Box
        component="code"
        sx={{
          bgcolor: '#f0f0f0',
          px: 0.8,
          py: 0.2,
          borderRadius: 1,
          fontFamily: 'monospace',
          fontSize: 12,
          wordBreak: 'break-all',
        }}
      >
        {text}
      </Box>
      <IconButton size="small" onClick={copy} sx={{ p: 0.3 }}>
        <ContentCopyIcon sx={{ fontSize: 14 }} />
      </IconButton>
    </Box>
  )
}

function Snippet({ text }) {
  return (
    <Box
      component="pre"
      sx={{ bgcolor: '#f5f5f5', p: 1, borderRadius: 1, overflowX: 'auto', fontSize: 12, m: 0 }}
    >
      {text}
    </Box>
  )
}

function Field({ label, children }) {
  return (
    <Typography variant="body2" sx={{ mb: 0.5 }}>
      <Typography component="span" variant="body2" color="text.secondary">
        {label}:
      </Typography>{' '}
      {children}
    </Typography>
  )
}

function portFromUrl(url) {
  if (!url) return ''
  try {
    return new URL(url).port
  } catch {
    return ''
  }
}

function PortEditor({ instance, onChanged }) {
  const currentPort = portFromUrl(instance.url)
  const [value, setValue] = useState(currentPort)
  const [updating, setUpdating] = useState(false)

  // Stay in sync with the instance's real port -- including right after a
  // successful update, at which point there's nothing left to apply and the
  // button should go back to disabled rather than staying "armed".
  useEffect(() => {
    if (!updating) setValue(currentPort)
  }, [currentPort, updating])

  const changed = value !== '' && String(value) !== String(currentPort)
  // Disabled for the entire in-flight window: the PUT doesn't resolve until
  // the recreated container is confirmed live, so "updating" alone already
  // means "grayed out until the service is live again".
  const disabled = updating || !changed || instance.state !== 'running'

  const apply = async () => {
    setUpdating(true)
    try {
      await updatePort(instance.id, Number(value))
      await onChanged()
      toast.success('port updated')
    } catch (err) {
      toast.error(err.message)
    } finally {
      setUpdating(false)
    }
  }

  return (
    <Stack direction="row" spacing={1} alignItems="center">
      <TextField
        size="small"
        label="Port"
        type="number"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        disabled={updating}
        sx={{ width: 110 }}
      />
      <Button size="small" variant="outlined" disabled={disabled} onClick={apply}>
        {updating ? 'Updating…' : 'Update'}
      </Button>
    </Stack>
  )
}

function buildSnippet(instance) {
  const { kind, auth, url } = instance
  if (!auth || !url) return null
  const target = `${url}${PATH_SUFFIX[kind] || ''}`
  // rest-api's /items also accepts POST ?name=... to add an item -- append
  // that example using whichever auth args the read command above used.
  const postCmd = (authArgs = '') =>
    kind === 'rest-api' ? `\ncurl ${authArgs}-X POST "${target}?name=myitem"` : ''

  if (auth.mode === 'none') return `curl ${target}${postCmd()}`
  if (auth.mode === 'apikey') {
    const args = `-H "X-API-Key: ${auth.api_key}" `
    return `curl ${args}${target}${postCmd(args)}`
  }
  if (auth.mode === 'basic') {
    const args = `-u ${auth.username}:${auth.password} `
    return `curl ${args}${target}${postCmd(args)}`
  }
  if (auth.mode === 'jwt') {
    const args = `-H "Authorization: Bearer ${auth.token}" `
    return `curl ${args}${target}${postCmd(args)}`
  }
  if (auth.mode === 'session') {
    const loginCmd = `curl -s -c cookies.txt -X POST ${auth.login_url} \\\n  -H "Content-Type: application/json" -d '{"username":"${auth.username}","password":"${auth.password}"}'`
    const useCmd = `curl -b cookies.txt ${target}`
    return `${loginCmd}\n${useCmd}${postCmd('-b cookies.txt ')}`
  }
  if (auth.mode === 'oauth') {
    const tokenCmd = `TOKEN=$(curl -s -X POST ${auth.token_endpoint} \\\n  -d "grant_type=client_credentials&client_id=${auth.client_id}&client_secret=${auth.client_secret}" \\\n  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")`
    const useCmd = `curl -H "Authorization: Bearer $TOKEN" ${target}`
    return `${tokenCmd}\n${useCmd}${postCmd('-H "Authorization: Bearer $TOKEN" ')}`
  }
  return null
}

function AuthDetails({ instance }) {
  const { auth } = instance
  if (!auth) return null

  return (
    <Box>
      {auth.mode === 'apikey' && (
        <Box sx={{ mb: 1 }}>
          <Field label="Header">
            <Code text={auth.header} />
          </Field>
          <Field label="Key">
            <Code text={auth.api_key} />
          </Field>
        </Box>
      )}
      {auth.mode === 'basic' && (
        <Box sx={{ mb: 1 }}>
          <Field label="Username">
            <Code text={auth.username} />
          </Field>
          <Field label="Password">
            <Code text={auth.password} />
          </Field>
        </Box>
      )}
      {auth.mode === 'jwt' && (
        <Box sx={{ mb: 1 }}>
          <Typography variant="body2" color="text.secondary">
            Token (self-contained, verified locally, no external calls):
          </Typography>
          <Code text={auth.token} sx={{ display: 'block', mb: 0.5 }} />
          <Field label="Get a fresh one anytime">
            <Code text={auth.debug_token_url} />
          </Field>
        </Box>
      )}
      {auth.mode === 'session' && (
        <Box sx={{ mb: 1 }}>
          <Field label="Login">
            <Code text={auth.login_url} />
          </Field>
          <Field label="Username">
            <Code text={auth.username} />
          </Field>
          <Field label="Password">
            <Code text={auth.password} />
          </Field>
          <Field label="Logout">
            <Code text={auth.logout_url} />
          </Field>
        </Box>
      )}
      {auth.mode === 'oauth' && (
        <Box sx={{ mb: 1 }}>
          <Field label="Token endpoint">
            <Code text={auth.token_endpoint} />
          </Field>
          <Field label="Client ID">
            <Code text={auth.client_id} />
          </Field>
          <Field label="Client secret">
            <Code text={auth.client_secret} />
          </Field>
        </Box>
      )}
      {instance.url && (
        <Field label="URL">
          <Code text={`${instance.url}${PATH_SUFFIX[instance.kind] || ''}`} />
        </Field>
      )}
      {buildSnippet(instance) && (
        <Box sx={{ mt: 1 }}>
          <Typography variant="body2" color="text.secondary">
            Try it:
          </Typography>
          <Snippet text={buildSnippet(instance)} />
        </Box>
      )}
    </Box>
  )
}

function OpenApiDetails({ instance }) {
  const openapi = instance.openapi
  if (!openapi) return null
  return (
    <Box sx={{ mt: 1, pt: 1, borderTop: '1px solid #f0f0f0' }}>
      <Typography variant="subtitle2">OpenAPI ({openapi.version})</Typography>
      <Field label="Spec">
        <Code text={openapi.spec_url} />
      </Field>
      <Field label="Swagger UI">
        <Code text={openapi.docs_url} />
      </Field>
      <Field label="ReDoc">
        <Code text={openapi.redoc_url} />
      </Field>
      {openapi.protected ? (
        <Box sx={{ mt: 0.5 }}>
          <Chip size="small" color="warning" label="spec protected" sx={{ mb: 0.5 }} />
          <Field label="Header">
            <Code text={openapi.auth.header} />
          </Field>
          <Field label="Token">
            <Code text={openapi.auth.token} />
          </Field>
          <Snippet text={`curl -H "${openapi.auth.header}: ${openapi.auth.token}" ${openapi.spec_url}`} />
        </Box>
      ) : (
        <Chip size="small" label="spec open" sx={{ mt: 0.5 }} />
      )}
    </Box>
  )
}

function AsyncJobDetails({ instance }) {
  const jobs = instance.async_jobs
  if (!jobs) return null
  return (
    <Box sx={{ mt: 1, pt: 1, borderTop: '1px solid #f0f0f0' }}>
      <Typography variant="subtitle2">Async jobs ({jobs.delay_seconds}s delay)</Typography>
      <Field label="Submit">
        <Code text={`POST ${jobs.submit_url}`} />
      </Field>
      <Field label="Poll">
        <Code text={`GET ${jobs.poll_url_template}`} />
      </Field>
      <Snippet text={`curl -X POST ${jobs.submit_url} -d '{"hello":"world"}'`} />
    </Box>
  )
}

function ChaosPanel({ instance, onChanged }) {
  const chaos = instance.chaos
  const [mode, setMode] = useState(chaos?.mode ?? 'normal')
  const [rateLimit, setRateLimit] = useState(chaos?.rate_limit ?? { limit: 5, window_seconds: 10 })
  const [chaosCfg, setChaosCfg] = useState(
    chaos?.chaos ?? { status_code: 200, body: { ok: true }, latency_ms: 0, failure_rate: 0 }
  )
  const [bodyText, setBodyText] = useState(JSON.stringify(chaosCfg.body))
  const [saving, setSaving] = useState(false)

  const save = async () => {
    let body
    try {
      body = JSON.parse(bodyText)
    } catch {
      toast.error('response body must be valid JSON')
      return
    }
    setSaving(true)
    try {
      await setChaosConfig(instance.id, { mode, rate_limit: rateLimit, chaos: { ...chaosCfg, body } })
      await onChanged()
      toast.success('config updated')
    } catch (err) {
      toast.error(err.message)
    } finally {
      setSaving(false)
    }
  }

  if (!chaos) return <Typography color="text.secondary">loading config…</Typography>

  return (
    <Stack spacing={2}>
      <Typography variant="body2" color="text.secondary">
        Test endpoint: <Code text={`${instance.url}/test`} />
      </Typography>

      <Select size="small" value={mode} onChange={(e) => setMode(e.target.value)}>
        <MenuItem value="normal">Normal (always 200)</MenuItem>
        <MenuItem value="rate_limit">Rate limit</MenuItem>
        <MenuItem value="chaos">Chaos (custom status/body/latency)</MenuItem>
      </Select>

      {mode === 'rate_limit' && (
        <Stack direction="row" spacing={2}>
          <TextField
            size="small"
            label="Requests allowed"
            type="number"
            value={rateLimit.limit}
            onChange={(e) => setRateLimit((r) => ({ ...r, limit: Number(e.target.value) }))}
          />
          <TextField
            size="small"
            label="Per (seconds)"
            type="number"
            value={rateLimit.window_seconds}
            onChange={(e) => setRateLimit((r) => ({ ...r, window_seconds: Number(e.target.value) }))}
          />
        </Stack>
      )}

      {mode === 'chaos' && (
        <>
          <Stack direction="row" spacing={2}>
            <TextField
              size="small"
              label="Status code"
              type="number"
              value={chaosCfg.status_code}
              onChange={(e) => setChaosCfg((c) => ({ ...c, status_code: Number(e.target.value) }))}
            />
            <TextField
              size="small"
              label="Latency (ms)"
              type="number"
              value={chaosCfg.latency_ms}
              onChange={(e) => setChaosCfg((c) => ({ ...c, latency_ms: Number(e.target.value) }))}
            />
          </Stack>
          <TextField
            size="small"
            label="Response body (JSON)"
            multiline
            minRows={2}
            value={bodyText}
            onChange={(e) => setBodyText(e.target.value)}
          />
          <Box>
            <Typography variant="body2" color="text.secondary">
              Random failure rate: {chaosCfg.failure_rate}
            </Typography>
            <Slider
              size="small"
              min={0}
              max={1}
              step={0.05}
              value={chaosCfg.failure_rate}
              onChange={(_, v) => setChaosCfg((c) => ({ ...c, failure_rate: v }))}
            />
          </Box>
        </>
      )}

      <Button size="small" variant="contained" disabled={saving} onClick={save} sx={{ alignSelf: 'flex-start' }}>
        Save
      </Button>
    </Stack>
  )
}

function MockRoutesPanel({ instanceId, active, onChanged }) {
  const [routes, setRoutes] = useState([])
  const [method, setMethod] = useState('*')
  const [path, setPath] = useState('*')
  const [statusCode, setStatusCode] = useState(200)
  const [responseBody, setResponseBody] = useState('{"ok": true}')
  const [requiredFields, setRequiredFields] = useState('')
  const [busy, setBusy] = useState(false)

  const load = async () => {
    try {
      setRoutes(await listRoutes(instanceId))
    } catch (err) {
      toast.error(err.message)
    }
  }

  useEffect(() => {
    if (active) load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active])

  const addRoute = async () => {
    let body
    try {
      body = JSON.parse(responseBody)
    } catch {
      toast.error('response body must be valid JSON')
      return
    }
    setBusy(true)
    try {
      await createRoute(instanceId, {
        method,
        path,
        status_code: statusCode,
        response_body: body,
        required_fields: requiredFields
          ? requiredFields.split(',').map((s) => s.trim()).filter(Boolean)
          : [],
      })
      setPath('*')
      setResponseBody('{"ok": true}')
      setRequiredFields('')
      await load()
      await onChanged()
    } catch (err) {
      toast.error(err.message)
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
    <Box>
      {routes.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          No routes yet -- add one below.
        </Typography>
      ) : (
        <List dense disablePadding>
          {routes.map((r) => (
            <ListItem
              key={r.id}
              disableGutters
              secondaryAction={
                <IconButton size="small" onClick={() => remove(r.id)}>
                  <DeleteIcon fontSize="small" color="error" />
                </IconButton>
              }
            >
              <ListItemText
                primary={
                  <>
                    <Box component="code" sx={{ fontFamily: 'monospace', fontSize: 13 }}>
                      {r.method} {r.path}
                    </Box>{' '}
                    <Chip size="small" label={r.status_code} />
                  </>
                }
              />
            </ListItem>
          ))}
        </List>
      )}

      <Stack spacing={1.5} sx={{ mt: 1.5 }}>
        <Stack direction="row" spacing={1} flexWrap="wrap">
          <Select size="small" value={method} onChange={(e) => setMethod(e.target.value)} sx={{ minWidth: 90 }}>
            {['*', 'GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map((m) => (
              <MenuItem key={m} value={m}>
                {m}
              </MenuItem>
            ))}
          </Select>
          <TextField
            size="small"
            label="Path (or *)"
            value={path}
            onChange={(e) => setPath(e.target.value)}
            sx={{ width: 160 }}
          />
          <TextField
            size="small"
            label="Status"
            type="number"
            value={statusCode}
            onChange={(e) => setStatusCode(Number(e.target.value))}
            sx={{ width: 90 }}
          />
        </Stack>
        <TextField
          size="small"
          label="Response body (JSON, supports {{request.body.x}}, {{uuid}}, {{now}})"
          multiline
          minRows={2}
          value={responseBody}
          onChange={(e) => setResponseBody(e.target.value)}
        />
        <TextField
          size="small"
          label="Required request body fields (comma-separated, optional)"
          placeholder="name, email"
          value={requiredFields}
          onChange={(e) => setRequiredFields(e.target.value)}
        />
        <Button size="small" variant="contained" startIcon={<AddIcon />} disabled={busy} onClick={addRoute} sx={{ alignSelf: 'flex-start' }}>
          Add route
        </Button>
      </Stack>
    </Box>
  )
}

function WebhookRequestsPanel({ instanceId, active }) {
  const [requests, setRequests] = useState([])

  const load = async () => {
    try {
      setRequests(await listWebhookRequests(instanceId))
    } catch (err) {
      toast.error(err.message)
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
    <Box>
      <Button size="small" onClick={clear} sx={{ mb: 1 }}>
        Clear
      </Button>
      {requests.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          No requests received yet
        </Typography>
      ) : (
        <List dense disablePadding>
          {[...requests].reverse().map((r, i) => (
            <ListItem key={i} disableGutters sx={{ display: 'block' }}>
              <Box component="code" sx={{ fontFamily: 'monospace', fontSize: 13 }}>
                {r.method} {r.path}
              </Box>{' '}
              <Typography component="span" variant="caption" color="text.secondary">
                {r.received_at}
              </Typography>
              <Box component="pre" sx={{ bgcolor: '#f5f5f5', p: 0.75, borderRadius: 1, fontSize: 11, m: '4px 0 0' }}>
                {JSON.stringify(r.body_json ?? r.body_text, null, 2)}
              </Box>
            </ListItem>
          ))}
        </List>
      )}
    </Box>
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
    <Box
      ref={boxRef}
      sx={{
        bgcolor: '#111',
        color: '#0f0',
        fontFamily: 'monospace',
        fontSize: 12,
        p: 1,
        height: 200,
        overflowY: 'auto',
        borderRadius: 1,
        whiteSpace: 'pre-wrap',
      }}
    >
      {lines.length === 0 ? 'waiting for log output…' : lines.join('')}
    </Box>
  )
}

function DeleteConfirmButton({ onConfirm, loading }) {
  const [anchorEl, setAnchorEl] = useState(null)
  const open = Boolean(anchorEl)

  const confirm = () => {
    setAnchorEl(null)
    onConfirm()
  }

  return (
    <>
      <IconButton size="small" color="error" onClick={(e) => setAnchorEl(e.currentTarget)} disabled={loading}>
        <DeleteIcon fontSize="small" />
      </IconButton>
      <Popover
        open={open}
        anchorEl={anchorEl}
        onClose={() => setAnchorEl(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
      >
        <Box sx={{ p: 2 }}>
          <Typography variant="body2" sx={{ mb: 1 }}>
            Stop and remove this instance?
          </Typography>
          <Stack direction="row" spacing={1} justifyContent="flex-end">
            <Button size="small" onClick={() => setAnchorEl(null)}>
              Cancel
            </Button>
            <Button size="small" color="error" variant="contained" onClick={confirm}>
              Delete
            </Button>
          </Stack>
        </Box>
      </Popover>
    </>
  )
}

export default function InstanceCard({ instance, kinds, onChanged }) {
  const [busy, setBusy] = useState(false)
  const [openPanel, setOpenPanel] = useState(null)

  const kindDef = kinds?.find((k) => k.id === instance.kind)
  const hasOwnUi = kindDef?.has_own_ui

  const canRotate =
    ['apikey', 'basic', 'jwt', 'session', 'oauth'].includes(instance.auth?.mode) || instance.openapi?.protected

  const rotate = async () => {
    setBusy(true)
    try {
      await rotateInstance(instance.id)
      await onChanged()
      toast.success('credentials rotated')
    } catch (err) {
      toast.error(err.message)
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
      toast.error(err.message)
      setBusy(false)
    }
  }

  const panels = []
  if (instance.kind === 'chaos-api') {
    panels.push({ key: 'chaos', label: 'Configure', content: <ChaosPanel instance={instance} onChanged={onChanged} /> })
  }
  if (instance.kind === 'mock-api') {
    panels.push({
      key: 'routes',
      label: `Routes (${instance.mock_routes?.length ?? 0})`,
      content: <MockRoutesPanel instanceId={instance.id} active={openPanel === 'routes'} onChanged={onChanged} />,
    })
  }
  if (instance.kind === 'webhook-receiver') {
    panels.push({
      key: 'requests',
      label: 'Received requests',
      content: <WebhookRequestsPanel instanceId={instance.id} active={openPanel === 'requests'} />,
    })
  }
  panels.push({ key: 'logs', label: 'Logs', content: <LogsPanel id={instance.id} active={openPanel === 'logs'} /> })

  return (
    <Card variant="outlined">
      <CardHeader
        title={instance.name}
        titleTypographyProps={{ variant: 'subtitle1', fontWeight: 600 }}
        action={<DeleteConfirmButton onConfirm={remove} loading={busy} />}
      />
      <CardContent sx={{ pt: 0 }}>
        <Stack spacing={1.5}>
          <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
            <Chip size="small" color={STATE_COLOR[instance.state] || 'default'} label={instance.state} />
            <Chip size="small" color="info" label={KIND_LABEL[instance.kind] || instance.kind} />
            {instance.auth && <Chip size="small" label={instance.auth.mode} />}
            <Typography variant="caption" color="text.secondary">
              {instance.id}
            </Typography>
          </Stack>

          <PortEditor instance={instance} onChanged={onChanged} />

          {instance.internal_url && (
            <Field label="Internal URL (for other sandbox-hub containers, e.g. the API Tester)">
              <Code text={instance.internal_url} />
            </Field>
          )}

          {hasOwnUi ? (
            <Button
              variant="contained"
              startIcon={<OpenInNewIcon />}
              component="a"
              href={instance.url}
              target="_blank"
              rel="noopener"
              sx={{ alignSelf: 'flex-start' }}
            >
              Open API Tester
            </Button>
          ) : (
            <>
              <AuthDetails instance={instance} />
              <OpenApiDetails instance={instance} />
              <AsyncJobDetails instance={instance} />
            </>
          )}

          {canRotate && (
            <Button size="small" startIcon={<RefreshIcon />} onClick={rotate} disabled={busy} sx={{ alignSelf: 'flex-start' }}>
              Rotate credentials
            </Button>
          )}

          {panels.map((p) => (
            <Accordion
              key={p.key}
              disableGutters
              expanded={openPanel === p.key}
              onChange={(_, isExpanded) => setOpenPanel(isExpanded ? p.key : null)}
            >
              <AccordionSummary expandIcon={<ExpandMoreIcon />}>
                <Typography variant="body2">{p.label}</Typography>
              </AccordionSummary>
              <AccordionDetails>{p.content}</AccordionDetails>
            </Accordion>
          ))}
        </Stack>
      </CardContent>
    </Card>
  )
}
