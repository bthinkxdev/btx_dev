const path = require('path');
const winston = require('winston');

function maskPhone(phone) {
  const raw = String(phone || '').replace(/\D/g, '');
  if (!raw) return '';
  if (raw.length <= 7) return raw.slice(0, 2) + '***';
  return `${raw.slice(0, 5)}****${raw.slice(-3)}`;
}

function buildLogger({ level = 'info', label = 'app', filename = 'app.log' }) {
  const logDir = path.resolve(process.cwd(), 'logs');
  const fmt = winston.format.printf(({ level, message, timestamp, ...meta }) => {
    const safeMeta = meta && Object.keys(meta).length ? ` ${JSON.stringify(meta)}` : '';
    return `${timestamp} ${label} ${level}: ${message}${safeMeta}`;
  });

  return winston.createLogger({
    level,
    format: winston.format.combine(
      winston.format.timestamp(),
      winston.format.errors({ stack: true })
    ),
    transports: [
      new winston.transports.File({
        filename: path.join(logDir, filename),
        format: winston.format.combine(fmt)
      }),
      new winston.transports.Console({
        format: winston.format.combine(winston.format.colorize(), fmt)
      })
    ]
  });
}

module.exports = {
  buildLogger,
  maskPhone
};

