const axios = require('axios');
const { djangoApiUrl, djangoApiToken } = require('../config/env');

function buildDjangoClient({ timeoutMs = 90_000 } = {}) {
  const http = axios.create({
    baseURL: djangoApiUrl,
    timeout: timeoutMs,
    headers: {
      Authorization: `Bearer ${djangoApiToken}`,
      'Content-Type': 'application/json'
    }
  });

  return {
    async postIncoming(payload) {
      const res = await http.post('/api/wa/incoming/', payload);
      return res.data;
    },
    async postStatus(payload) {
      const res = await http.post('/api/wa/status/', payload);
      return res.data;
    },
    async postOutgoingAck(payload) {
      const res = await http.post('/api/wa/outgoing/', payload);
      return res.data;
    }
  };
}

module.exports = { buildDjangoClient };

