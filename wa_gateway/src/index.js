const express = require('express');
const helmet = require('helmet');
const morgan = require('morgan');
const rateLimit = require('express-rate-limit');

const env = require('./config/env');
const { buildLogger } = require('./utils/logger');
const { SessionManager } = require('./services/sessionManager');
const { buildSendMessageRouter } = require('./routes/sendMessage');
const { buildSessionsRouter } = require('./routes/sessions');
const { buildQrRouter } = require('./routes/qr');

const logger = buildLogger({ level: env.logLevel, label: 'app', filename: 'app.log' });
const sessionsLogger = buildLogger({ level: env.logLevel, label: 'sessions', filename: 'sessions.log' });
const incomingLogger = buildLogger({ level: env.logLevel, label: 'incoming', filename: 'incoming.log' });
const outgoingLogger = buildLogger({ level: env.logLevel, label: 'outgoing', filename: 'outgoing.log' });
const errorsLogger = buildLogger({ level: env.logLevel, label: 'errors', filename: 'errors.log' });

const sessionManager = new SessionManager({
  logger,
  incomingLogger,
  outgoingLogger,
  sessionLogger: sessionsLogger
});

async function main() {
  const app = express();
  app.use(helmet());
  app.use(express.json({ limit: '2mb' }));
  app.use(morgan('combined'));

  app.use(
    rateLimit({
      windowMs: 60_000,
      max: 120,
      standardHeaders: true,
      legacyHeaders: false
    })
  );

  app.get('/health', (req, res) => res.json({ ok: true }));

  app.use(buildSendMessageRouter({ sessionManager }));
  app.use(buildSessionsRouter({ sessionManager }));
  app.use(buildQrRouter({ sessionManager, qrToken: env.bridgeToken }));

  app.use((err, req, res, next) => {
    errorsLogger.error('Unhandled error', { err: String(err) });
    return res.status(500).json({ error: 'internal_error' });
  });

  const server = app.listen(env.port, () => {
    logger.info(`WA Gateway listening on port ${env.port}`);
  });

  await sessionManager.startAll();

  const shutdown = async () => {
    logger.info('Shutting down...');
    server.close(() => {});
    await sessionManager.stopAll();
    process.exit(0);
  };

  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

main().catch((e) => {
  errorsLogger.error('Fatal startup error', { err: String(e) });
  process.exit(1);
});

