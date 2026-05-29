class TTLSet {
  constructor({ ttlMs = 60_000, maxSize = 50_000 } = {}) {
    this.ttlMs = ttlMs;
    this.maxSize = maxSize;
    this.map = new Map(); // key -> expiresAt
  }

  _prune(now) {
    for (const [k, exp] of this.map) {
      if (exp <= now) this.map.delete(k);
    }
    if (this.map.size <= this.maxSize) return;
    const overflow = this.map.size - this.maxSize;
    let i = 0;
    for (const k of this.map.keys()) {
      this.map.delete(k);
      i += 1;
      if (i >= overflow) break;
    }
  }

  has(key) {
    const now = Date.now();
    const exp = this.map.get(key);
    if (!exp) return false;
    if (exp <= now) {
      this.map.delete(key);
      return false;
    }
    return true;
  }

  add(key) {
    const now = Date.now();
    this._prune(now);
    this.map.set(key, now + this.ttlMs);
  }
}

module.exports = { TTLSet };

