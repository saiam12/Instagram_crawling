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
  "follower_source",
  "api_user_id",
  "lookup_status",
  "last_lookup_at",
  "last_error",
];

const FOLLOWER_LOOKUP_FIELDS = [
  "collected_at",
  "user_id",
  "username",
  "api_user_id",
  "follower_count",
  "source",
  "lookup_status",
  "error",
];

const RATE_LIMIT_CODES = new Set([4, 17, 32, 613, 80001, 80002]);

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

function loadDotEnvFile(filePath) {
  if (!filePath || !fs.existsSync(filePath)) return;
  const lines = fs.readFileSync(filePath, "utf8").replace(/^\uFEFF/, "").split(/\r?\n/);
  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    const separator = line.indexOf("=");
    const key = line.slice(0, separator).trim().replace(/^export\s+/, "");
    let value = line.slice(separator + 1).trim();
    if (value.length >= 2 && value[0] === value.at(-1) && ['"', "'"].includes(value[0])) {
      value = value.slice(1, -1);
    }
    if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(key) && process.env[key] === undefined) {
      process.env[key] = value;
    }
  }
}

function parseInstagramJson(rawText) {
  const protectedIntegers = String(rawText ?? "").replace(
    /("(?:pk|pk_id|id)"\s*:\s*)(\d{16,})(?=\s*[,}])/g,
    '$1"$2"',
  );
  return JSON.parse(protectedIntegers);
}

function normalizeApiVersion(value) {
  const version = String(value || "v26.0").trim();
  return version.startsWith("v") ? version : `v${version}`;
}

function usagePercentFromHeaders(headers) {
  let maximum = 0;
  const visit = (value) => {
    if (Array.isArray(value)) {
      value.forEach(visit);
    } else if (value && typeof value === "object") {
      for (const [key, child] of Object.entries(value)) {
        if (["call_count", "total_cputime", "total_time"].includes(key)) {
          const number = Number(child);
          if (Number.isFinite(number)) maximum = Math.max(maximum, number);
        } else {
          visit(child);
        }
      }
    }
  };
  for (const name of ["x-business-use-case-usage", "x-app-usage"]) {
    const raw = headers?.get?.(name);
    if (!raw) continue;
    try {
      visit(JSON.parse(raw));
    } catch {
      // A malformed optional usage header does not invalidate a successful result.
    }
  }
  return maximum;
}

function classifyGraphError(payload, httpStatus) {
  const error = payload?.error ?? {};
  const code = Number(error.code ?? httpStatus ?? 0);
  const message = String(error.message ?? `Graph API request failed (${httpStatus || "unknown"}).`);
  if (httpStatus === 429 || RATE_LIMIT_CODES.has(code)) return { status: "rate_limited", message };
  if (code === 190) return { status: "auth_error", message };
  if ([10, 100].includes(code)) {
    return { status: "not_professional_or_unavailable", message };
  }
  return { status: "api_error", message };
}

async function requestFollowerCount({
  username,
  accessToken,
  ownerIgUserId,
  apiVersion = "v26.0",
  fetchImpl = globalThis.fetch,
  timeoutMilliseconds = 30_000,
}) {
  const url = new URL(
    `https://graph.facebook.com/${normalizeApiVersion(apiVersion)}/${encodeURIComponent(ownerIgUserId)}`,
  );
  url.searchParams.set(
    "fields",
    `business_discovery.username(${username}){id,username,followers_count}`,
  );
  url.searchParams.set("access_token", accessToken);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMilliseconds);
  try {
    const response = await fetchImpl(url, {
      method: "GET",
      headers: { "User-Agent": "instagram-reels-follower-enricher/1.0" },
      signal: controller.signal,
    });
    const rawBody = await response.text();
    let payload = {};
    try {
      payload = parseInstagramJson(rawBody);
    } catch {
      return {
        status: "api_error",
        error: "Graph API returned invalid JSON.",
        usagePercent: usagePercentFromHeaders(response.headers),
      };
    }
    const usagePercent = usagePercentFromHeaders(response.headers);
    if (!response.ok || payload.error) {
      const classified = classifyGraphError(payload, response.status);
      return { status: classified.status, error: classified.message, usagePercent };
    }
    const account = payload.business_discovery;
    const followerCount = Number(account?.followers_count);
    if (!account || !Number.isFinite(followerCount)) {
      return {
        status: "not_professional_or_unavailable",
        error: "Business Discovery did not return followers_count.",
        usagePercent,
      };
    }
    return {
      status: "success",
      followerCount: Math.round(followerCount),
      apiUserId: String(account.id ?? ""),
      apiUsername: String(account.username ?? username),
      error: "",
      usagePercent,
    };
  } catch (error) {
    return {
      status: error?.name === "AbortError" ? "timeout" : "network_error",
      error: error?.name === "AbortError" ? "Graph API request timed out." : String(error?.message ?? error),
      usagePercent: 0,
    };
  } finally {
    clearTimeout(timer);
  }
}

class FollowerEnricher {
  constructor({
    dataDir,
    accessToken = process.env.INSTAGRAM_ACCESS_TOKEN ?? "",
    ownerIgUserId = process.env.INSTAGRAM_IG_USER_ID ?? "",
    apiVersion = process.env.INSTAGRAM_API_VERSION ?? "v26.0",
    concurrency = 3,
    cacheHours = 1,
    usageThreshold = 90,
    fetchImpl = globalThis.fetch,
    lookupImpl = null,
    source = "",
    onProgress = null,
    now = () => new Date(),
  }) {
    this.dataDir = path.resolve(dataDir);
    this.usersPath = path.join(this.dataDir, "users.csv");
    this.lookupsPath = path.join(this.dataDir, "follower_lookups.csv");
    this.accessToken = String(accessToken).trim();
    this.ownerIgUserId = String(ownerIgUserId).trim();
    this.apiVersion = normalizeApiVersion(apiVersion);
    this.concurrency = Math.max(1, Math.min(10, Math.trunc(Number(concurrency) || 3)));
    this.cacheHours = Math.max(0, Number(cacheHours) || 0);
    this.usageThreshold = Math.max(1, Math.min(100, Number(usageThreshold) || 90));
    this.fetchImpl = fetchImpl;
    this.lookupImpl = lookupImpl;
    this.source = lookupImpl ? (source || "instagram_web") : "graph_api";
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

  get configured() {
    return Boolean(this.lookupImpl || (this.accessToken && this.ownerIgUserId));
  }

  async _load() {
    await fsp.mkdir(this.dataDir, { recursive: true });
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
    if (!this.configured || this.stopped || !row.username || this.queued.has(row._key)) return false;
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
    if (!this.configured && !row.follower_count) {
      row.lookup_status = "api_not_configured";
      row.last_error = "Set INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_IG_USER_ID in .env.";
    }
    this._enqueue(row, false);
    this._markUsersDirty();
    return row;
  }

  async enqueueAll({ force = false } = {}) {
    await this.ready;
    if (!this.configured) {
      for (const row of this.users.values()) {
        if (!row.follower_count) {
          row.lookup_status = "api_not_configured";
          row.last_error = "Set INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_IG_USER_ID in .env.";
        }
      }
      this._markUsersDirty();
      return 0;
    }
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
      result = this.lookupImpl
        ? await this.lookupImpl({ username: row.username, userId: row.user_id })
        : await requestFollowerCount({
          username: row.username,
          accessToken: this.accessToken,
          ownerIgUserId: this.ownerIgUserId,
          apiVersion: this.apiVersion,
          fetchImpl: this.fetchImpl,
        });
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
      row.follower_source = source;
      if (result.apiUserId) row.api_user_id = result.apiUserId;
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
      api_user_id: result.apiUserId ?? row.api_user_id,
      follower_count: result.status === "success" ? result.followerCount : "",
      source,
      lookup_status: result.status,
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
    if (
      Number(result.usagePercent || 0) >= this.usageThreshold
      || ["rate_limited", "auth_error", "login_required", "challenge_required"].includes(result.status)
    ) {
      if (!this.stats.stopStatus) {
        this.stats.stopStatus = result.status;
        this.stats.stopError = row.last_error
          || `API usage reached ${result.usagePercent}%.`;
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
    return { ...this.stats, configured: this.configured, stopped: this.stopped };
  }
}

export {
  FOLLOWER_LOOKUP_FIELDS,
  FollowerEnricher,
  USER_FIELDS,
  csvObjects,
  loadDotEnvFile,
  parseInstagramJson,
  requestFollowerCount,
  usagePercentFromHeaders,
};
