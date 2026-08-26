import { useState } from 'react'
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Button,
  TextField,
  MenuItem,
  FormControlLabel,
  Switch,
  Typography,
  Stack,
} from '@mui/material'
import { createInstance } from './api'
import { toast } from './toast'

const AUTH_LABELS = {
  none: 'No auth',
  apikey: 'Static API key',
  basic: 'HTTP Basic',
  jwt: 'Self-contained JWT',
  session: 'Cookie / session login',
  oauth: 'OAuth2',
}

const EMPTY_FORM = {
  kind: '',
  name: '',
  auth_mode: 'none',
  openapi_version: '3.1',
  openapi_protect: false,
  async_jobs: false,
  async_job_delay_seconds: 5,
}

export default function NewInstanceModal({ open, kinds, authModes, onClose, onCreated }) {
  const [values, setValues] = useState(EMPTY_FORM)
  const [submitting, setSubmitting] = useState(false)
  const kind = kinds.find((k) => k.id === values.kind)

  const set = (field) => (e) => {
    const v = e?.target?.type === 'checkbox' ? e.target.checked : e?.target?.value
    setValues((prev) => ({ ...prev, [field]: v }))
  }

  const handleClose = () => {
    setValues(EMPTY_FORM)
    onClose()
  }

  const submit = async () => {
    if (!values.kind) {
      toast.error('pick a kind')
      return
    }
    setSubmitting(true)
    try {
      const payload = { kind: values.kind, name: values.name || undefined, auth_mode: values.auth_mode }
      if (kind?.supports_openapi) {
        payload.openapi_version = values.openapi_version
        payload.openapi_protect = values.openapi_protect
      }
      if (kind?.supports_async_job) {
        payload.async_jobs = values.async_jobs
        if (values.async_jobs) payload.async_job_delay_seconds = Number(values.async_job_delay_seconds)
      }
      await createInstance(payload)
      toast.success('started')
      setValues(EMPTY_FORM)
      onCreated()
      onClose()
    } catch (err) {
      toast.error(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog open={open} onClose={handleClose} fullWidth maxWidth="sm">
      <DialogTitle>New resource</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          <TextField select label="Kind" value={values.kind} onChange={set('kind')} required>
            {kinds.map((k) => (
              <MenuItem key={k.id} value={k.id}>
                {k.label}
              </MenuItem>
            ))}
          </TextField>

          {kind && (
            <>
              <Typography variant="body2" color="text.secondary">
                {kind.description}
                {(kind.supports_routes || kind.supports_chaos_config) &&
                  ' Further settings are configured on the card after you start it.'}
              </Typography>

              <TextField
                label="Name (optional)"
                value={values.name}
                onChange={set('name')}
                placeholder={`e.g. "${kind.label} for testing my client"`}
              />

              {kind.supports_auth && (
                <TextField select label="Auth" value={values.auth_mode} onChange={set('auth_mode')}>
                  {authModes.map((m) => (
                    <MenuItem key={m} value={m}>
                      {AUTH_LABELS[m] || m}
                    </MenuItem>
                  ))}
                </TextField>
              )}

              {kind.supports_openapi && (
                <>
                  <TextField
                    select
                    label="OpenAPI spec version"
                    value={values.openapi_version}
                    onChange={set('openapi_version')}
                  >
                    <MenuItem value="3.1">3.1</MenuItem>
                    <MenuItem value="3.0">3.0</MenuItem>
                  </TextField>
                  <FormControlLabel
                    control={<Switch checked={values.openapi_protect} onChange={set('openapi_protect')} />}
                    label="Protect the OpenAPI spec / docs behind a static token"
                  />
                </>
              )}

              {kind.supports_async_job && (
                <>
                  <FormControlLabel
                    control={<Switch checked={values.async_jobs} onChange={set('async_jobs')} />}
                    label="Enable async job endpoint (POST /jobs, GET /jobs/{id})"
                  />
                  {values.async_jobs && (
                    <TextField
                      label="Job delay (seconds)"
                      type="number"
                      value={values.async_job_delay_seconds}
                      onChange={set('async_job_delay_seconds')}
                    />
                  )}
                </>
              )}
            </>
          )}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={handleClose}>Cancel</Button>
        <Button variant="contained" onClick={submit} disabled={submitting}>
          Start
        </Button>
      </DialogActions>
    </Dialog>
  )
}
