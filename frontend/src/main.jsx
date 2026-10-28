import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './index.css'

class RootErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    console.error('RootErrorBoundary caught error:', error, info)
  }

  render() {
    if (this.state.error) {
      return (
        <div className="auth-screen">
          <div className="glass-card auth-card" style={{ maxWidth: '540px' }}>
            <h2 style={{ color: '#f87171', marginBottom: '0.75rem' }}>Something went wrong</h2>
            <p style={{ color: 'var(--text-muted)', fontSize: '0.9rem', marginBottom: '1rem' }}>
              {this.state.error?.message || String(this.state.error)}
            </p>
            <button
              type="button"
              className="btn-primary"
              onClick={() => {
                try {
                  window.sessionStorage.clear()
                } catch {}
                window.location.reload()
              }}
            >
              Reset Session &amp; Reload
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <RootErrorBoundary>
      <App />
    </RootErrorBoundary>
  </React.StrictMode>
)
