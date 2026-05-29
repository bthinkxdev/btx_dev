const { Client, LocalAuth, MessageMedia, Buttons, List } = require('whatsapp-web.js');
const env = require('../config/env');
const { TTLSet } = require('../utils/dedupe');
const { writeTempMedia } = require('./mediaStore');

function isTruthy(v) {
  return String(v || '').toLowerCase() in { '1': 1, true: 1, yes: 1, y: 1, on: 1 };
}

class WaSession {
  constructor({
    sessionId,
    logger,
    django,
    sendQueue,
    onQr,
    humanTakeoverCooldownMinutes = 30,
    mediaMaxBytes = 10 * 1024 * 1024
  }) {
    this.sessionId = sessionId;
    this.logger = logger;
    this.django = django;
    this.sendQueue = sendQueue;
    this.mediaMaxBytes = mediaMaxBytes;
    this.onQr = typeof onQr === 'function' ? onQr : null;

    this.dedupe = new TTLSet({ ttlMs: 24 * 60 * 60 * 1000, maxSize: 200_000 });

    this.humanCooldownMs = humanTakeoverCooldownMinutes * 60 * 1000;
    this.humanTakeoverUntilByChat = new Map(); // chatId -> epoch ms

    this.sentByGateway = new TTLSet({ ttlMs: 60 * 60 * 1000, maxSize: 100_000 }); // message ids we sent

    // Map normalized phone -> last known WhatsApp chat id (often @lid_...).
    // This lets us send replies reliably even when WhatsApp uses LID addressing.
    this.phoneToChatId = new Map();

    this.client = new Client({
      authStrategy: new LocalAuth({ clientId: sessionId }),
      puppeteer: {
        headless: env.waHeadless,
        executablePath: env.puppeteerExecutablePath || undefined,
        args: [
          '--no-sandbox',
          '--disable-setuid-sandbox',
          '--disable-dev-shm-usage',
          '--disable-gpu'
        ]
      }
    });

    this._bind();
  }

  _bind() {
    this.client.on('qr', (qr) => {
      this.logger.info(`QR generated for session=${this.sessionId}`);
      // QR is served via the web UI (/qr/:id). Terminal QR is optional.
      if (env.qrInTerminal) {
        const qrcode = require('qrcode-terminal');
        const small = String(process.env.QR_SMALL || '').trim().toLowerCase() === 'true';
        qrcode.generate(qr, { small });
      }
      if (this.onQr) this.onQr(qr);
      this._postStatus({ event: 'qr' }).catch(() => {});
    });

    this.client.on('authenticated', () => {
      this.logger.info(`Authenticated session=${this.sessionId}`);
      this._postStatus({ event: 'authenticated' }).catch(() => {});
    });

    this.client.on('auth_failure', (msg) => {
      this.logger.error(`Auth failure session=${this.sessionId}`, { msg: String(msg || '').slice(0, 200) });
      this._postStatus({ event: 'auth_failure', message: String(msg || '').slice(0, 200) }).catch(() => {});
    });

    this.client.on('ready', async () => {
      this.logger.info(`Ready session=${this.sessionId}`);
      this._postStatus({ event: 'ready' }).catch(() => {});
    });

    this.client.on('disconnected', (reason) => {
      this.logger.warn(`Disconnected session=${this.sessionId}`, { reason: String(reason || '') });
      this._postStatus({ event: 'disconnected', reason: String(reason || '') }).catch(() => {});
    });

    // Inbound customer messages
    this.client.on('message', async (msg) => {
      try {
        await this._handleInbound(msg);
      } catch (err) {
        this.logger.error('Inbound handler failed', { session: this.sessionId, err: String(err) });
      }
    });


    // message_create: human takeover (fromMe) + inbound fallback (some WA builds skip `message` for LID).
    this.client.on('message_create', async (msg) => {
      try {
        if (msg.fromMe) {
          if (this.sentByGateway.has(msg.id?._serialized)) return;
          const body = String(msg.body || '').trim();
          if (!body) return;
          const toChat = String(msg.to || '');
          if (toChat.endsWith('@c.us') || toChat.endsWith('@lid')) {
            const until = Date.now() + this.humanCooldownMs;
            this.humanTakeoverUntilByChat.set(toChat, until);
            const phone = this._normalizePhone(toChat.split('@')[0]);
            this.logger.info('Human takeover detected', { session: this.sessionId, to: phone, chatId: toChat });
            await this._postStatus({ event: 'human_takeover', phone, chat_id: toChat });
          }
          return;
        }
        await this._handleInbound(msg);
      } catch (err) {
        this.logger.error('message_create handler failed', { err: String(err) });
      }
    });
  }

  async start() {
    try {
      await this.client.initialize();
    } catch (err) {
      this.logger.error('Session initialize failed', { session: this.sessionId, err: String(err) });
      await this._postStatus({ event: 'start_failed', error: String(err).slice(0, 200) }).catch(() => {});
      throw err;
    }
  }

  async stop() {
    try {
      await this.client.destroy();
    } catch (_) {}
  }

  async logout() {
    // On Windows, whatsapp-web.js LocalAuth may throw EBUSY when unlinking lockfile.
    // Never let that crash the gateway.
    try {
      await this.client.logout();
      return { ok: true };
    } catch (err) {
      this.logger.error('Logout failed (non-fatal)', { session: this.sessionId, err: String(err) });
      return { ok: false, error: String(err) };
    }
  }

  status() {
    const state = this.client.info ? 'ready' : 'starting';
    return {
      session: this.sessionId,
      state,
      queue: this.sendQueue.size()
    };
  }

  _chatIdForPhone(phone) {
    const digits = String(phone || '').replace(/\D/g, '');
    return `${digits}@c.us`;
  }

  _chatIdForInbound(msg) {
    const from = String(msg?.from || '');
    if (!from) return this._chatIdForPhone('');
    // Prefer a stable conversation id when WhatsApp provides a LID-based "from".
    // For replies, use msg.from (or msg.author) directly.
    return from;
  }

  _normalizePhone(phone) {
    return String(phone || '').replace(/\D/g, '');
  }

  /** Reject WhatsApp LID opaque ids mistaken for phone numbers (often 14+ digits). */
  _isValidPhone(digits) {
    const d = this._normalizePhone(digits);
    if (!d || d.length < 10 || d.length > 15) return false;
    if (d.length >= 14) {
      if (d.startsWith('91') && d.length <= 12) return true;
      if (d.startsWith('1') && d.length === 11) return true;
      return false;
    }
    return true;
  }

  /**
   * Resolve the customer's real phone (not LID) for CRM storage and outbound routing.
   */
  async _resolveCustomerIdentity(msg, chatId) {
    const chatIdStr = String(chatId || '').trim();
    let phone = '';
    let contactName = '';

    try {
      const contact = await msg.getContact();
      contactName = String(contact?.pushname || contact?.name || '').trim();
      const fromContact = this._normalizePhone(contact?.number || '');
      if (fromContact && this._isValidPhone(fromContact)) {
        phone = fromContact;
      }
    } catch (err) {
      this.logger.debug('getContact failed', { session: this.sessionId, err: String(err) });
    }

    if (!phone && chatIdStr.includes('@lid') && this.client) {
      try {
        const rows = await this.client.getContactLidAndPhone([chatIdStr]);
        const row = Array.isArray(rows) ? rows[0] : rows;
        const pn = row?.pn ? String(row.pn) : '';
        if (pn) {
          const resolved = this._normalizePhone(pn.split('@')[0]);
          if (resolved && this._isValidPhone(resolved)) {
            phone = resolved;
          }
        }
      } catch (err) {
        this.logger.warn('getContactLidAndPhone failed', {
          session: this.sessionId,
          chatId: chatIdStr,
          err: String(err)
        });
      }
    }

    if (!phone && chatIdStr.endsWith('@c.us')) {
      const fromChat = this._normalizePhone(chatIdStr.split('@')[0]);
      if (fromChat && this._isValidPhone(fromChat)) {
        phone = fromChat;
      }
    }

    const mapKey = phone || this._normalizePhone(chatIdStr.split('@')[0]);
    if (mapKey && chatIdStr) {
      this.phoneToChatId.set(mapKey, chatIdStr);
    }

    return { phone, contactName, chatId: chatIdStr };
  }

  _humanPausedForChatKey(chatId) {
    const until = this.humanTakeoverUntilByChat.get(chatId);
    return Boolean(until && until > Date.now());
  }

  _humanPausedForChat(chatId) {
    const id = String(chatId || '');
    if (!id) return false;
    if (this._humanPausedForChatKey(id)) return true;
    if (id.includes('@lid')) {
      const digits = id.split('@')[0];
      if (digits && this._humanPausedForChatKey(`${digits}@c.us`)) return true;
    }
    return false;
  }

  async _postStatus(extra) {
    await this.django.postStatus({
      session: this.sessionId,
      ...extra
    });
  }

  async _handleInbound(msg) {
    if (msg.fromMe) return;

    const mid = msg.id?._serialized || '';
    if (mid && this.dedupe.has(`${this.sessionId}:${mid}`)) {
      return;
    }
    if (mid) this.dedupe.add(`${this.sessionId}:${mid}`);

    const msgType = String(msg?.type || '').toLowerCase();
    if (['notification_template', 'e2e_notification', 'protocol', 'call_log'].includes(msgType)) {
      return;
    }

    const chatId = this._chatIdForInbound(msg);
    const { phone, contactName, chatId: resolvedChatId } = await this._resolveCustomerIdentity(msg, chatId);

    let messageText = String(msg.body || '').trim();
    const selectedButtonId = msg.selectedButtonId ? String(msg.selectedButtonId).trim() : '';
    const selectedRowId =
      msg.selectedRowId ||
      (msg.listResponse && msg.listResponse.singleSelectReply
        ? msg.listResponse.singleSelectReply.selectedRowId
        : '');
    if (!messageText && selectedButtonId) messageText = selectedButtonId;
    if (!messageText && selectedRowId) messageText = String(selectedRowId).trim();

    const payload = {
      session: this.sessionId,
      phone,
      message: messageText,
      message_id: mid,
      timestamp: msg.timestamp ? Number(msg.timestamp) : undefined,
      message_type: msgType || 'text',
      chat_id: resolvedChatId || chatId,
      contact_name: contactName || undefined,
      selected_button_id: selectedButtonId || undefined,
      selected_row_id: selectedRowId ? String(selectedRowId) : undefined
    };

    // Media support (download + temporary store + metadata to Django)
    if (msg.hasMedia) {
      const media = await msg.downloadMedia();
      const stored = writeTempMedia({
        base64: media.data,
        mimeType: media.mimetype,
        maxBytes: this.mediaMaxBytes
      });
      payload.media = {
        mimetype: media.mimetype,
        filename: media.filename || '',
        bytes: stored.bytes,
        temp_path: stored.filePath,
        id: stored.id
      };
      const audioTypes = new Set(['ptt', 'audio', 'voice']);
      const isAudio =
        audioTypes.has(String(msg.type || '').toLowerCase()) ||
        String(media.mimetype || '').toLowerCase().startsWith('audio/');
      if (isAudio && stored.bytes <= 2 * 1024 * 1024) {
        payload.media.data_base64 = media.data;
      }
      payload.message_type = msg.type || 'media';
      payload.message = msg.caption || msg.body || '';
    }

    this.logger.info('Incoming message', { session: this.sessionId, phone, type: payload.message_type, id: mid });
    try {
      const res = await this.django.postIncoming(payload);
      const reply = res && typeof res.reply === 'string' ? res.reply : '';
      const stopBot = Boolean(res && res.stop_bot);
      if (stopBot) {
        const until = Date.now() + this.humanCooldownMs;
        this.humanTakeoverUntilByChat.set(chatId, until);
        if (resolvedChatId && resolvedChatId !== chatId) {
          this.humanTakeoverUntilByChat.set(resolvedChatId, until);
        }
      }
      if (reply) {
        await this.sendText({ phone, text: reply, chatId: resolvedChatId || chatId });
      }
    } catch (err) {
      const status = err.response?.status;
      const detail = err.response?.data ? JSON.stringify(err.response.data) : String(err.message || err);
      this.logger.error('Django postIncoming failed', {
        session: this.sessionId,
        phone,
        status,
        detail: detail.slice(0, 400)
      });
    }
  }

  _resolveToChatId(phone, chatId) {
    const normalizedPhone = this._normalizePhone(phone);
    const mappedChatId = normalizedPhone ? this.phoneToChatId.get(normalizedPhone) : '';
    return (
      String(chatId || '').trim() ||
      String(mappedChatId || '').trim() ||
      this._chatIdForPhone(normalizedPhone)
    );
  }

  /**
   * WhatsApp reply buttons (max 3) — green tap buttons under the message.
   */
  async sendButtons({ phone, body, buttons, chatId }) {
    const normalizedPhone = this._normalizePhone(phone);
    const toChatId = this._resolveToChatId(phone, chatId);

    const specs = (buttons || [])
      .map((b) => {
        if (typeof b === 'string') return { id: b, body: b };
        const label = String(b.body || b.title || '').trim();
        return { id: String(b.id || label), body: label };
      })
      .filter((b) => b.body)
      .slice(0, 3)
      .map((b) => ({ id: b.id, body: b.body.slice(0, 20) }));

    if (!specs.length) throw new Error('buttons_required');

    const buttonsMsg = new Buttons(String(body || '').trim(), specs);
    const meta = { session: this.sessionId, phone: normalizedPhone, type: 'buttons' };
    return this.sendQueue.add(async () => {
      await this.client.sendPresenceAvailable();
      let sent;
      try {
        sent = await this.client.sendMessage(toChatId, buttonsMsg);
      } catch (err) {
        this.logger.warn('Buttons send failed (deprecated by WhatsApp)', {
          session: this.sessionId,
          err: String(err)
        });
        throw err;
      }
      const sid = sent && sent.id && sent.id._serialized ? sent.id._serialized : '';
      if (sid) this.sentByGateway.add(sid);
      this.logger.info('Outgoing buttons sent', { ...meta, id: sid, count: specs.length });
      await this.django.postOutgoingAck({
        session: this.sessionId,
        message_id: sid,
        status: 'sent'
      }).catch(() => {});
      return { ok: true, message_id: sid };
    }, meta);
  }

  /**
   * WhatsApp list menu (4–10 options) — "Select" opens a picker.
   */
  async sendList({ phone, body, buttonText, options, chatId }) {
    const normalizedPhone = this._normalizePhone(phone);
    const toChatId = this._resolveToChatId(phone, chatId);

    const rows = (options || [])
      .map((o) => {
        if (typeof o === 'string') return { id: o, title: o };
        const title = String(o.title || o.body || '').trim();
        return { id: String(o.id || title), title: title.slice(0, 24) };
      })
      .filter((r) => r.title)
      .slice(0, 10);

    if (!rows.length) throw new Error('list_rows_required');

    const listMsg = new List(
      String(body || '').trim(),
      String(buttonText || 'Select').trim().slice(0, 20),
      [{ title: 'Options', rows }],
      '',
      ''
    );

    const meta = { session: this.sessionId, phone: normalizedPhone, type: 'list' };
    return this.sendQueue.add(async () => {
      await this.client.sendPresenceAvailable();
      const sent = await this.client.sendMessage(toChatId, listMsg);
      const sid = sent && sent.id && sent.id._serialized ? sent.id._serialized : '';
      if (sid) this.sentByGateway.add(sid);
      this.logger.info('Outgoing list sent', { ...meta, id: sid, count: rows.length });
      await this.django.postOutgoingAck({
        session: this.sessionId,
        message_id: sid,
        status: 'sent'
      }).catch(() => {});
      return { ok: true, message_id: sid };
    }, meta);
  }

  async sendText({ phone, text, chatId }) {
    const normalizedPhone = this._normalizePhone(phone);
    const mappedChatId = normalizedPhone ? this.phoneToChatId.get(normalizedPhone) : '';
    const toChatId =
      String(chatId || '').trim() ||
      String(mappedChatId || '').trim() ||
      this._chatIdForPhone(normalizedPhone);
    const meta = { session: this.sessionId, phone };
    return this.sendQueue.add(async () => {
      await this.client.sendPresenceAvailable();
      const sent = await this.client.sendMessage(toChatId, String(text || ''));
      const sid = sent && sent.id && sent.id._serialized ? sent.id._serialized : '';
      if (sid) this.sentByGateway.add(sid);
      this.logger.info('Outgoing sent', { ...meta, id: sid });
      await this.django.postOutgoingAck({
        session: this.sessionId,
        message_id: sid,
        status: 'sent'
      }).catch(() => {});
      return { ok: true, message_id: sid };
    }, meta);
  }

  async sendMedia({ phone, media, chatId }) {
    const normalizedPhone = this._normalizePhone(phone);
    const mappedChatId = normalizedPhone ? this.phoneToChatId.get(normalizedPhone) : '';
    const toChatId =
      String(chatId || '').trim() ||
      String(mappedChatId || '').trim() ||
      this._chatIdForPhone(normalizedPhone);
    const meta = { session: this.sessionId, phone };
    return this.sendQueue.add(async () => {
      const mm = new MessageMedia(media.mimetype, media.data, media.filename || undefined);
      const sent = await this.client.sendMessage(toChatId, mm, { caption: media.caption || '' });
      const sid = sent && sent.id && sent.id._serialized ? sent.id._serialized : '';
      if (sid) this.sentByGateway.add(sid);
      this.logger.info('Outgoing media sent', { ...meta, id: sid });
      return { ok: true, message_id: sid };
    }, meta);
  }
}

module.exports = { WaSession };

