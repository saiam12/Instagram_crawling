import assert from "node:assert/strict";
import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  acquireCollectorLock,
  advanceToNextReel,
  createReelStore,
  parseArgs,
} from "./instagram_reels_browser.mjs";
import { FollowerEnricher } from "./instagram_follower_enricher.mjs";

function reelRecord(index) {
  const collectedAt = new Date(Date.UTC(2026, 0, 1) + index * 1_000).toISOString();
  return {
    collected_at: collectedAt,
    url: `https://www.instagram.com/reels/test_${index}/`,
    user_id: String(10_000_000 + index),
    username: `test_user_${index}`,
    title: `test reel ${index}`,
    hashtags: "#test",
    audio_name: "test audio",
    location_name: "",
    ad: false,
    uploaded_at: collectedAt,
    days_since_upload: 0,
    like_count: index,
    comment_count: 0,
    repost_count: 0,
    follower_count: "",
    reaction_rate: "",
    follower_count_collected_at: "",
    follower_lookup_status: "",
  };
}

test("parseArgs accepts long-run collection controls", () => {
  const defaults = parseArgs([]);
  assert.equal(defaults.pageRecycleItems, 200);
  assert.equal(defaults.checkpointItems, 100);

  const options = parseArgs([
    "--max-items", "10000",
    "--page-recycle-items", "250",
    "--checkpoint-items", "500",
    "--transition-timeout-seconds", "3",
    "--followers-after-reels",
  ]);
  assert.equal(options.maxItems, 10_000);
  assert.equal(options.pageRecycleItems, 250);
  assert.equal(options.checkpointItems, 500);
  assert.equal(options.transitionTimeoutSeconds, 3);
  assert.equal(options.followersAfterReels, true);
});

test("reel navigation waits until the active shortcode changes", async () => {
  const identities = [
    { currentUrl: "https://www.instagram.com/reels/old/", activeHref: "" },
    { currentUrl: "https://www.instagram.com/reels/new/", activeHref: "" },
  ];
  let evaluateCalls = 0;
  let arrowPresses = 0;
  let wheelScrolls = 0;
  const page = {
    evaluate: async () => identities[Math.min(evaluateCalls++, identities.length - 1)],
    waitForTimeout: async () => {},
    keyboard: { press: async () => { arrowPresses += 1; } },
    mouse: { wheel: async () => { wheelScrolls += 1; } },
  };
  const transition = await advanceToNextReel(page, "old", 1_000);
  assert.equal(transition.changed, true);
  assert.equal(transition.shortcode, "new");
  assert.equal(arrowPresses, 1);
  assert.equal(wheelScrolls, 0);
});

test("collector lock prevents a second writer and is released cleanly", async () => {
  const directory = await fsp.mkdtemp(path.join(os.tmpdir(), "instagram-collector-lock-"));
  try {
    const release = await acquireCollectorLock(directory);
    await assert.rejects(
      acquireCollectorLock(directory),
      /Another collector is already using this data directory/,
    );
    await release();
    assert.equal(fs.existsSync(path.join(directory, "collector.lock.json")), false);
  } finally {
    await fsp.rm(directory, { recursive: true, force: true });
  }
});

test("reel store checkpoints 10,000 records without leaving a journal", async () => {
  const directory = await fsp.mkdtemp(path.join(os.tmpdir(), "instagram-reels-10k-"));
  try {
    const csvPath = path.join(directory, "reels_web.csv");
    const store = await createReelStore(csvPath, { flushRecordCount: 500 });
    for (let index = 0; index < 10_000; index += 1) {
      await store.append(reelRecord(index));
    }
    const stats = store.stats();
    assert.equal(stats.rows, 10_000);
    assert.equal(stats.pending, 0);
    assert.equal(fs.existsSync(stats.journalPath), false);
    const lineCount = (await fsp.readFile(csvPath, "utf8")).trimEnd().split(/\r?\n/).length;
    assert.equal(lineCount, 10_001);
  } finally {
    await fsp.rm(directory, { recursive: true, force: true });
  }
});

test("reel store replays records left in the crash-recovery journal", async () => {
  const directory = await fsp.mkdtemp(path.join(os.tmpdir(), "instagram-reels-recovery-"));
  try {
    const csvPath = path.join(directory, "reels_web.csv");
    const interruptedStore = await createReelStore(csvPath, { flushRecordCount: 500 });
    await interruptedStore.append(reelRecord(1));
    await interruptedStore.append(reelRecord(2));
    assert.equal(fs.existsSync(interruptedStore.stats().journalPath), true);

    const recoveredStore = await createReelStore(csvPath, { flushRecordCount: 500 });
    assert.equal(recoveredStore.stats().rows, 2);
    assert.equal(fs.existsSync(recoveredStore.stats().journalPath), false);
    const lineCount = (await fsp.readFile(csvPath, "utf8")).trimEnd().split(/\r?\n/).length;
    assert.equal(lineCount, 3);
  } finally {
    await fsp.rm(directory, { recursive: true, force: true });
  }
});

test("follower lookups can be deferred until reel collection finishes", async () => {
  const directory = await fsp.mkdtemp(path.join(os.tmpdir(), "instagram-followers-deferred-"));
  let lookupCalls = 0;
  try {
    const enricher = new FollowerEnricher({
      dataDir: directory,
      lookupImpl: async () => {
        lookupCalls += 1;
        return { status: "success", followerCount: 123, error: "", source: "test" };
      },
    });
    const payload = {
      userId: "123456789",
      username: "test_user",
      seenAt: "2026-01-01T00:00:00.000Z",
    };
    await enricher.trackUser({ ...payload, enqueue: false });
    let stats = await enricher.drain();
    assert.equal(stats.queued, 0);
    assert.equal(lookupCalls, 0);

    await enricher.trackUser({ ...payload, enqueue: true });
    stats = await enricher.drain();
    assert.equal(stats.success, 1);
    assert.equal(lookupCalls, 1);
  } finally {
    await fsp.rm(directory, { recursive: true, force: true });
  }
});

test("follower cache is fixed at six hours", async () => {
  const directory = await fsp.mkdtemp(path.join(os.tmpdir(), "instagram-followers-cache-"));
  let currentTime = new Date("2026-01-01T00:00:00.000Z");
  let lookupCalls = 0;
  try {
    const enricher = new FollowerEnricher({
      dataDir: directory,
      now: () => currentTime,
      lookupImpl: async () => {
        lookupCalls += 1;
        return { status: "success", followerCount: 123, error: "", source: "test" };
      },
    });
    const user = {
      userId: "987654321",
      username: "cached_user",
      seenAt: currentTime.toISOString(),
    };
    await enricher.trackUser(user);
    await enricher.drain();
    await enricher.trackUser(user);
    await enricher.drain();
    assert.equal(lookupCalls, 1);

    currentTime = new Date(currentTime.getTime() + 6 * 3_600_000);
    await enricher.trackUser({ ...user, seenAt: currentTime.toISOString() });
    await enricher.drain();
    assert.equal(lookupCalls, 2);
  } finally {
    await fsp.rm(directory, { recursive: true, force: true });
  }
});

test("follower enrichment stops after repeated browser errors", async () => {
  const directory = await fsp.mkdtemp(path.join(os.tmpdir(), "instagram-followers-errors-"));
  let lookupCalls = 0;
  try {
    const enricher = new FollowerEnricher({
      dataDir: directory,
      lookupImpl: async () => {
        lookupCalls += 1;
        return { status: "web_error", error: "page crashed", source: "test" };
      },
    });
    for (let index = 0; index < 10; index += 1) {
      await enricher.trackUser({
        userId: String(index + 1),
        username: `error_user_${index}`,
        seenAt: "2026-01-01T00:00:00.000Z",
      });
    }
    const stats = await enricher.drain();
    assert.equal(stats.stopStatus, "repeated_web_error");
    assert.equal(stats.failed, 5);
    assert.equal(lookupCalls, 5);
  } finally {
    await fsp.rm(directory, { recursive: true, force: true });
  }
});
