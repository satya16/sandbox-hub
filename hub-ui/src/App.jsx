import { useEffect, useState, useCallback } from 'react'
import {
  AppBar,
  Toolbar,
  Typography,
  Box,
  Container,
  Grid,
  CircularProgress,
  Alert,
  Button,
  IconButton,
  Chip,
  Snackbar,
} from '@mui/material'
import AddIcon from '@mui/icons-material/Add'
import RefreshIcon from '@mui/icons-material/Refresh'
import InstanceCard from './InstanceCard'
import NewInstanceModal from './NewInstanceModal'
import { listInstances, getKinds, getOauthProviderStatus } from './api'
import { setToastListener } from './toast'

function App() {
  const [instances, setInstances] = useState(null)
  const [kinds, setKinds] = useState([])
  const [authModes, setAuthModes] = useState([])
  const [oauthStatus, setOauthStatus] = useState(null)
  const [error, setError] = useState(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [toastState, setToastState] = useState({ open: false, severity: 'success', text: '' })

  useEffect(() => {
    setToastListener((severity, text) => setToastState({ open: true, severity, text }))
  }, [])

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
    getKinds().then((d) => {
      setKinds(d.kinds)
      setAuthModes(d.auth_modes)
    })
    refresh()
    const interval = setInterval(refresh, 5000)
    return () => clearInterval(interval)
  }, [refresh])

  return (
    <Box sx={{ minHeight: '100vh', bgcolor: '#f5f5f5' }}>
      <AppBar position="static" color="default" enableColorOnDark sx={{ bgcolor: '#0a0f1e' }}>
        <Toolbar sx={{ gap: 2 }}>
          <Typography variant="h6" sx={{ color: 'white', fontWeight: 700 }}>
            sandbox-hub
          </Typography>
          <Typography variant="body2" sx={{ color: 'rgba(255,255,255,0.65)' }}>
            local test resources, on demand
          </Typography>
          {oauthStatus?.state === 'running' && (
            <Chip
              size="small"
              color="secondary"
              label={`oauth-provider running at ${oauthStatus.url}`}
            />
          )}
          <Box sx={{ flexGrow: 1 }} />
          <Button variant="contained" startIcon={<AddIcon />} onClick={() => setModalOpen(true)}>
            New
          </Button>
          <IconButton onClick={refresh} sx={{ color: 'white' }}>
            <RefreshIcon />
          </IconButton>
        </Toolbar>
      </AppBar>

      <Container maxWidth="lg" sx={{ py: 3 }}>
        {error && (
          <Alert severity="error" sx={{ mb: 2 }}>
            {error}
          </Alert>
        )}
        {!instances && !error && (
          <Box sx={{ display: 'flex', justifyContent: 'center', mt: 8 }}>
            <CircularProgress />
          </Box>
        )}
        {instances && instances.length === 0 && (
          <Box sx={{ textAlign: 'center', mt: 8, color: 'text.secondary' }}>
            <Typography>
              Nothing running yet. Click New to start a REST API, MCP server, mock API, webhook
              receiver, or chaos/rate-limit endpoint.
            </Typography>
          </Box>
        )}
        <Grid container spacing={2}>
          {(instances || []).map((inst) => (
            <Grid key={inst.id} size={{ xs: 12, md: 6 }}>
              <InstanceCard instance={inst} kinds={kinds} onChanged={refresh} />
            </Grid>
          ))}
        </Grid>
      </Container>

      <NewInstanceModal
        open={modalOpen}
        kinds={kinds}
        authModes={authModes}
        onClose={() => setModalOpen(false)}
        onCreated={refresh}
      />

      <Snackbar
        open={toastState.open}
        autoHideDuration={4000}
        onClose={() => setToastState((s) => ({ ...s, open: false }))}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
      >
        <Alert
          severity={toastState.severity}
          variant="filled"
          onClose={() => setToastState((s) => ({ ...s, open: false }))}
        >
          {toastState.text}
        </Alert>
      </Snackbar>
    </Box>
  )
}

export default App
