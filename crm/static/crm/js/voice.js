/*
 * Voice messages for the photo pages. Light on purpose:
 *  - the phone's own recorder (MediaRecorder) does the work; the CRM server never converts or analyses audio
 *  - a low bit rate (24 kbps, about 180 KB a minute) and a hard stop at the limit
 *  - nothing is sent until the person presses Send, and playback loads nothing until they press play
 * Needs a secure page (https), which the CRM already is.
 */
function voiceRecorder(maxSeconds) {
  return {
    state: 'idle',            // idle | recording | ready
    seconds: 0, blob: null, url: '', error: '',
    max: maxSeconds || 120,
    supported: !!(window.MediaRecorder && navigator.mediaDevices && navigator.mediaDevices.getUserMedia),
    rec: null, stream: null, timer: null,
    pick() {
      // Prefer MP4/AAC (plays everywhere, including iPhones), then WebM/Opus, which is the smallest.
      var types = ['audio/mp4', 'audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus'];
      for (var i = 0; i < types.length; i++) { if (MediaRecorder.isTypeSupported(types[i])) return types[i]; }
      return '';
    },
    async start() {
      this.error = '';
      try {
        this.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      } catch (e) {
        this.error = 'We could not use the microphone. Allow it in your browser settings, or type your instructions instead.';
        return;
      }
      var mime = this.pick(), opts = { audioBitsPerSecond: 24000 }, chunks = [];
      if (mime) opts.mimeType = mime;
      this.rec = new MediaRecorder(this.stream, opts);
      this.rec.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
      this.rec.onstop = () => {
        clearInterval(this.timer);
        this.stream.getTracks().forEach(function (t) { t.stop(); });
        this.blob = new Blob(chunks, { type: (this.rec.mimeType || mime || 'audio/webm') });
        if (this.url) URL.revokeObjectURL(this.url);
        this.url = URL.createObjectURL(this.blob);
        this.state = 'ready';
        this.$dispatch('voice-ready', { blob: this.blob, seconds: this.seconds });
      };
      this.rec.start();
      this.state = 'recording';
      this.seconds = 0;
      this.timer = setInterval(() => { this.seconds++; if (this.seconds >= this.max) this.stop(); }, 1000);
    },
    stop() { if (this.rec && this.rec.state !== 'inactive') this.rec.stop(); },
    discard() {
      if (this.url) URL.revokeObjectURL(this.url);
      this.blob = null; this.url = ''; this.seconds = 0; this.state = 'idle';
      this.$dispatch('voice-cleared');
    },
    clock() {
      var m = Math.floor(this.seconds / 60), s = this.seconds % 60;
      return m + ':' + (s < 10 ? '0' : '') + s;
    }
  };
}

// Send a recording. Returns the parsed JSON ({ok, error}).
async function sendVoice(url, voice, fields) {
  var fd = new FormData();
  var type = (voice.blob.type || '').toLowerCase();
  var ext = type.indexOf('mp4') >= 0 ? 'm4a' : (type.indexOf('ogg') >= 0 ? 'ogg' : 'webm');
  fd.append('file', voice.blob, 'voice.' + ext);
  fd.append('seconds', String(voice.seconds || 0));
  Object.keys(fields || {}).forEach(function (k) { fd.append(k, fields[k]); });
  try {
    var res = await fetch(url, { method: 'POST', body: fd, credentials: 'same-origin',
      headers: { 'X-CSRFToken': document.body.dataset.csrf, 'X-Requested-With': 'XMLHttpRequest' } });
    var data = {};
    try { data = await res.json(); } catch (e) {}
    if (res.ok && data.ok) return { ok: true };
    return { ok: false, error: data.error || 'We could not send the voice message. Try again.' };
  } catch (e) {
    return { ok: false, error: 'The connection dropped. Try again.' };
  }
}
