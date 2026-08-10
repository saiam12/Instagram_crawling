import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";

const USER_FIELDS = [
  "user_id",
  "username",
  "first_seen_at",
  "last_seen_at",
  "follower_count",
  "follower_count_collected_at",
  "lookup_status",
  "last_lookup_at",
  "last_error",
];

const FOLLOWER_LOOKUP_FIELDS = [
  "collected_at",
  "user_id",
  "username",
  "follower_count",
  "error",
];

function csvValue(value) {
  const text = String(value ?? "").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  return `"${text.replace(/"/g, '""')}"`;
}

function parseCsv(text) {
  const rows = [];
  let row = [];
  let value = "";
  let quoted = false;
  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    if (quoted) {
      if (character === '"' && text[index + 1] === '"') {
        value += '"';
        index += 1;
      } else if (character === '"') {
        quoted = false;
      } else {
        value += character;
      }
    } else if (character === '"') {
      quoted = true;
    } else if (character === ",") {
      row.push(value);
      value = "";
    } else if (character === "\n") {
      row.push(value);
      rows.push(row);
      row = [];
      value = "";
    } else if (character !== "\r") {
      value += character;
    }
  }
  if (value || row.length) {
    row.push(value);
    rows.push(row);
  }
  return rows.filter((candidate) => candidate.some((cell) => cell !== ""));
}

function csvObjects(text) {
  const rows = parseCsv(String(text ?? "").replace(/^\uFEFF/, ""));
  if (!rows.length) return [];
  const headers = rows[0];
  return rows.slice(1).map((row) => Object.fromEntries(
    headers.map((header, index) => [header, row[index] ?? ""]),
  ));
}

async function atomicWriteCsv(destination, rows, fields) {
  await fsp.mkdir(path.dirname(destination), { recursive: true });
  const content = [
    `\uFEFF${fields.map(csvValue).join(",")}`,
    ...rows.map((row) => fields.map((field) => csvValue(row[field])).join(",")),
  ].join("\r\n") + "\r\n";
  const temporary = path.join(
    path.dirname(destination),
    `.${path.basename(destination)}.${process.pid}.${Date.now()}.tmp`,
  );
  await fsp.writeFile(temporary, content, "utf8");
  await fsp.rename(temporary, destination);
}

async function appendCsv(destination, row, fields) {
  await fsp.mkdir(path.dirname(destination), { recursive: true });
  const needsHeader = !fs.existsSync(destination) || (await fsp.stat(destination)).size === 0;
  const lines = [];
  if (needsHeader) lines.push(`\uFEFF${fields.map(csvValue).join(",")}`);
  lines.push(fields.map((field) => csvValue(row[field])).join(","));
  await fsp.appendFile(destination, `${lines.join("\r\n")}\r\n`, "utf8");
}

async function normalizeCsvSchema(destination, fields) {
  if (!fs.existsSync(destination)) return;
  const text = await fsp.readFile(destination, "utf8");
  const rows = parseCsv(text.replace(/^\uFEFF/, ""));
  const headers = rows[0] ?? [];
  if (headers.join("\u0000") === fields.join("\u0000")) return;
  const normalized = csvObjects(text).map((row) => Object.fromEntries(
    fields.map((field) => [field, row[field] ?? ""]),
  ));
  await atomicWriteCsv(destination, normalized, fields);
}

function parseInstagramJson(rawText) {
  const protectedIntegers = String(rawText ?? "").replace(
    /("(?:pk|pk_id|id)"\s*:\s*)(\d{16,})(?=\s*[,}])/g,
    '$1"$2"',
  );
  return JSON.parse(protectedIntegers);
}

class FollowerEnricher {
  constructor({
    dataDir,
    concurrency = 1,
    cacheHours = 1,
    lookupImpl,
    source = "instagram_web",
    onProgress = null,
    now = () => new Date(),
  }) {
    this.dataDir = path.resolve(dataDir);
    this.usersPath = path.join(this.dataDir, "users.csv");
    this.lookupsPath = path.join(this.dataDir, "follower_lookups.csv");
    if (typeof lookupImpl !== "function") {
      throw new TypeError("FollowerEnricher requires a web lookup function.");
    }
    this.concurrency = Math.max(1, Math.min(10, Math.trunc(Number(concurrency) || 1)));
    this.cacheHours = Math.max(0, Number(cacheHours) || 0);
    this.lookupImpl = lookupImpl;
    this.source = source;
    this.onProgress = onProgress;
    this.now = now;
    this.users = new Map();
    this.userIdIndex = new Map();
    this.usernameIndex = new Map();
    this.queue = [];
    this.queued = new Set();
    this.active = 0;
    this.stopped = false;
    this.writeChain = Promise.resolve();
    this.lookupWriteChain = Promise.resolve();
    this.usersDirtyChanges = 0;
    this.usersFlushTimer = null;
    this.drainWaiters = [];
    this.stats = {
      queued: 0,
      success: 0,
      unavailable: 0,
      failed: 0,
      completed: 0,
      stopStatus: "",
      stopError: "",
    };
    this.ready = this._load();
  }

  async _load() {
    await fsp.mkdir(this.dataDir, { recursive: true });
    await normalizeCsvSchema(this.usersPath, USER_FIELDS);
    await normalizeCsvSchema(this.lookupsPath, FOLLOWER_LOOKUP_FIELDS);
    if (!fs.existsSync(this.usersPath)) return;
    const rows = csvObjects(await fsp.readFile(this.usersPath, "utf8"));
    for (const source of rows) {
      const row = Object.fromEntries(USER_FIELDS.map((field) => [field, source[field] ?? ""]));
      const key = row.user_id ? `id:${row.user_id}` : `username:${row.username.toLowerCase()}`;
      if (key.endsWith(":")) continue;
      row._key = key;
      this.users.set(key, row);
      this._indexRow(row);
    }
  }

  _indexRow(row, previousUsername = "") {
    if (row.user_id) this.userIdIndex.set(row.user_id, row);
    if (previousUsername && this.usernameIndex.get(previousUsername.toLowerCase()) === row) {
      this.usernameIndex.delete(previousUsername.toLowerCase());
    }
    if (row.username) this.usernameIndex.set(row.username.toLowerCase(), row);
  }

  _findOrCreate(userId, username, seenAt) {
    let row = (userId && this.userIdIndex.get(userId))
      || (username && this.usernameIndex.get(username.toLowerCase()));
    if (!row) {
      const key = userId ? `id:${userId}` : `username:${username.toLowerCase()}`;
      row = Object.fromEntries(USER_FIELDS.map((field) => [field, ""]));
      row._key = key;
      row.first_seen_at = seenAt;
      this.users.set(key, row);
    }
    const previousUsername = row.username;
    if (userId) row.user_id = userId;
    if (username) row.username = username;
    row.first_seen_at ||= seenAt;
    row.last_seen_at = seenAt;
    this._indexRow(row, previousUsername);
    return row;
  }

  _flushUsers() {
    if (!this.usersDirtyChanges) return this.writeChain;
    this.usersDirtyChanges = 0;
    if (this.usersFlushTimer) {
      clearTimeout(this.usersFlushTimer);
      this.usersFlushTimer = null;
    }
    this.writeChain = this.writeChain.then(async () => {
      const rows = [...this.users.values()]
        .sort((left, right) => (left.first_seen_at || "").localeCompare(right.first_seen_at || ""))
        .map((row) => Object.fromEntries(USER_FIELDS.map((field) => [field, row[field] ?? ""])));
      await atomicWriteCsv(this.usersPath, rows, USER_FIELDS);
    });
    return this.writeChain;
  }

  _markUsersDirty() {
    this.usersDirtyChanges += 1;
    if (this.usersDirtyChanges >= 100) {
      void this._flushUsers().catch(() => {});
    } else if (!this.usersFlushTimer) {
      this.usersFlushTimer = setTimeout(() => {
        this.usersFlushTimer = null;
        void this._flushUsers().catch(() => {});
      }, 60_000);
    }
  }

  _scheduleLookupWrite(row) {
    this.lookupWriteChain = this.lookupWriteChain.then(() => appendCsv(
      this.lookupsPath,
      row,
      FOLLOWER_LOOKUP_FIELDS,
    ));
    return this.lookupWriteChain;
  }

  _isFresh(row) {
    const raw = row.last_lookup_at;
    if (!raw || this.cacheHours === 0) return false;
    const timestamp = new Date(raw).getTime();
    if (!Number.isFinite(timestamp)) return false;
    return this.now().getTime() - timestamp < this.cacheHours * 3_600_000;
  }

  _enqueue(row, force = false) {
    if (this.stopped || !row.username || this.queued.has(row._key)) return false;
    if (!force && this._isFresh(row)) return false;
    row.lookup_status = "queued";
    row.last_error = "";
    this.queue.push(row);
    this.queued.add(row._key);
    this.stats.queued += 1;
    this._pump();
    return true;
  }

  async trackUser({ userId = "", username = "", seenAt = "" }) {
    await this.ready;
    const normalizedId = String(userId ?? "").trim();
    const normalizedUsername = String(username ?? "").trim().replace(/^@/, "");
    if (!normalizedId && !normalizedUsername) return null;
    const timestamp = seenAt || this.now().toISOString();
    const row = this._findOrCreate(normalizedId, normalizedUsername, timestamp);
    this._enqueue(row, false);
    this._markUsersDirty();
    return row;
  }

  async enqueueAll({ force = false } = {}) {
    await this.ready;
    let count = 0;
    for (const row of this.users.values()) {
      if (this._enqueue(row, force)) count += 1;
    }
    this._markUsersDirty();
    return count;
  }

  _pump() {
    while (!this.stopped && this.active < this.concurrency && this.queue.length) {
      const row = this.queue.shift();
      this.active += 1;
      void this._lookupOne(row).finally(() => {
        this.active -= 1;
        this.queued.delete(row._key);
        this._pump();
        this._resolveDrainIfIdle();
      });
    }
    this._resolveDrainIfIdle();
  }

  async _lookupOne(row) {
    let result;
    try {
      result = await this.lookupImpl({ username: row.username, userId: row.user_id });
    } catch (error) {
      result = {
        status: "web_error",
        error: String(error?.message ?? error).slice(0, 500),
        source: this.source,
      };
    }
    const source = result.source || this.source;
    const collectedAt = this.now().toISOString();
    row.last_lookup_at = collectedAt;
    row.lookup_status = result.status;
    row.last_error = String(result.error ?? "").slice(0, 500);
    if (result.status === "success") {
      row.follower_count = result.followerCount;
      row.follower_count_collected_at = collectedAt;
      this.stats.success += 1;
    } else if (["not_professional_or_unavailable", "profile_unavailable"].includes(result.status)) {
      this.stats.unavailable += 1;
    } else {
      this.stats.failed += 1;
    }
    await this._scheduleLookupWrite({
      collected_at: collectedAt,
      user_id: row.user_id,
      username: row.username,
      follower_count: result.status === "success" ? result.followerCount : "",
      error: row.last_error,
    });
    this.stats.completed += 1;
    if (this.onProgress) {
      try {
        this.onProgress({
          completed: this.stats.completed,
          queued: this.stats.queued,
          username: row.username,
          status: result.status,
          followerCount: result.status === "success" ? result.followerCount : "",
          error: row.last_error,
        });
      } catch {
        // Console progress reporting must not interrupt collection.
      }
    }
    this._markUsersDirty();
    if (["rate_limited", "login_required", "challenge_required"].includes(result.status)) {
      if (!this.stats.stopStatus) {
        this.stats.stopStatus = result.status;
        this.stats.stopError = row.last_error || "Follower lookup stopped.";
      }
      this.stopped = true;
      const deferredStatus = `deferred_${result.status}`;
      for (const pending of this.queue.splice(0)) {
        pending.lookup_status = deferredStatus;
        this.queued.delete(pending._key);
      }
      this._markUsersDirty();
    }
  }

  _resolveDrainIfIdle() {
    if (this.active || this.queue.length) return;
    for (const resolve of this.drainWaiters.splice(0)) resolve();
  }

  async drain() {
    await this.ready;
    if (this.active || this.queue.length) {
      await new Promise((resolve) => this.drainWaiters.push(resolve));
    }
    await this._flushUsers();
    await this.writeChain;
    await this.lookupWriteChain;
    return { ...this.stats, stopped: this.stopped };
  }
}

export {
  FOLLOWER_LOOKUP_FIELDS,
  FollowerEnricher,
  USER_FIELDS,
  csvObjects,
  parseInstagramJson,
};
