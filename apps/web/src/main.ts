import { createApp } from 'vue'

import App from './App.vue'
import { nexusClientKey } from './api/clientContext'
import { MockSemanticNexusClient } from './api/mockSemanticNexusClient'
import { createNexusRouter } from './router'
import './styles.css'

const app = createApp(App)
const router = createNexusRouter()

app.provide(nexusClientKey, new MockSemanticNexusClient())
app.use(router)
app.mount('#app')
