const { buildDjangoClient } = require('./djangoClient');
const { SessionSendQueue } = require('./sendQueue');
const env = require('../config/env');
const { WaSession } = require('./waSession');

class SessionManager {
  constructor({ logger, incomingLogger, outgoingLogger, sessionLogger }) {
    this.logger = logger;
    this.incomingLogger = incomingLogger;
    this.outgoingLogger = outgoingLogger;
    this.sessionLogger = sessionLogger;

    this.django = buildDjangoClient();
    this.sessions = new Map(); // id -> WaSession
    this.lastQrBySession = new Map(); // id -> qr string
  }

  listIds() {
    return Array.from(this.sessions.keys()).sort();
  }

  get(id) {
    return this.sessions.get(id);
  }

  statusAll() {
    return this.listIds().map((id) => ({
      ...this.sessions.get(id).status(),
      has_qr: Boolean(this.lastQrBySession.get(id))
    }));
  }

  getLastQr(id) {
    return this.lastQrBySession.get(String(id || '').trim()) || '';
  }

  async startAll() {
    const ids = env.waSessions;
    if (!ids.length) {
      this.logger.warn('WA_SESSIONS is empty; no sessions will start');
      return;
    }
    for (const id of ids) {
      try {
        await this.start(id);
      } catch (err) {
        this.logger.error('Session start failed (continuing)', { session: String(id), err: String(err) });
      }
    }
  }

  async start(id) {
    const sessionId = String(id || '').trim();
    if (!sessionId) throw new Error('invalid_session');
    if (this.sessions.has(sessionId)) return this.sessions.get(sessionId);

    const sendQueue = new SessionSendQueue({
      concurrency: env.sendQueueConcurrency,
      minDelayMs: env.sendMinDelayMs,
      maxDelayMs: env.sendMaxDelayMs,
      logger: this.outgoingLogger
    });

    const wa = new WaSession({
      sessionId,
      logger: this.sessionLogger,
      django: this.django,
      sendQueue,
      onQr: (qr) => {
        this.lastQrBySession.set(sessionId, String(qr || ''));
      },
      humanTakeoverCooldownMinutes: env.humanTakeoverCooldownMinutes,
      mediaMaxBytes: env.mediaMaxBytes
    });

    this.sessions.set(sessionId, wa);
    await this.django.postStatus({ session: sessionId, event: 'starting' }).catch(() => {});
    await wa.start();
    return wa;
  }

  async restart(id) {
    const sessionId = String(id || '').trim();
    const wa = this.sessions.get(sessionId);
    if (!wa) throw new Error('unknown_session');
    await wa.stop();
    this.sessions.delete(sessionId);
    return this.start(sessionId);
  }

  async logout(id) {
    const sessionId = String(id || '').trim();
    const wa = this.sessions.get(sessionId);
    if (!wa) throw new Error('unknown_session');
    await wa.logout(); // non-fatal on Windows EBUSY
    await this.django.postStatus({ session: sessionId, event: 'logout' }).catch(() => {});
    return true;
  }

  async stopAll() {
    for (const id of this.listIds()) {
      try {
        await this.sessions.get(id).stop();
      } catch (_) {}
    }
  }
}

module.exports = { SessionManager };

