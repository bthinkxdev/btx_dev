const PQueue = require('p-queue').default;

function randomInt(min, max) {
  const a = Math.max(0, Number(min) || 0);
  const b = Math.max(a, Number(max) || a);
  return Math.floor(a + Math.random() * (b - a + 1));
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

class SessionSendQueue {
  constructor({ concurrency = 1, minDelayMs = 1200, maxDelayMs = 3500, logger }) {
    this.logger = logger;
    this.queue = new PQueue({ concurrency });
    this.minDelayMs = minDelayMs;
    this.maxDelayMs = maxDelayMs;
  }

  async add(task, meta = {}) {
    return this.queue.add(async () => {
      const delay = randomInt(this.minDelayMs, this.maxDelayMs);
      await sleep(delay);
      return task();
    }).catch((err) => {
      if (this.logger) this.logger.error('SendQueue task failed', { err: String(err), ...meta });
      throw err;
    });
  }

  size() {
    return this.queue.size;
  }
}

module.exports = { SessionSendQueue };

