import { createApp } from 'vue'

import App from './App.vue'
import { nexusClientKey } from './api/clientContext'
import { createSemanticNexusClient } from './api/createSemanticNexusClient'
import { createNexusRouter } from './router'
import './styles.css'

const app = createApp(App)
const router = createNexusRouter()

app.provide(nexusClientKey, createSemanticNexusClient())
app.use(router)
app.mount('#app')
