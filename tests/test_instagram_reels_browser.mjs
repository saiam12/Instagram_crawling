import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { csvObjects } from "../instagram_follower_enricher.mjs";

import {
  CSV_FIELDS,
  appendRecord,
  buildCollectedRecord,
  calculateReactionRate,
  captionWithoutHashtags,
  collectHashtagReelUrls,
  collectReelMetadata,
  collectReelUploadDates,
  collectionLabel,
  createBackgroundFollowerRuntime,
  followerLookupDelaySeconds,
  daysSinceUpload,
  elapsedSnapshotLabel,
  extractHashtags,
  hasAnyHashtag,
  hasCompleteReelCoreData,
  followerCountFromInstagramData,
  isCaptionMoreText,
  isInstagramHashtagSurface,
  isInstagramReelsSurface,
  isProfileInfoText,
  loadReelUrls,
  mediaIsAdvertisement,
  mergeFollowerDataIntoReels,
  normalizeReelUrl,
  normalizeUploadTime,
  parseArgs,
  parseHashtagQuery,
  parseFollowerCount,
  parseMetricCount,
  prepareCsv,
  requestWebFollowerCount,
  truncateCaption,
} from "../instagram_reels_browser.mjs";

test("background mode cannot be combined with manual mode", () => {
  assert.equal(parseArgs(["--background"]).background, true);
  assert.throws(
    () => parseArgs(["--background", "--manual"]),
    /cannot be combined/,
  );
});

test("OR hashtag queries accept quotes and normalize leading hash signs", () => {
  assert.deepEqual(
    parseHashtagQuery('"\uB9DB\uC9D1" OR "#\uC11C\uC6B8\uB9DB\uC9D1"'),
    ["\uB9DB\uC9D1", "\uC11C\uC6B8\uB9DB\uC9D1"],
  );
  assert.deepEqual(
    parseArgs(["--hashtag-query", "\uB9DB\uC9D1 or \uC11C\uC6B8\uB9DB\uC9D1"]).hashtags,
    ["\uB9DB\uC9D1", "\uC11C\uC6B8\uB9DB\uC9D1"],
  );
});

test("hashtag OR filtering includes partial hashtag matches", () => {
  const query = ["\uB9DB\uC9D1", "\uC11C\uC6B8\uB9DB\uC9D1"];
  assert.equal(hasAnyHashtag("#\uAC15\uB0A8\uB9DB\uC9D1 #\uCE74\uD398", query), true);
  assert.equal(hasAnyHashtag("#\uC11C\uC6B8\uB9DB\uC9D1\uCD94\uCC9C", query), true);
  assert.equal(hasAnyHashtag("#\uC11C\uC6B8\uCE74\uD398 #\uC5EC\uD589", query), false);
});

test("Instagram hashtag page URLs are recognized", () => {
  assert.equal(
    isInstagramHashtagSurface("https://www.instagram.com/explore/tags/%EB%A7%9B%EC%A7%91/"),
    true,
  );
  assert.equal(isInstagramHashtagSurface("https://www.instagram.com/reels/"), false);
});

test("hashtag Reel candidates are interleaved across OR terms", async () => {
  const pages = new Map([
    ["\uB9DB\uC9D1", [
      "https://www.instagram.com/reel/AAA111/",
      "https://www.instagram.com/reel/BBB222/",
    ]],
    ["\uC11C\uC6B8\uB9DB\uC9D1", [
      "https://www.instagram.com/reel/CCC333/",
      "https://www.instagram.com/reel/DDD444/",
    ]],
  ]);
  let activeTag = "";
  const page = {
    goto: async (url) => {
      activeTag = decodeURIComponent(new URL(url).pathname.split("/").filter(Boolean).at(-1));
    },
    waitForTimeout: async () => {},
    url: () => `https://www.instagram.com/explore/tags/${encodeURIComponent(activeTag)}/`,
    evaluate: async () => pages.get(activeTag) ?? [],
    mouse: { wheel: async () => {} },
  };
  assert.deepEqual(
    await collectHashtagReelUrls(page, ["\uB9DB\uC9D1", "\uC11C\uC6B8\uB9DB\uC9D1"], 2),
    [
      "https://www.instagram.com/reels/AAA111/",
      "https://www.instagram.com/reels/CCC333/",
      "https://www.instagram.com/reels/BBB222/",
      "https://www.instagram.com/reels/DDD444/",
    ],
  );
});

test("only the caption More control is accepted", () => {
  assert.equal(isCaptionMoreText("더 보기"), true);
  assert.equal(isCaptionMoreText("... 더 보기"), true);
  assert.equal(isCaptionMoreText("More"), true);
  assert.equal(isCaptionMoreText("이 계정 정보 더 보기"), false);
  assert.equal(isCaptionMoreText("프로필 더 보기"), false);
  assert.equal(isCaptionMoreText("About this account More"), false);
  assert.equal(isProfileInfoText("이 계정 정보 더 보기"), true);
  assert.equal(isProfileInfoText("About this account More"), true);
  assert.equal(isProfileInfoText("긴 캡션 ... 더 보기"), false);
});

test("Reels feed and individual Reel URLs are recognized as safe surfaces", () => {
  assert.equal(isInstagramReelsSurface("https://www.instagram.com/reels/"), true);
  assert.equal(isInstagramReelsSurface("https://www.instagram.com/reels/DRMYmFkEiYh/"), true);
  assert.equal(isInstagramReelsSurface("https://www.instagram.com/poket.onemore/"), false);
  assert.equal(isInstagramReelsSurface("https://www.instagram.com/accounts/about/"), false);
});

test("Korean and international abbreviated counts become integers", () => {
  assert.equal(parseMetricCount("154.9만"), 1_549_000);
  assert.equal(parseMetricCount("6745"), 6_745);
  assert.equal(parseMetricCount("2.2만"), 22_000);
  assert.equal(parseMetricCount("1.2M"), 1_200_000);
  assert.equal(parseMetricCount(""), "");
});

test("reaction rate is likes divided by followers", () => {
  assert.equal(calculateReactionRate(125, 510), 0.245098);
  assert.equal(calculateReactionRate("1,000", "2,000"), 0.5);
  assert.equal(calculateReactionRate(100, 0), "");
  assert.equal(calculateReactionRate("", 500), "");
});

test("complete Reel data uses the 0.5 second fast path", () => {
  const complete = {
    url: "https://www.instagram.com/reels/ABC123/",
    user_id: "123",
    username: "sample",
    uploaded_at: "2026-08-04T00:00:00.000Z",
    like_count: 0,
    comment_count: 12,
    repost_count: 3,
    ad: "false",
  };
  assert.equal(hasCompleteReelCoreData(complete), true);
  assert.equal(hasCompleteReelCoreData({ ...complete, uploaded_at: "" }), false);
  assert.equal(hasCompleteReelCoreData({ ...complete, comment_count: "" }), false);
});

test("successful follower data uses 0.5 seconds and failures keep the configured delay", () => {
  assert.equal(followerLookupDelaySeconds({ status: "success", followerCount: 123 }, 8), 0.5);
  assert.equal(followerLookupDelaySeconds({ status: "web_unavailable" }, 8), 8);
  assert.equal(followerLookupDelaySeconds({ status: "success", followerCount: "" }, 8), 8);
});

test("profile follower labels become exact integers", () => {
  assert.equal(parseFollowerCount("\uD314\uB85C\uC6CC 1.2\uB9CC"), 12_000);
  assert.equal(parseFollowerCount("20.3K followers"), 20_300);
  assert.equal(parseFollowerCount("1,234 followers"), 1_234);
  assert.equal(parseFollowerCount("1,234"), 1_234);
  assert.equal(parseFollowerCount(""), "");
});

test("profile JSON follower count is matched to the requested username", () => {
  const payload = {
    users: [
      { username: "other", follower_count: 99 },
      { username: "target.user", edge_followed_by: { count: 12_345 } },
    ],
  };
  assert.equal(followerCountFromInstagramData(payload, "target.user"), 12_345);
});

test("web follower lookup reads the public profile header without an API token", async () => {
  const page = {
    goto: async () => ({ status: () => 200 }),
    waitForTimeout: async () => {},
    url: () => "https://www.instagram.com/target.user/",
    evaluate: async () => ({
      candidates: ["\uD314\uB85C\uC6CC 1.2\uB9CC"],
      bodyText: "",
      hasLoginForm: false,
    }),
    locator: () => ({ allTextContents: async () => [] }),
  };
  assert.deepEqual(await requestWebFollowerCount({ page, username: "target.user" }), {
    status: "success",
    followerCount: 12_000,
    error: "",
    source: "instagram_web",
  });
});

test("follower lookups use a separate headless browser with copied login state", async () => {
  const storageState = { cookies: [{ name: "sessionid", value: "saved" }], origins: [] };
  const backgroundPage = { name: "follower-page" };
  let launchOptions;
  let contextOptions;
  const followerContext = {
    newPage: async () => backgroundPage,
  };
  const browser = {
    newContext: async (options) => {
      contextOptions = options;
      return followerContext;
    },
  };
  const chromium = {
    launch: async (options) => {
      launchOptions = options;
      return browser;
    },
  };
  const result = await createBackgroundFollowerRuntime({
    chromium,
    sourceContext: { storageState: async () => storageState },
    executablePath: "C:/browser.exe",
  });
  assert.deepEqual(launchOptions, {
    executablePath: "C:/browser.exe",
    headless: true,
  });
  assert.deepEqual(contextOptions.storageState, storageState);
  assert.equal(result.browser, browser);
  assert.equal(result.context, followerContext);
  assert.equal(result.page, backgroundPage);
});

test("Reel and Reels URLs normalize to the canonical Reels URL", () => {
  assert.deepEqual(normalizeReelUrl("https://www.instagram.com/reel/DY1zvzBS5dZ/?x=1"), {
    url: "https://www.instagram.com/reels/DY1zvzBS5dZ/",
    shortcode: "DY1zvzBS5dZ",
  });
});

test("refresh URL files are normalized and deduplicated in row order", async (t) => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "instagram-reel-urls-"));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const filePath = path.join(directory, "urls.txt");
  await fs.writeFile(
    filePath,
    "https://www.instagram.com/reel/AAA111/\n"
      + "https://www.instagram.com/reels/BBB222/?source=x\n"
      + "https://www.instagram.com/reels/AAA111/\n",
    "utf8",
  );

  assert.deepEqual(await loadReelUrls(filePath), [
    "https://www.instagram.com/reels/AAA111/",
    "https://www.instagram.com/reels/BBB222/",
  ]);
});

test("captions longer than 300 characters are truncated after character 300", () => {
  const caption = `${"가".repeat(300)}나`;
  assert.equal(truncateCaption(caption, 300), `${"가".repeat(300)}...`);
  assert.equal(truncateCaption("짧은 캡션"), "짧은 캡션");
});

test("elapsed snapshot labels use minutes, hours, days, and weeks", () => {
  const initial = "2026-08-01T00:00:00.000Z";
  assert.equal(elapsedSnapshotLabel(initial, "2026-08-01T00:30:00.000Z"), "+30Minute");
  assert.equal(elapsedSnapshotLabel(initial, "2026-08-01T02:00:00.000Z"), "+2Hour");
  assert.equal(elapsedSnapshotLabel(initial, "2026-08-03T00:00:00.000Z"), "+2Day");
  assert.equal(elapsedSnapshotLabel(initial, "2026-09-05T00:00:00.000Z"), "+5Weeks");
});

test("collection labels use ordinals and upload age is measured in days", () => {
  assert.equal(collectionLabel(2), "2nd collect");
  assert.equal(collectionLabel(3), "3rd collect");
  assert.equal(collectionLabel(11), "11th collect");
  assert.equal(collectionLabel(21), "21st collect");
  assert.equal(
    daysSinceUpload("2026-08-01T00:00:00.000Z", "2026-08-02T12:00:00.000Z"),
    1.5,
  );
});

test("hashtags are extracted from the full caption and deduplicated", () => {
  const caption = `${"가".repeat(110)} #긴캡션태그 #Seoul #긴캡션태그`;
  assert.equal(extractHashtags(caption, ["#추가태그", "#seoul"]), "#긴캡션태그 #Seoul #추가태그");
  assert.equal(captionWithoutHashtags("본문 #태그1 #태그2"), "본문");
});

test("upload timestamps from Reel feed responses are indexed by shortcode", () => {
  const uploadDates = new Map();
  collectReelUploadDates(
    {
      data: {
        edges: [
          { node: { media: { code: "Dbhqn-PBtr3", taken_at: 1_785_648_753 } } },
        ],
      },
    },
    uploadDates,
  );
  assert.equal(uploadDates.get("Dbhqn-PBtr3"), "2026-08-02T05:32:33.000Z");
  assert.equal(normalizeUploadTime("not-a-date"), "");
});

test("username, caption, and music are read from matching Reel metadata", () => {
  const metadata = new Map();
  collectReelMetadata(
    {
      data: {
        media: {
          code: "DbSwT1ISw4k",
          taken_at: 1_785_148_335,
          user: { pk: "12345678901234567", username: "emma_chuan0527" },
          caption: { text: "だいじょうぶ\n#fyp #추천" },
          like_count: 3_217,
          comment_count: 203,
          media_repost_count: 82,
          location: { name: "서울숲", lat: 37.5445, lng: 127.0374 },
          clips_metadata: {
            music_info: {
              music_asset_info: {
                title: "Otsukare SUMMER",
                display_artist: "HALCALI",
              },
            },
          },
        },
      },
    },
    metadata,
  );
  assert.deepEqual(metadata.get("DbSwT1ISw4k"), {
    userId: "12345678901234567",
    username: "emma_chuan0527",
    caption: "だいじょうぶ\n#fyp #추천",
    audioName: "HALCALI · Otsukare SUMMER",
    locationName: "서울숲",
    ad: false,
    uploadedAt: "2026-07-27T10:32:15.000Z",
    likeCount: 3_217,
    commentCount: 203,
    repostCount: 82,
  });
});

test("advertisements are recognized from explicit Instagram ad signals", () => {
  assert.equal(mediaIsAdvertisement({ is_ad: true }), true);
  assert.equal(mediaIsAdvertisement({ is_paid_partnership: true }), true);
  assert.equal(mediaIsAdvertisement({ ad_id: "123456" }), true);
  assert.equal(mediaIsAdvertisement({ commerciality_status: "organic" }), false);
  assert.equal(mediaIsAdvertisement({ is_ad: false, sponsor_tags: [] }), false);
});

test("response metadata replaces a misclassified audio title and stale DOM counts", () => {
  const record = buildCollectedRecord(
    {
      url: "https://www.instagram.com/reels/DbgglUZhtfy/",
      username: "",
      title: "Sia · Unstoppable",
      hashtagTexts: ["#다이어트", "#동기부여"],
      audioName: "",
      uploadedAt: "",
      likeText: "3190",
      commentText: "203",
      repostText: "82",
    },
    {
      userId: "98765432109876543",
      username: "joyyyyforme",
      caption: "97kg - 56kg까지 7년\n목표는 48kg입니다.\n\n#다이어트 #동기부여",
      audioName: "Sia · Unstoppable",
      uploadedAt: "2026-08-02T07:24:27.000Z",
      likeCount: 3_217,
      commentCount: 203,
      repostCount: 82,
    },
    "2026-08-02T14:29:06.908Z",
  );
  assert.equal(record.user_id, "98765432109876543");
  assert.equal(record.username, "joyyyyforme");
  assert.equal(record.title, "97kg - 56kg까지 7년 목표는 48kg입니다.");
  assert.equal(record.hashtags, "#다이어트 #동기부여");
  assert.equal(record.audio_name, "Sia · Unstoppable");
  assert.equal(record.location_name, "");
  assert.equal(record.ad, "false");
  assert.equal(record.like_count, 3_217);
  assert.equal(record.comment_count, 203);
  assert.equal(record.repost_count, 82);
});

test("a lone username captured as the title is restored to username", () => {
  const record = buildCollectedRecord({
    url: "https://www.instagram.com/reels/AAA111/",
    username: "",
    userId: "",
    title: "gong_ppadeol",
    hashtagTexts: [],
    audioName: "",
    uploadedAt: "",
    likeText: "1278",
    commentText: "15",
    repostText: "17",
  });
  assert.equal(record.username, "gong_ppadeol");
  assert.equal(record.title, "");
});

test("an incompatible CSV is preserved before the new schema is written", async (t) => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "instagram-reels-test-"));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const csvPath = path.join(directory, "reels_web.csv");
  await fs.writeFile(csvPath, '"old_header"\r\n"old_value"\r\n', "utf8");

  await prepareCsv(csvPath);
  assert.rejects(() => fs.access(csvPath));
  const legacyFiles = (await fs.readdir(directory)).filter((name) => name.startsWith("reels_web_legacy_"));
  assert.equal(legacyFiles.length, 1);

  await appendRecord(
    csvPath,
    {
      collected_at: "2026-08-02T00:00:00.000Z",
      url: "https://www.instagram.com/reels/DY1zvzBS5dZ/",
      user_id: "12345678901234567",
      username: "vibrro_",
      title: "남자의 향수 비로 No.5",
      hashtags: "#향수 #남자패션",
      audio_name: "nuts · Se Acabo (Translated Remix)",
      uploaded_at: "2026-08-01T04:00:00.000Z",
      like_count: 1_549_000,
      comment_count: 6_745,
      repost_count: 22_000,
    },
    true,
  );
  const written = await fs.readFile(csvPath, "utf8");
  assert.ok(written.startsWith(`\uFEFF${CSV_FIELDS.map((field) => `"${field}"`).join(",")}`));
  assert.match(written, /"1549000","6745","22000"/);
});

test("an older compatible reels CSV gains follower columns without losing rows", async (t) => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "instagram-reels-migrate-"));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const csvPath = path.join(directory, "reels_web.csv");
  const oldFields = [
    "collected_at", "url", "user_id", "username", "title", "hashtags",
    "audio_name", "uploaded_at", "like_count", "comment_count", "repost_count",
  ];
  const oldValues = [
    "2026-08-02T00:00:00.000Z",
    "https://www.instagram.com/reels/DY1zvzBS5dZ/",
    "12345678901234567",
    "vibrro_",
    "기존 제목",
    "#기존",
    "기존 음원",
    "2026-08-01T04:00:00.000Z",
    "1549000",
    "6745",
    "22000",
  ];
  await fs.writeFile(
    csvPath,
    `\uFEFF${oldFields.map((value) => `"${value}"`).join(",")}\r\n${oldValues.map((value) => `"${value}"`).join(",")}\r\n`,
    "utf8",
  );

  await prepareCsv(csvPath);

  const rows = csvObjects(await fs.readFile(csvPath, "utf8"));
  assert.equal(rows.length, 1);
  assert.equal(rows[0].url, oldValues[1]);
  assert.equal(rows[0].like_count, "1549000");
  assert.equal(rows[0].follower_count, "");
  assert.equal(
    (await fs.readdir(directory)).some((name) => name.includes("_legacy_")),
    false,
  );
});

test("legacy coordinate columns are removed without losing Reel rows", async (t) => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "instagram-reels-coordinates-"));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const csvPath = path.join(directory, "reels_web.csv");
  const fields = [
    ...CSV_FIELDS.slice(0, 8),
    "location_latitude",
    "location_longitude",
    ...CSV_FIELDS.slice(8),
  ];
  const row = Object.fromEntries(fields.map((field) => [field, ""]));
  Object.assign(row, {
    collected_at: "2026-08-02T00:00:00.000Z",
    url: "https://www.instagram.com/reels/AAA111/",
    username: "creator",
    location_name: "Seoul",
    location_latitude: "37.5665",
    location_longitude: "126.9780",
    like_count: "100",
  });
  await fs.writeFile(
    csvPath,
    `\uFEFF${fields.map((field) => `"${field}"`).join(",")}\r\n`
      + `${fields.map((field) => `"${row[field]}"`).join(",")}\r\n`,
    "utf8",
  );

  await prepareCsv(csvPath);

  const migratedText = await fs.readFile(csvPath, "utf8");
  const [migrated] = csvObjects(migratedText);
  assert.doesNotMatch(migratedText.split(/\r?\n/, 1)[0], /location_latitude|location_longitude/);
  assert.equal(migrated.url, row.url);
  assert.equal(migrated.location_name, "Seoul");
  assert.equal(migrated.like_count, "100");
});

test("follower results are merged into the same reels rows", async (t) => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "instagram-reels-followers-"));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const csvPath = path.join(directory, "reels_web.csv");
  const usersPath = path.join(directory, "users.csv");
  const values = [
    "2026-08-02T00:00:00.000Z",
    "https://www.instagram.com/reels/DY1zvzBS5dZ/",
    "12345678901234567",
    "vibrro_",
    "기존 제목",
    "#기존",
    "기존 음원",
    "",
    "false",
    "2026-08-01T04:00:00.000Z",
    "0.83",
    "1549000",
    "6745",
    "22000",
    "",
    "",
    "",
    "queued",
  ];
  await fs.writeFile(
    csvPath,
    `\uFEFF${CSV_FIELDS.map((value) => `"${value}"`).join(",")}\r\n${values.map((value) => `"${value}"`).join(",")}\r\n`,
    "utf8",
  );
  await fs.writeFile(
    usersPath,
    `\uFEFF"user_id","username","first_seen_at","last_seen_at","follower_count","follower_count_collected_at","follower_source","api_user_id","lookup_status","last_lookup_at","last_error"\r\n"12345678901234567","vibrro_","","","12345","2026-08-03T01:02:03.000Z","business_discovery","","success","",""\r\n`,
    "utf8",
  );

  assert.equal(await mergeFollowerDataIntoReels(csvPath, usersPath), 1);
  const [row] = csvObjects(await fs.readFile(csvPath, "utf8"));
  assert.equal(row.follower_count, "12345");
  assert.equal(row.follower_count_collected_at, "2026-08-03T01:02:03.000Z");
  assert.equal(row.follower_lookup_status, "success");
  assert.equal(row.like_count, "1549000");
  assert.equal(row.reaction_rate, "125.475901");
});

test("repeat collection keeps one row and adds only changed values to snapshot columns", async (t) => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "instagram-reels-wide-"));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const csvPath = path.join(directory, "reels_web.csv");
  const initial = {
    collected_at: "2026-08-01T00:00:00.000Z",
    url: "https://www.instagram.com/reels/AAA111/",
    user_id: "12345678901234567",
    username: "creator",
    title: "같은 캡션",
    hashtags: "#same",
    audio_name: "Artist · Song",
    location_name: "서울숲",
    ad: "false",
    uploaded_at: "2026-07-31T00:00:00.000Z",
    like_count: 100,
    comment_count: 20,
    repost_count: 3,
    follower_count: 500,
    follower_count_collected_at: "2026-08-01T00:00:00.000Z",
    follower_lookup_status: "success",
  };
  await appendRecord(csvPath, initial);
  await appendRecord(csvPath, {
    ...initial,
    collected_at: "2026-08-01T02:00:00.000Z",
    location_name: "부산 해운대",
    ad: "true",
    like_count: 125,
    follower_count: 510,
    follower_count_collected_at: "2026-08-01T02:00:00.000Z",
  });
  await appendRecord(csvPath, {
    ...initial,
    collected_at: "2026-08-01T04:00:00.000Z",
    like_count: 130,
    follower_count: 515,
  });

  const text = await fs.readFile(csvPath, "utf8");
  const [row] = csvObjects(text);
  assert.equal(csvObjects(text).length, 1);
  assert.equal(row.reaction_rate, "0.2");
  assert.equal(row["2nd collect_collected_at"], "2026-08-01T02:00:00.000Z");
  assert.equal(row["2nd collect_days_since_upload"], "1.08");
  assert.equal(row["2nd collect_url"], undefined);
  assert.equal(row["2nd collect_title"], undefined);
  assert.equal(row["2nd collect_location_name"], undefined);
  assert.equal(row["2nd collect_ad"], undefined);
  assert.equal(row["2nd collect_like_count"], "125");
  assert.equal(row["2nd collect_comment_count"], "");
  assert.equal(row["2nd collect_follower_count"], "510");
  assert.equal(row["2nd collect_reaction_rate"], "0.245098");
  assert.equal(row["3rd collect_collected_at"], "2026-08-01T04:00:00.000Z");
  assert.equal(row["3rd collect_like_count"], "130");
  assert.equal(row["3rd collect_reaction_rate"], "0.252427");
});

test("a later collection backfills missing base identity fields", async (t) => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "instagram-reels-identity-"));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const csvPath = path.join(directory, "reels_web.csv");
  const initial = Object.fromEntries(CSV_FIELDS.map((field) => [field, ""]));
  initial.collected_at = "2026-08-01T00:00:00.000Z";
  initial.url = "https://www.instagram.com/reels/AAA111/";
  await appendRecord(csvPath, initial);
  await appendRecord(csvPath, {
    ...initial,
    collected_at: "2026-08-01T02:00:00.000Z",
    user_id: "123456789",
    username: "creator_name",
  });

  const [row] = csvObjects(await fs.readFile(csvPath, "utf8"));
  assert.equal(row.user_id, "123456789");
  assert.equal(row.username, "creator_name");
  assert.equal(row["2nd collect_user_id"], undefined);
  assert.equal(row["2nd collect_username"], undefined);
});

test("legacy elapsed snapshot columns migrate to ordinal metric-only columns", async (t) => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "instagram-reels-ordinal-"));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const csvPath = path.join(directory, "reels_web.csv");
  const fields = [
    ...CSV_FIELDS.filter((field) => field !== "days_since_upload"),
    "+2Hour_collected_at",
    "+2Hour_title",
    "+2Hour_like_count",
    "+2Hour_comment_count",
    "+2Hour_repost_count",
    "+2Hour_follower_count",
  ];
  const values = Object.fromEntries(fields.map((field) => [field, ""]));
  Object.assign(values, {
    collected_at: "2026-08-01T00:00:00.000Z",
    url: "https://www.instagram.com/reels/AAA111/",
    uploaded_at: "2026-07-31T00:00:00.000Z",
    "+2Hour_collected_at": "2026-08-01T02:00:00.000Z",
    "+2Hour_title": "changed caption",
    "+2Hour_like_count": "125",
    "+2Hour_comment_count": "21",
    "+2Hour_repost_count": "4",
    "+2Hour_follower_count": "510",
  });
  await fs.writeFile(
    csvPath,
    `\uFEFF${fields.map((field) => `"${field}"`).join(",")}\r\n`
      + `${fields.map((field) => `"${values[field]}"`).join(",")}\r\n`,
    "utf8",
  );

  await prepareCsv(csvPath);

  const text = await fs.readFile(csvPath, "utf8");
  const [row] = csvObjects(text);
  assert.match(text.split(/\r?\n/, 1)[0], /2nd collect_collected_at/);
  assert.doesNotMatch(text.split(/\r?\n/, 1)[0], /2nd collect_title|\+2Hour/);
  assert.equal(row.days_since_upload, "1");
  assert.equal(row["2nd collect_days_since_upload"], "1.08");
  assert.equal(row["2nd collect_like_count"], "125");
});
