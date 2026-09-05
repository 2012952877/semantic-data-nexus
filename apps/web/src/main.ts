import { createApp } from 'vue'

import App from './App.vue'
import { nexusClientKey } from './api/clientContext'
import { createSemanticNexusClient } from './api/createSemanticNexusClient'
import { createNexusRouter } from './router'
import './styles.css'
import { BrowserSession, browserSessionKey } from './api/browserSession'

const app = createApp(App)
const router = createNexusRouter()

const session = import.meta.env.VITE_NEXUS_AUTH_MODE === 'oidc' ? new BrowserSession() : undefined
if (session) app.provide(browserSessionKey, session)
app.provide(nexusClientKey, createSemanticNexusClient(undefined, undefined, session))
app.use(router)
app.mount('#app')
