import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import './styles/global.css'
// 授权 UI（Phase 8.4）独立样式模块，依赖 global.css 里的令牌，必须放在其后
import './styles/license.css'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
