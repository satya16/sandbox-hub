// Minimal global toast bus so any component can call toast.success/error
// without prop-drilling, mirroring antd's `message` API ergonomics on top
// of MUI's Snackbar (which has no built-in imperative/global API).
let listener = null

export function setToastListener(fn) {
  listener = fn
}

function emit(severity, text) {
  if (listener) listener(severity, text)
}

export const toast = {
  success: (text) => emit('success', text),
  error: (text) => emit('error', text),
}
