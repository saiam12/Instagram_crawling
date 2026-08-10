import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  FollowerEnricher,
  csvObjects,
  parseInstagramJson,
  requestFollowerCount,
} from "../instagram_follower_enricher.mjs";

test("large Instagram user IDs are preserved exactly", () => {
  const parsed = parseInstagramJson(
    '{"media":{"user":{"pk":17841443137530278,"username":"example"}}}',
  );
  assert.equal(parsed.media.user.pk, "17841443137530278");
});

test("Business Discovery follower response is normalized", async () => {
  const result = await requestFollowerCount({
    username: "professional_account",
    accessToken: "test-token",
    ownerIgUserId: "owner-id",
    fetchImpl: async () => new Response(JSON.stringify({
      business_discovery: {
        id: "17840000000000000",
        username: "professional_account",
        followers_count: 12_345,
      },
    }), { status: 200 }),
  });
  assert.deepEqual(result, {
    status: "success",
    followerCount: 12_345,
    apiUserId: "17840000000000000",
    apiUsername: "professional_account",
    error: "",
    usagePercent: 0,
  });
});

test("follower enrichment runs concurrently and writes one row per user", async (t) => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "instagram-followers-test-"));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  let active = 0;
  let maximumActive = 0;
  const fetchImpl = async (url) => {
    active += 1;
    maximumActive = Math.max(maximumActive, active);
    await new Promise((resolve) => setTimeout(resolve, 30));
    const username = url.searchParams.get("fields").match(/username\(([^)]+)\)/)?.[1] ?? "";
    active -= 1;
    return new Response(JSON.stringify({
      business_discovery: {
        id: `api-${username}`,
        username,
        followers_count: username.length * 100,
      },
    }), { status: 200 });
  };
  const enricher = new FollowerEnricher({
    dataDir: directory,
    accessToken: "test-token",
    ownerIgUserId: "owner-id",
    concurrency: 2,
    cacheHours: 1,
    fetchImpl,
  });
  await Promise.all([
    enricher.trackUser({ userId: "101", username: "first", seenAt: "2026-08-02T00:00:00.000Z" }),
    enricher.trackUser({ userId: "102", username: "second", seenAt: "2026-08-02T00:00:01.000Z" }),
    enricher.trackUser({ userId: "103", username: "third", seenAt: "2026-08-02T00:00:02.000Z" }),
  ]);
  const stats = await enricher.drain();
  const users = csvObjects(await fs.readFile(path.join(directory, "users.csv"), "utf8"));
  const lookups = csvObjects(await fs.readFile(path.join(directory, "follower_lookups.csv"), "utf8"));
  assert.equal(maximumActive, 2);
  assert.equal(stats.success, 3);
  assert.equal(users.length, 3);
  assert.equal(lookups.length, 3);
  assert.ok(users.every((row) => row.lookup_status === "success"));
  assert.ok(users.every((row) => Number(row.follower_count) > 0));
});

test("tracking a Reel user does not wait for the follower API response", async (t) => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "instagram-followers-nonblocking-"));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  let releaseRequest;
  const fetchImpl = () => new Promise((resolve) => {
    releaseRequest = () => resolve(new Response(JSON.stringify({
      business_discovery: {
        id: "api-201",
        username: "nonblocking",
        followers_count: 500,
      },
    }), { status: 200 }));
  });
  const enricher = new FollowerEnricher({
    dataDir: directory,
    accessToken: "test-token",
    ownerIgUserId: "owner-id",
    fetchImpl,
  });
  const tracked = await Promise.race([
    enricher.trackUser({ userId: "201", username: "nonblocking" }).then(() => true),
    new Promise((resolve) => setTimeout(() => resolve(false), 100)),
  ]);
  assert.equal(tracked, true);
  releaseRequest();
  const stats = await enricher.drain();
  assert.equal(stats.success, 1);
});

test("an expired token stops queued lookups and reports the reason", async (t) => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "instagram-followers-auth-"));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  let releaseRequest;
  const fetchImpl = () => new Promise((resolve) => {
    releaseRequest = () => resolve(new Response(JSON.stringify({
      error: { code: 190, message: "Error validating access token: Session has expired." },
    }), { status: 400 }));
  });
  const enricher = new FollowerEnricher({
    dataDir: directory,
    accessToken: "expired-token",
    ownerIgUserId: "owner-id",
    concurrency: 1,
    fetchImpl,
  });
  await enricher.trackUser({ userId: "301", username: "first_auth" });
  await enricher.trackUser({ userId: "302", username: "second_deferred" });
  releaseRequest();
  const stats = await enricher.drain();
  const users = csvObjects(await fs.readFile(path.join(directory, "users.csv"), "utf8"));
  assert.equal(stats.stopStatus, "auth_error");
  assert.match(stats.stopError, /Session has expired/);
  assert.deepEqual(
    users.map((row) => row.lookup_status).sort(),
    ["auth_error", "deferred_auth_error"],
  );
});

test("a token-free web lookup writes follower data with the web source", async (t) => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "instagram-followers-web-"));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const usernames = [];
  const enricher = new FollowerEnricher({
    dataDir: directory,
    concurrency: 1,
    lookupImpl: async ({ username }) => {
      usernames.push(username);
      return {
        status: "success",
        followerCount: username.length * 1_000,
        source: "instagram_web",
        error: "",
      };
    },
  });
  await enricher.trackUser({ userId: "401", username: "web_creator" });
  const stats = await enricher.drain();
  const [user] = csvObjects(await fs.readFile(path.join(directory, "users.csv"), "utf8"));
  const [lookup] = csvObjects(
    await fs.readFile(path.join(directory, "follower_lookups.csv"), "utf8"),
  );
  assert.equal(stats.configured, true);
  assert.deepEqual(usernames, ["web_creator"]);
  assert.equal(user.follower_count, "11000");
  assert.equal(user.follower_source, "instagram_web");
  assert.equal(lookup.source, "instagram_web");
  assert.equal(lookup.api_user_id, "");
});

test("a closed follower page is recorded without crashing the queue", async (t) => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "instagram-followers-closed-"));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const progress = [];
  const enricher = new FollowerEnricher({
    dataDir: directory,
    concurrency: 1,
    lookupImpl: async () => {
      throw new Error("Target page, context or browser has been closed");
    },
    source: "instagram_web",
    onProgress: (event) => progress.push(event),
  });
  await enricher.trackUser({ userId: "501", username: "closed_page" });
  const stats = await enricher.drain();
  const [user] = csvObjects(await fs.readFile(path.join(directory, "users.csv"), "utf8"));
  assert.equal(stats.failed, 1);
  assert.equal(stats.completed, 1);
  assert.equal(user.lookup_status, "web_error");
  assert.match(user.last_error, /has been closed/);
  assert.deepEqual(
    progress.map(({ completed, queued, username, status }) => ({
      completed, queued, username, status,
    })),
    [{ completed: 1, queued: 1, username: "closed_page", status: "web_error" }],
  );
});
