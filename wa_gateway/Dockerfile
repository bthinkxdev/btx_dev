FROM node:20-bookworm-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    chromium \
    ca-certificates \
    fonts-liberation \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
ENV CHROME_BIN=/usr/bin/chromium

COPY package.json package-lock.json* ./
RUN npm install --omit=dev

COPY src ./src
COPY ecosystem.config.cjs ./

RUN mkdir -p /app/logs /app/sessions /app/tmp_media

EXPOSE 3001

CMD ["node", "src/index.js"]

