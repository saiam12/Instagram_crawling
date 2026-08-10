import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import readline from "node:readline/promises";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import {
  FollowerEnricher,
  csvObjects,
  loadDotEnvFile,
  parseInstagramJson,
} from "./instagram_follower_enricher.mjs";

const CSV_FIELDS = [
  "collected_at",
  "url",
  "user_id",
  "username",
  "title",
  "hashtags",
  "audio_name",
  "location_name",
  "ad",
  "uploaded_at",
  "days_since_upload",
  "like_count",
  "comment_count",
  "repost_count",
  "follower_count",
  "reaction_rate",
  "follower_count_collected_at",
  "follower_lookup_status",
];

const DROPPED_CSV_FIELDS = new Set([
  "location_latitude",
  "location_longitude",
]);

const RECOLLECT_FIELDS = [
  "collected_at",
  "days_since_upload",
  "like_count",
  "comment_count",
  "repost_count",
  "follower_count",
  "reaction_rate",
];

const FAST_SUCCESS_INTERVAL_SECONDS = 0.5;

const CAPTION_MORE_TEXT_PATTERN = /^(?:(?:…|\.\.\.)\s*)?(?:더\s*보기|more)$/i;
const PROFILE_INFO_TEXT_PATTERN = /(?:계정\s*정보|프로필|about\s+this\s+account|account\s+(?:info|information)|view\s+profile|profile)/i;
const INSTAGRAM_USERNAME_PATTERN = /^[A-Za-z0-9._]{1,30}$/;

function isCaptionMoreText(value) {
  return CAPTION_MORE_TEXT_PATTERN.test(String(value ?? "").trim().replace(/\s+/g, " "));
}

function isProfileInfoText(value) {
  return PROFILE_INFO_TEXT_PATTERN.test(String(value ?? "").trim().replace(/\s+/g, " "));
}

function isInstagramReelsSurface(value) {
  try {
    const url = new URL(value, "https://www.instagram.com/");
    return url.hostname.endsWith("instagram.com") && /^\/reels?(?:\/|$)/i.test(url.pathname);
  } catch {
    return false;
  }
}

function isInstagramHashtagSurface(value) {
  try {
    const url = new URL(value, "https://www.instagram.com/");
    return url.hostname.endsWith("instagram.com")
      && /^\/explore\/tags\/[^/]+\/?$/i.test(url.pathname);
  } catch {
    return false;
  }
}

function parseHashtagQuery(value) {
  const query = String(value ?? "").trim();
  if (!query) return [];
  const parts = query.split(/\s+or\s+/i);
  if (parts.some((part) => !part.trim())) {
    throw new Error("--hashtag-query must join complete hashtag names with OR.");
  }
  const unique = new Map();
  for (const part of parts) {
    const hashtag = part.trim()
      .replace(/^["']|["']$/g, "")
      .replace(/^#/, "")
      .trim()
      .normalize("NFC");
    if (!/^[\p{L}\p{N}_]+$/u.test(hashtag)) {
      throw new Error(`Invalid hashtag in --hashtag-query: ${part.trim()}`);
    }
    unique.set(hashtag.toLocaleLowerCase(), hashtag);
  }
  return [...unique.values()];
}

function hasAnyHashtag(value, requiredHashtags) {
  if (!requiredHashtags.length) return true;
  const observed = new Set(
    (String(value ?? "").match(/#[\p{L}\p{N}_]+/gu) ?? [])
      .map((tag) => tag.slice(1).normalize("NFC").toLocaleLowerCase()),
  );
  return requiredHashtags.some((tag) => {
    const required = tag.normalize("NFC").toLocaleLowerCase();
    return [...observed].some((candidate) => candidate.includes(required));
  });
}

function hashtagPageUrl(hashtag) {
  return `https://www.instagram.com/explore/tags/${encodeURIComponent(hashtag)}/`;
}

function parseArgs(argv) {
  const options = {
    startUrl: "https://www.instagram.com/reels/",
    maxItems: 50,
    intervalSeconds: 5,
    manual: false,
    background: false,
    followersOnly: false,
    forceFollowers: false,
    followerCacheHours: 1,
    followerIntervalSeconds: 8,
    hashtags: [],
    urlsFile: "",
    dataDir: path.resolve("data_web"),
    profileDir: path.resolve(".instagram_browser_profile"),
  };
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--manual") options.manual = true;
    else if (value === "--background") options.background = true;
    else if (value === "--followers-only") options.followersOnly = true;
    else if (value === "--force-followers") options.forceFollowers = true;
    else if (value === "--start-url") options.startUrl = argv[++index];
    else if (value === "--max-items") options.maxItems = Number(argv[++index]);
    else if (value === "--interval-seconds") options.intervalSeconds = Number(argv[++index]);
    else if (value === "--data-dir") options.dataDir = path.resolve(argv[++index]);
    else if (value === "--profile-dir") options.profileDir = path.resolve(argv[++index]);
    else if (value === "--follower-cache-hours") options.followerCacheHours = Number(argv[++index]);
    else if (value === "--follower-interval-seconds") options.followerIntervalSeconds = Number(argv[++index]);
    else if (value === "--hashtag-query") options.hashtags = parseHashtagQuery(argv[++index]);
    else if (value === "--urls-file") options.urlsFile = path.resolve(argv[++index]);
    else throw new Error(`Unknown argument: ${value}`);
  }
  if (!Number.isInteger(options.maxItems) || options.maxItems < 1) {
    throw new Error("--max-items must be a positive integer.");
  }
  if (!Number.isFinite(options.intervalSeconds) || options.intervalSeconds < 1) {
    throw new Error("--interval-seconds must be at least 1.");
  }
  if (options.manual && options.background) {
    throw new Error("--manual cannot be combined with --background.");
  }
  if (!Number.isFinite(options.followerCacheHours) || options.followerCacheHours < 0) {
    throw new Error("--follower-cache-hours must be 0 or greater.");
  }
  if (!Number.isFinite(options.followerIntervalSeconds) || options.followerIntervalSeconds < 1) {
    throw new Error("--follower-interval-seconds must be at least 1.");
  }
  if (!/^https:\/\/(?:www\.)?instagram\.com\//i.test(options.startUrl)) {
    throw new Error("--start-url must be an https://www.instagram.com/ URL.");
  }
  return options;
}

async function loadReelUrls(filePath) {
  const text = await fsp.readFile(filePath, "utf8");
  const urls = [];
  const seen = new Set();
  for (const line of text.split(/\r?\n/)) {
    const normalized = normalizeReelUrl(line.trim());
    if (!normalized || seen.has(normalized.url)) continue;
    seen.add(normalized.url);
    urls.push(normalized.url);
  }
  if (!urls.length) throw new Error(`No Instagram Reel URLs were found in ${filePath}`);
  return urls;
}

async function collectHashtagReelUrls(page, hashtags, maxItems) {
  const groups = [];
  const perHashtagLimit = Math.max(20, maxItems * 3);
  for (const hashtag of hashtags) {
    await page.goto(hashtagPageUrl(hashtag), {
      waitUntil: "domcontentloaded",
      timeout: 30_000,
    });
    await page.waitForTimeout(1_500);
    if (/\/accounts\/login/i.test(page.url())) {
      throw new Error("Instagram login is required for hashtag collection.");
    }
    if (/\/(?:challenge|checkpoint)\//i.test(page.url())) {
      throw new Error("Instagram requested an account check during hashtag collection.");
    }
    const urls = [];
    const seen = new Set();
    let unchangedAttempts = 0;
    for (let attempt = 0; attempt < 30 && urls.length < perHashtagLimit; attempt += 1) {
      const hrefs = await page.evaluate(() => (
        [...document.querySelectorAll('a[href*="/reel/"], a[href*="/reels/"]')]
          .map((anchor) => anchor.href)
      ));
      const before = urls.length;
      for (const href of hrefs) {
        const normalized = normalizeReelUrl(href);
        if (!normalized || seen.has(normalized.url)) continue;
        seen.add(normalized.url);
        urls.push(normalized.url);
      }
      unchangedAttempts = urls.length === before ? unchangedAttempts + 1 : 0;
      if (unchangedAttempts >= 4 || urls.length >= perHashtagLimit) break;
      await page.mouse.wheel(0, 1_200);
      await page.waitForTimeout(800);
    }
    groups.push(urls);
  }
  const combined = [];
  const combinedSeen = new Set();
  const maximumCandidates = Math.max(maxItems * 4, maxItems);
  for (let index = 0; combined.length < maximumCandidates; index += 1) {
    let found = false;
    for (const group of groups) {
      const url = group[index];
      if (!url) continue;
      found = true;
      if (combinedSeen.has(url)) continue;
      combinedSeen.add(url);
      combined.push(url);
      if (combined.length >= maximumCandidates) break;
    }
    if (!found) break;
  }
  return combined;
}

function loadPlaywright() {
  const modulePath = process.env.INSTAGRAM_PLAYWRIGHT_MODULE;
  if (!modulePath) {
    throw new Error("INSTAGRAM_PLAYWRIGHT_MODULE was not configured by the launcher.");
  }
  const require = createRequire(import.meta.url);
  return require(modulePath);
}

function csvValue(value) {
  const text = String(value ?? "").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  return `"${text.replace(/"/g, '""')}"`;
}

function normalizeReelUrl(value) {
  try {
    const url = new URL(value, "https://www.instagram.com/");
    const match = url.pathname.match(/^\/reels?\/([A-Za-z0-9_-]+)\/?/i);
    if (!match) return null;
    return {
      url: `https://www.instagram.com/reels/${match[1]}/`,
      shortcode: match[1],
    };
  } catch {
    return null;
  }
}

function parseMetricCount(value) {
  const compact = String(value ?? "")
    .trim()
    .replace(/,/g, "")
    .replace(/\s+/g, "");
  const match = compact.match(/(\d+(?:\.\d+)?)(천|만|억|[kmb])?/i);
  if (!match) return "";
  const multipliers = {
    "": 1,
    천: 1_000,
    만: 10_000,
    억: 100_000_000,
    k: 1_000,
    m: 1_000_000,
    b: 1_000_000_000,
  };
  const unit = (match[2] ?? "").toLowerCase();
  const count = Number(match[1]) * multipliers[unit];
  return Number.isFinite(count) ? Math.round(count) : "";
}

function calculateReactionRate(likeCount, followerCount) {
  const likes = parseMetricCount(likeCount);
  const followers = parseMetricCount(followerCount);
  if (likes === "" || followers === "" || followers <= 0) return "";
  return Math.round((likes / followers) * 1_000_000) / 1_000_000;
}

function parseFollowerCount(value) {
  const text = String(value ?? "").replace(/\u00a0/g, " ").trim();
  const number = "([0-9][0-9,.]*)\\s*(\\uCC9C|\\uB9CC|\\uC5B5|[KMB])?";
  const patterns = [
    new RegExp(`(?:followers?|\\uD314\\uB85C\\uC6CC)\\s*[:\\uFF1A]?\\s*${number}`, "i"),
    new RegExp(`${number}\\s*(?:followers?|\\uD314\\uB85C\\uC6CC)`, "i"),
    new RegExp(`^\\s*${number}\\s*$`, "i"),
  ];
  const match = patterns.map((pattern) => text.match(pattern)).find(Boolean);
  if (!match) return "";
  const rawNumber = match[1].replace(/,/g, "");
  const unit = String(match[2] ?? "").toLowerCase();
  const multipliers = {
    "": 1,
    "\uCC9C": 1_000,
    "\uB9CC": 10_000,
    "\uC5B5": 100_000_000,
    k: 1_000,
    m: 1_000_000,
    b: 1_000_000_000,
  };
  const count = Number(rawNumber) * multipliers[unit];
  return Number.isFinite(count) ? Math.round(count) : "";
}

function followerCountFromInstagramData(value, username, depth = 0) {
  if (!value || typeof value !== "object" || depth > 20) return "";
  const expected = String(username ?? "").toLowerCase();
  const candidateUsername = String(value.username ?? value.user_name ?? "").toLowerCase();
  if (candidateUsername && candidateUsername === expected) {
    for (const candidate of [
      value.follower_count,
      value.followers_count,
      value.edge_followed_by?.count,
    ]) {
      const count = typeof candidate === "number"
        ? Math.round(candidate)
        : parseFollowerCount(candidate);
      if (Number.isInteger(count) && count >= 0) return count;
    }
  }
  for (const child of Object.values(value)) {
    const found = followerCountFromInstagramData(child, username, depth + 1);
    if (found !== "") return found;
  }
  return "";
}

async function requestWebFollowerCount({ page, username }) {
  const normalizedUsername = String(username ?? "").trim().replace(/^@/, "");
  if (!INSTAGRAM_USERNAME_PATTERN.test(normalizedUsername)) {
    return { status: "profile_unavailable", error: "Invalid Instagram username.", source: "instagram_web" };
  }
  try {
    const response = await page.goto(
      `https://www.instagram.com/${encodeURIComponent(normalizedUsername)}/`,
      { waitUntil: "domcontentloaded", timeout: 30_000 },
    );
    if (response?.status?.() === 429) {
      return { status: "rate_limited", error: "Instagram returned HTTP 429.", source: "instagram_web" };
    }
    await page.waitForTimeout(1_500);
    const currentUrl = page.url();
    if (/\/accounts\/login/i.test(currentUrl)) {
      return { status: "login_required", error: "Instagram login is required.", source: "instagram_web" };
    }
    if (/\/(?:challenge|checkpoint)\//i.test(currentUrl)) {
      return { status: "challenge_required", error: "Instagram requested an account check.", source: "instagram_web" };
    }
    const browserData = await page.evaluate((expectedUsername) => {
      const candidates = [];
      const expectedPath = `/${expectedUsername.toLowerCase()}/followers/`;
      for (const anchor of document.querySelectorAll('a[href*="/followers/"]')) {
        let pathname = "";
        try {
          pathname = new URL(anchor.href, location.href).pathname.toLowerCase();
        } catch {
          continue;
        }
        if (pathname !== expectedPath) continue;
        for (const element of [anchor, ...anchor.querySelectorAll("span")]) {
          candidates.push(
            element.getAttribute?.("title") ?? "",
            element.getAttribute?.("aria-label") ?? "",
            element.textContent ?? "",
          );
        }
      }
      for (const element of document.querySelectorAll("header li, header section")) {
        const text = element.textContent ?? "";
        if (/(?:followers?|\uD314\uB85C\uC6CC)/i.test(text)) candidates.push(text);
      }
      candidates.push(
        document.querySelector('meta[property="og:description"]')?.content ?? "",
        document.querySelector('meta[name="description"]')?.content ?? "",
      );
      const bodyText = document.body?.innerText ?? "";
      return {
        candidates: candidates.filter(Boolean),
        bodyText: bodyText.slice(0, 8_000),
        hasLoginForm: Boolean(document.querySelector('input[name="username"], form[action*="/accounts/login"]')),
      };
    }, normalizedUsername);
    if (browserData.hasLoginForm) {
      return { status: "login_required", error: "Instagram login is required.", source: "instagram_web" };
    }
    if (/(?:try again later|please wait a few minutes|\uC7A0\uC2DC \uD6C4 \uB2E4\uC2DC)/i.test(browserData.bodyText)) {
      return { status: "rate_limited", error: "Instagram temporarily limited profile access.", source: "instagram_web" };
    }
    for (const candidate of browserData.candidates) {
      const followerCount = parseFollowerCount(candidate);
      if (followerCount !== "") {
        return { status: "success", followerCount, error: "", source: "instagram_web" };
      }
    }
    const embeddedJson = await page.locator('script[type="application/json"]').allTextContents()
      .catch(() => []);
    for (const raw of embeddedJson) {
      try {
        const followerCount = followerCountFromInstagramData(
          parseInstagramJson(raw),
          normalizedUsername,
        );
        if (followerCount !== "") {
          return { status: "success", followerCount, error: "", source: "instagram_web" };
        }
      } catch {
        // Embedded JSON is an optional fallback.
      }
    }
    const unavailable = /(?:sorry, this page isn't available|page isn't available|\uD398\uC774\uC9C0\uB97C \uC0AC\uC6A9\uD560 \uC218 \uC5C6)/i
      .test(browserData.bodyText);
    return {
      status: unavailable ? "profile_unavailable" : "web_unavailable",
      error: unavailable
        ? "Instagram profile is unavailable."
        : "Follower count was not visible on the Instagram profile.",
      source: "instagram_web",
    };
  } catch (error) {
    return {
      status: "web_error",
      error: String(error?.message ?? error).slice(0, 500),
      source: "instagram_web",
    };
  }
}

function createSequentialWebFollowerLookup(page, intervalSeconds = 8) {
  let waitBeforeNextLookupMilliseconds = 0;
  return async ({ username }) => {
    if (waitBeforeNextLookupMilliseconds) {
      await page.waitForTimeout(waitBeforeNextLookupMilliseconds);
    }
    const result = await requestWebFollowerCount({ page, username });
    waitBeforeNextLookupMilliseconds = followerLookupDelaySeconds(result, intervalSeconds) * 1_000;
    return result;
  };
}

function followerLookupDelaySeconds(result, fallbackSeconds) {
  return result?.status === "success" && parseMetricCount(result.followerCount) !== ""
    ? FAST_SUCCESS_INTERVAL_SECONDS
    : fallbackSeconds;
}

async function createBackgroundFollowerRuntime({ chromium, sourceContext, executablePath }) {
  const storageState = await sourceContext.storageState();
  const browser = await chromium.launch({ executablePath, headless: true });
  const context = await browser.newContext({
    storageState,
    viewport: { width: 1440, height: 1000 },
  });
  const page = await context.newPage();
  return { browser, context, page };
}

function truncateCaption(value, maxCharacters = 300) {
  const normalized = String(value ?? "").trim().replace(/\s+/g, " ");
  const characters = Array.from(normalized);
  if (characters.length <= maxCharacters) return normalized;
  return `${characters.slice(0, maxCharacters).join("")}...`;
}

function extractHashtags(value, additionalTags = []) {
  const candidates = [
    ...(String(value ?? "").match(/#[\p{L}\p{N}_]+/gu) ?? []),
    ...additionalTags,
  ];
  const unique = new Map();
  for (const candidate of candidates) {
    const tag = String(candidate ?? "").trim().match(/#[\p{L}\p{N}_]+/u)?.[0] ?? "";
    if (tag && !unique.has(tag.toLocaleLowerCase())) {
      unique.set(tag.toLocaleLowerCase(), tag);
    }
  }
  return [...unique.values()].join(" ");
}

function captionWithoutHashtags(value) {
  return String(value ?? "")
    .replace(/#[\p{L}\p{N}_]+/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function normalizeUploadTime(value) {
  if (value === null || value === undefined || value === "") return "";
  const numeric = typeof value === "number"
    ? value
    : /^\d+(?:\.\d+)?$/.test(String(value).trim()) ? Number(value) : NaN;
  let date;
  if (Number.isFinite(numeric)) {
    const milliseconds = numeric >= 1_000_000_000_000 ? numeric : numeric * 1000;
    date = new Date(milliseconds);
  } else {
    date = new Date(String(value));
  }
  if (Number.isNaN(date.getTime())) return "";
  const year = date.getUTCFullYear();
  if (year < 2005 || year > new Date().getUTCFullYear() + 2) return "";
  return date.toISOString();
}

function formatAudioName(artistValue, titleValue) {
  const artist = String(artistValue ?? "").trim();
  const title = String(titleValue ?? "").trim();
  if (!title) return "";
  if (!artist || title.toLocaleLowerCase().includes(artist.toLocaleLowerCase())) return title;
  return `${artist} · ${title}`;
}

function hasAdSignal(value) {
  if (value === true || value === 1) return true;
  const text = String(value ?? "").trim();
  return Boolean(text) && !/^(?:false|0|null|none)$/i.test(text);
}

function mediaIsAdvertisement(media) {
  const directSignals = [
    media?.is_ad,
    media?.is_sponsored,
    media?.is_paid_partnership,
    media?.ad_id,
    media?.ad_client_token,
    media?.ad_action,
    media?.sponsored_label,
  ];
  if (directSignals.some(hasAdSignal)) return true;
  const commercialType = `${media?.commerciality_status ?? ""} ${media?.commercial_content_type ?? ""}`
    .trim();
  if (commercialType && !/(?:^|\s)(?:not[_ -]?commercial|organic|none|false)(?:\s|$)/i.test(commercialType)
      && /(?:sponsor|paid|advertis|commercial)/i.test(commercialType)) return true;
  return [media?.sponsor_tags, media?.paid_partnership_info, media?.affiliate_info]
    .some((value) => Array.isArray(value) ? value.length > 0 : hasAdSignal(value));
}

function metadataFromMedia(media) {
  const user = media?.user ?? media?.owner ?? {};
  const clipsMetadata = media?.clips_metadata ?? {};
  const musicInfo = clipsMetadata.music_info ?? {};
  const musicAsset = musicInfo.music_asset_info ?? {};
  const originalSound = clipsMetadata.original_sound_info ?? {};
  const musicTitle = musicAsset.title ?? musicAsset.song_name ?? "";
  const musicArtist = musicAsset.display_artist
    ?? musicAsset.artist_name
    ?? musicInfo.music_consumption_info?.ig_artist?.username
    ?? "";
  const originalTitle = originalSound.original_audio_title
    ?? originalSound.audio_name
    ?? originalSound.title
    ?? "";
  const originalArtist = originalSound.ig_artist?.username
    ?? originalSound.artist?.username
    ?? media?.user?.username
    ?? "";
  const location = media?.location ?? media?.location_info ?? {};
  return {
    userId: String(user?.pk ?? user?.pk_id ?? user?.id ?? "").trim(),
    username: String(user?.username ?? "").trim(),
    caption: String(media?.caption?.text ?? media?.caption_text ?? "").trim(),
    audioName: formatAudioName(musicArtist, musicTitle)
      || formatAudioName(originalArtist, originalTitle),
    locationName: String(
      location?.name
      ?? location?.short_name
      ?? media?.location_name
      ?? "",
    ).trim(),
    ad: mediaIsAdvertisement(media),
    uploadedAt: [
      media?.taken_at,
      media?.taken_at_timestamp,
      media?.datePublished,
      media?.uploadDate,
      media?.published_at,
      media?.publish_time,
      media?.creation_time,
      media?.created_at,
    ].map(normalizeUploadTime).find(Boolean) ?? "",
    likeCount: parseMetricCount(media?.like_count),
    commentCount: parseMetricCount(media?.comment_count),
    repostCount: parseMetricCount(media?.media_repost_count ?? media?.repost_count),
  };
}

function collectReelMetadata(value, destination, depth = 0) {
  if (!value || typeof value !== "object" || depth > 16) return;
  const shortcode = String(value.code ?? value.shortcode ?? value.media_code ?? "");
  if (/^[A-Za-z0-9_-]+$/.test(shortcode)) {
    const current = destination.get(shortcode) ?? {};
    const found = metadataFromMedia(value);
    const merged = {
      userId: current.userId || found.userId,
      username: current.username || found.username,
      caption: current.caption || found.caption,
      audioName: current.audioName || found.audioName,
      locationName: current.locationName || found.locationName,
      ad: Boolean(current.ad || found.ad),
      uploadedAt: current.uploadedAt || found.uploadedAt,
      likeCount: current.likeCount !== "" && current.likeCount !== undefined
        ? current.likeCount : found.likeCount,
      commentCount: current.commentCount !== "" && current.commentCount !== undefined
        ? current.commentCount : found.commentCount,
      repostCount: current.repostCount !== "" && current.repostCount !== undefined
        ? current.repostCount : found.repostCount,
    };
    if (Object.values(merged).some(Boolean)) destination.set(shortcode, merged);
  }
  for (const child of Object.values(value)) {
    if (child && typeof child === "object") {
      collectReelMetadata(child, destination, depth + 1);
    }
  }
}

function collectReelUploadDates(value, destination, depth = 0) {
  const metadata = new Map();
  collectReelMetadata(value, metadata, depth);
  for (const [shortcode, record] of metadata) {
    if (record.uploadedAt) destination.set(shortcode, record.uploadedAt);
  }
}

async function waitForReelMetadata(shortcode, reelMetadata, timeoutMilliseconds = 1_000) {
  const deadline = Date.now() + timeoutMilliseconds;
  while (Date.now() < deadline) {
    const metadata = reelMetadata.get(shortcode);
    if (metadata) return metadata;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  return reelMetadata.get(shortcode) ?? {};
}

function buildCollectedRecord(record, responseMetadata = {}, collectedAt = new Date().toISOString()) {
  const titleCandidate = String(record.title ?? "").trim();
  const inferredUsername = !responseMetadata.userId
    && !responseMetadata.username
    && !record.userId
    && !record.username
    && !record.audioName
    && !record.uploadedAt
    && INSTAGRAM_USERNAME_PATTERN.test(titleCandidate)
    ? titleCandidate
    : "";
  const fullCaption = responseMetadata.caption || (inferredUsername ? "" : titleCandidate);
  const uploadedAt = record.uploadedAt || responseMetadata.uploadedAt || "";
  return {
    collected_at: collectedAt,
    url: record.url,
    user_id: responseMetadata.userId || record.userId || "",
    username: responseMetadata.username || record.username || inferredUsername,
    title: truncateCaption(captionWithoutHashtags(fullCaption), 300),
    hashtags: extractHashtags(fullCaption, record.hashtagTexts),
    audio_name: responseMetadata.audioName || record.audioName,
    location_name: responseMetadata.locationName || record.locationName || "",
    ad: responseMetadata.ad === true || record.ad === true ? "true" : "false",
    uploaded_at: uploadedAt,
    days_since_upload: daysSinceUpload(uploadedAt, collectedAt),
    like_count: responseMetadata.likeCount !== "" && responseMetadata.likeCount !== undefined
      ? responseMetadata.likeCount : parseMetricCount(record.likeText),
    comment_count: responseMetadata.commentCount !== "" && responseMetadata.commentCount !== undefined
      ? responseMetadata.commentCount : parseMetricCount(record.commentText),
    repost_count: responseMetadata.repostCount !== "" && responseMetadata.repostCount !== undefined
      ? responseMetadata.repostCount : parseMetricCount(record.repostText),
    follower_count: "",
    reaction_rate: "",
    follower_count_collected_at: "",
    follower_lookup_status: "",
  };
}

function hasCompleteReelCoreData(record) {
  const textFields = ["url", "user_id", "username", "uploaded_at"];
  const countFields = ["like_count", "comment_count", "repost_count"];
  return textFields.every((field) => String(record?.[field] ?? "").trim() !== "")
    && countFields.every((field) => parseMetricCount(record?.[field]) !== "")
    && ["true", "false"].includes(String(record?.ad ?? "").toLowerCase());
}

function parseCsvHeader(text) {
  const currentHeader = (text.split(/\r?\n/, 1)[0] ?? "").replace(/^\uFEFF/, "");
  return currentHeader.match(/"(?:[^"]|"")*"|[^,]+/g)
    ?.map((field) => field.replace(/^"|"$/g, "").replace(/""/g, '"'))
    ?? [];
}

function parseSnapshotField(field) {
  const match = String(field).match(
    /^(\d+(?:st|nd|rd|th) collect|\+\d+(?:Minute|Hour|Day|Weeks)(?:_\d+)?)_(.+)$/,
  );
  if (!match || !CSV_FIELDS.includes(match[2])) return null;
  return { label: match[1], baseField: match[2] };
}

function snapshotLabels(fields) {
  const labels = [];
  const seen = new Set();
  for (const field of fields) {
    const parsed = parseSnapshotField(field);
    if (parsed && !seen.has(parsed.label)) {
      seen.add(parsed.label);
      labels.push(parsed.label);
    }
  }
  return labels;
}

function elapsedSnapshotLabel(initialTimestamp, currentTimestamp) {
  const initial = new Date(initialTimestamp).getTime();
  const current = new Date(currentTimestamp).getTime();
  const elapsedMinutes = Number.isFinite(initial) && Number.isFinite(current)
    ? Math.max(1, Math.round((current - initial) / 60_000))
    : 1;
  if (elapsedMinutes < 60) return `+${elapsedMinutes}Minute`;
  const hours = Math.max(1, Math.round(elapsedMinutes / 60));
  if (hours < 24) return `+${hours}Hour`;
  const days = Math.max(1, Math.round(hours / 24));
  if (days < 7) return `+${days}Day`;
  return `+${Math.max(1, Math.round(days / 7))}Weeks`;
}

function daysSinceUpload(uploadedTimestamp, collectedTimestamp) {
  const uploaded = new Date(uploadedTimestamp).getTime();
  const collected = new Date(collectedTimestamp).getTime();
  if (!Number.isFinite(uploaded) || !Number.isFinite(collected)) return "";
  return Math.round(Math.max(0, (collected - uploaded) / 86_400_000) * 100) / 100;
}

function collectionLabel(collectionNumber) {
  const remainder100 = collectionNumber % 100;
  const remainder10 = collectionNumber % 10;
  const suffix = remainder100 >= 11 && remainder100 <= 13
    ? "th"
    : remainder10 === 1 ? "st" : remainder10 === 2 ? "nd" : remainder10 === 3 ? "rd" : "th";
  return `${collectionNumber}${suffix} collect`;
}

function schemaHeader(fields = CSV_FIELDS) {
  return fields.map(csvValue).join(",");
}

function latestFieldValue(row, fields, baseField, stopBeforeLabel = "") {
  let latest = String(row[baseField] ?? "");
  for (const label of snapshotLabels(fields)) {
    if (label === stopBeforeLabel) break;
    const value = String(row[`${label}_${baseField}`] ?? "");
    if (value !== "") latest = value;
  }
  return latest;
}

function populateReactionRates(row, fields) {
  let changed = false;
  let latestLikes = String(row.like_count ?? "");
  let latestFollowers = String(row.follower_count ?? "");
  const assign = (field, value) => {
    const normalized = value === "" ? "" : String(value);
    if (String(row[field] ?? "") !== normalized) {
      row[field] = normalized;
      changed = true;
    }
  };
  assign("reaction_rate", calculateReactionRate(latestLikes, latestFollowers));
  for (const label of snapshotLabels(fields)) {
    const likes = String(row[`${label}_like_count`] ?? "");
    const followers = String(row[`${label}_follower_count`] ?? "");
    if (likes !== "") latestLikes = likes;
    if (followers !== "") latestFollowers = followers;
    assign(
      `${label}_reaction_rate`,
      row[`${label}_collected_at`]
        ? calculateReactionRate(latestLikes, latestFollowers)
        : "",
    );
  }
  return changed;
}

function chooseSnapshotLabel(row, fields) {
  const candidates = snapshotLabels(fields);
  const available = candidates.find((label) => !row[`${label}_collected_at`]);
  if (available) return available;
  return collectionLabel(candidates.length + 2);
}

function addSnapshotColumns(fields, label) {
  for (const field of RECOLLECT_FIELDS) {
    const snapshotField = `${label}_${field}`;
    if (!fields.includes(snapshotField)) fields.push(snapshotField);
  }
}

function integrateCollectedRecord(rows, fields, record) {
  const existing = rows.find((row) => row.url === record.url);
  if (!existing) {
    const initial = Object.fromEntries(CSV_FIELDS.map((field) => [field, record[field] ?? ""]));
    initial.days_since_upload = daysSinceUpload(initial.uploaded_at, initial.collected_at);
    populateReactionRates(initial, fields);
    rows.push(initial);
    return { addedRow: true, label: "Initial" };
  }

  for (const field of ["user_id", "username"]) {
    if (!existing[field] && record[field]) existing[field] = record[field];
  }

  const label = chooseSnapshotLabel(existing, fields);
  addSnapshotColumns(fields, label);
  for (const field of RECOLLECT_FIELDS) {
    const rawValue = field === "days_since_upload"
      ? daysSinceUpload(record.uploaded_at || existing.uploaded_at, record.collected_at)
      : record[field];
    const current = String(rawValue ?? "");
    if (field === "collected_at") {
      existing[`${label}_${field}`] = current;
      continue;
    }
    if (field === "days_since_upload") {
      existing[`${label}_${field}`] = current;
      continue;
    }
    const previous = latestFieldValue(existing, fields, field, label);
    existing[`${label}_${field}`] = current && current !== previous ? current : "";
  }
  populateReactionRates(existing, fields);
  return {
    addedRow: false,
    label,
    elapsed: elapsedSnapshotLabel(existing.collected_at, record.collected_at),
  };
}

function collapseLongRows(rows) {
  const collapsed = [];
  const fields = [...CSV_FIELDS];
  for (const row of rows) integrateCollectedRecord(collapsed, fields, row);
  return { rows: collapsed, fields };
}

async function prepareCsv(csvPath) {
  if (!fs.existsSync(csvPath) || (await fsp.stat(csvPath)).size === 0) return;
  const existing = await fsp.readFile(csvPath, "utf8");
  const parsedHeader = parseCsvHeader(existing);
  const parsedRows = csvObjects(existing);
  let repairedRows = 0;
  for (const row of parsedRows) {
    const title = String(row.title ?? "").trim();
    if (!row.user_id && !row.username && INSTAGRAM_USERNAME_PATTERN.test(title)) {
      row.username = title;
      row.title = "";
      repairedRows += 1;
    }
  }
  const compatible = parsedHeader.length && parsedHeader.every(
    (field) => CSV_FIELDS.includes(field) || DROPPED_CSV_FIELDS.has(field) || parseSnapshotField(field),
  );
  if (compatible) {
    const sourceLabels = snapshotLabels(parsedHeader);
    const targetLabels = sourceLabels.map((_, index) => collectionLabel(index + 2));
    const fields = [
      ...CSV_FIELDS,
      ...targetLabels.flatMap((label) => RECOLLECT_FIELDS.map((field) => `${label}_${field}`)),
    ];
    const normalizedRows = parsedRows.map((row) => {
      const normalized = Object.fromEntries(CSV_FIELDS.map((field) => [field, row[field] ?? ""]));
      normalized.days_since_upload = row.days_since_upload
        || daysSinceUpload(normalized.uploaded_at, normalized.collected_at);
      sourceLabels.forEach((sourceLabel, index) => {
        const targetLabel = targetLabels[index];
        for (const field of RECOLLECT_FIELDS) {
          normalized[`${targetLabel}_${field}`] = field === "days_since_upload"
            ? row[`${sourceLabel}_${field}`]
              || daysSinceUpload(normalized.uploaded_at, row[`${sourceLabel}_collected_at`])
            : row[`${sourceLabel}_${field}`] ?? "";
        }
      });
      populateReactionRates(normalized, fields);
      return normalized;
    });
    if (sourceLabels.length) {
      await writeCsvRecords(csvPath, normalizedRows, fields);
      return;
    }
    const collapsed = collapseLongRows(normalizedRows);
    await writeCsvRecords(csvPath, collapsed.rows, collapsed.fields);
    console.log(`CSV 가로형 시계열 변환 완료: ${csvPath}`);
    return;
  }

  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
  const legacyPath = path.join(
    path.dirname(csvPath),
    `${path.basename(csvPath, path.extname(csvPath))}_legacy_${stamp}.csv`,
  );
  await fsp.rename(csvPath, legacyPath);
  console.log(`기존 형식 CSV 보관: ${legacyPath}`);
}

async function writeCsvRecords(csvPath, rows, fields = CSV_FIELDS) {
  await fsp.mkdir(path.dirname(csvPath), { recursive: true });
  const lines = [
    `\uFEFF${schemaHeader(fields)}`,
    ...rows.map((row) => fields.map((field) => csvValue(row[field])).join(",")),
  ];
  const temporaryPath = path.join(
    path.dirname(csvPath),
    `.${path.basename(csvPath)}.${process.pid}.${Date.now()}.tmp`,
  );
  await fsp.writeFile(temporaryPath, `${lines.join("\r\n")}\r\n`, "utf8");
  await fsp.rename(temporaryPath, csvPath);
}

async function mergeFollowerDataIntoReels(csvPath, usersPath) {
  if (!fs.existsSync(csvPath) || !fs.existsSync(usersPath)) return 0;
  await prepareCsv(csvPath);
  const reelText = await fsp.readFile(csvPath, "utf8");
  const fields = parseCsvHeader(reelText);
  const reelRows = csvObjects(reelText);
  const userRows = csvObjects(await fsp.readFile(usersPath, "utf8"));
  const byId = new Map();
  const byUsername = new Map();
  for (const user of userRows) {
    if (user.user_id) byId.set(user.user_id, user);
    if (user.username) byUsername.set(user.username.toLowerCase(), user);
  }
  let changed = 0;
  for (const reel of reelRows) {
    const reactionChangedBefore = populateReactionRates(reel, fields);
    const user = (reel.user_id && byId.get(reel.user_id))
      || (reel.username && byUsername.get(reel.username.toLowerCase()));
    if (!user) {
      if (reactionChangedBefore) changed += 1;
      continue;
    }
    const latestLabel = snapshotLabels(fields).filter(
      (label) => reel[`${label}_collected_at`],
    ).at(-1) ?? "";
    const prefix = latestLabel ? `${latestLabel}_` : "";
    if (!latestLabel && reel.follower_count) {
      if (reactionChangedBefore) changed += 1;
      continue;
    }
    const previous = reel[`${prefix}follower_count`] ?? "";
    const priorCount = latestLabel
      ? latestFieldValue(reel, fields, "follower_count", latestLabel)
      : "";
    if (user.follower_count) {
      reel[`${prefix}follower_count`] = user.follower_count !== priorCount
        ? user.follower_count : "";
    }
    if (!latestLabel) {
      reel.follower_count_collected_at = reel.follower_count
        ? user.follower_count_collected_at : "";
      reel.follower_lookup_status = user.lookup_status ?? "";
    }
    const current = reel[`${prefix}follower_count`] ?? "";
    const reactionChangedAfter = populateReactionRates(reel, fields);
    if (current !== previous || reactionChangedBefore || reactionChangedAfter) changed += 1;
  }
  if (changed) await writeCsvRecords(csvPath, reelRows, fields);
  return changed;
}

async function expandVisibleCaption(page) {
  const beforeUrl = page.url();
  const expanded = await page.evaluate(({ captionMorePattern, profileInfoPattern }) => {
    const captionMoreRegex = new RegExp(captionMorePattern, "i");
    const profileInfoRegex = new RegExp(profileInfoPattern, "i");
    const visible = (element) => {
      const rect = element.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < innerHeight;
    };
    const videos = [...document.querySelectorAll("video")].filter(visible);
    const activeVideo = videos.sort((left, right) => {
      const leftRect = left.getBoundingClientRect();
      const rightRect = right.getBoundingClientRect();
      const leftDistance = Math.hypot(
        leftRect.left + leftRect.width / 2 - innerWidth / 2,
        leftRect.top + leftRect.height / 2 - innerHeight / 2,
      );
      const rightDistance = Math.hypot(
        rightRect.left + rightRect.width / 2 - innerWidth / 2,
        rightRect.top + rightRect.height / 2 - innerHeight / 2,
      );
      return leftDistance - rightDistance;
    })[0] ?? null;
    const videoRect = activeVideo?.getBoundingClientRect() ?? null;
    const candidates = [...document.querySelectorAll('button, [role="button"], span')]
      .filter(visible)
      .map((element) => ({
        element,
        clickable: element.closest('button, [role="button"]') ?? element,
        text: (element.innerText ?? element.textContent ?? "").trim().replace(/\s+/g, " "),
      }))
      .map((candidate) => ({
        ...candidate,
        clickableText: (
          candidate.clickable.innerText
          ?? candidate.clickable.textContent
          ?? ""
        ).trim().replace(/\s+/g, " "),
        label: [
          candidate.clickable.getAttribute?.("aria-label") ?? "",
          candidate.clickable.getAttribute?.("title") ?? "",
        ].join(" ").trim(),
      }))
      .filter(({ text }) => captionMoreRegex.test(text))
      .filter(({ text, clickableText, label }) => (
        !profileInfoRegex.test(`${text} ${clickableText} ${label}`)
      ))
      .filter(({ clickable }) => !clickable.closest('a[href]'))
      .filter(({ element }) => {
        if (!videoRect) return true;
        const rect = element.getBoundingClientRect();
        const centerY = rect.top + rect.height / 2;
        return centerY >= videoRect.top + videoRect.height * 0.35
          && centerY <= videoRect.bottom + 120;
      })
      .sort((left, right) => {
        if (!videoRect) return left.text.length - right.text.length;
        const distance = ({ element }) => {
          const rect = element.getBoundingClientRect();
          return Math.hypot(
            rect.left + rect.width / 2 - (videoRect.left + videoRect.width / 2),
            rect.top + rect.height / 2 - (videoRect.bottom - videoRect.height * 0.15),
          );
        };
        return distance(left) - distance(right);
      });
    const candidate = candidates[0] ?? null;
    if (!candidate) return false;
    candidate.clickable.click();
    return true;
  }, {
    captionMorePattern: CAPTION_MORE_TEXT_PATTERN.source,
    profileInfoPattern: PROFILE_INFO_TEXT_PATTERN.source,
  });
  if (expanded) {
    await page.waitForTimeout(400);
    if (isInstagramReelsSurface(beforeUrl) && !isInstagramReelsSurface(page.url())) {
      console.warn("캡션 확장 중 릴스를 벗어나 원래 화면으로 복귀합니다.");
      await page.goto(beforeUrl, { waitUntil: "domcontentloaded" });
      await page.waitForTimeout(500);
      return false;
    }
  }
  return expanded;
}

async function extractVisibleReel(page) {
  const browserData = await page.evaluate(() => {
    const visible = (element) => {
      const rect = element.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < innerHeight;
    };
    const centerDistance = (element, referenceRect = null) => {
      const rect = element.getBoundingClientRect();
      const x = referenceRect ? referenceRect.left + referenceRect.width / 2 : innerWidth / 2;
      const y = referenceRect ? referenceRect.top + referenceRect.height / 2 : innerHeight / 2;
      return Math.hypot(rect.left + rect.width / 2 - x, rect.top + rect.height / 2 - y);
    };
    const textOf = (element) => (element?.innerText ?? element?.textContent ?? "").trim();
    const labelOf = (element) => [
      element?.getAttribute?.("aria-label") ?? "",
      element?.getAttribute?.("title") ?? "",
      element?.querySelector?.("[aria-label]")?.getAttribute("aria-label") ?? "",
      element?.querySelector?.("title")?.textContent ?? "",
    ].join(" ").trim().toLowerCase();
    const activeVideo = [...document.querySelectorAll("video")]
      .filter(visible)
      .sort((left, right) => centerDistance(left) - centerDistance(right))[0] ?? null;
    const videoRect = activeVideo?.getBoundingClientRect() ?? null;
    const reelLinks = [...document.querySelectorAll('a[href*="/reel/"] , a[href*="/reels/"]')]
      .filter(visible)
      .map((element) => {
        const rect = element.getBoundingClientRect();
        return { element, distance: Math.abs(rect.top + rect.height / 2 - innerHeight / 2) };
      })
      .sort((left, right) => left.distance - right.distance);
    const currentMatch = location.pathname.match(/^\/reels?\/([A-Za-z0-9_-]+)\/?/i);
    const activeLink = reelLinks.find(({ element }) => {
      if (!currentMatch) return false;
      return element.getAttribute("href")?.includes(`/${currentMatch[1]}/`);
    })?.element ?? reelLinks[0]?.element ?? null;

    const controlCandidates = [...document.querySelectorAll('button, [role="button"], svg[aria-label], [title]')]
      .filter(visible);
    const findControls = (terms) => controlCandidates
      .filter((element) => terms.some((term) => labelOf(element).includes(term)))
      .sort((left, right) => centerDistance(left, videoRect) - centerDistance(right, videoRect));
    const likeControls = findControls(["좋아요", "like", "unlike"]);
    const commentControls = findControls(["댓글", "comment"]);
    const repostControls = findControls(["리포스트", "repost"]);

    const commonAncestor = (elements) => {
      const usable = elements.filter(Boolean);
      if (!usable.length) return null;
      let candidate = usable[0];
      while (candidate && !usable.every((element) => candidate.contains(element))) {
        candidate = candidate.parentElement;
      }
      return candidate;
    };
    let scope = activeLink?.closest("article")
      ?? commonAncestor([activeVideo, likeControls[0], commentControls[0], repostControls[0]])
      ?? activeVideo?.closest("article")
      ?? document.querySelector("main")
      ?? document.body;

    // Feed layouts often place the author/caption beside the video, one level above
    // the video/action common ancestor. Expand only while the candidate stays local
    // to the Reel instead of swallowing the global navigation sidebar.
    for (let depth = 0; depth < 3 && scope.parentElement; depth += 1) {
      const parent = scope.parentElement;
      const rect = parent.getBoundingClientRect();
      if (rect.height > innerHeight * 1.7 || rect.width > innerWidth * 0.92) break;
      scope = parent;
    }

    const metricToken = (value) => {
      const matches = String(value ?? "").match(/\d+(?:[.,]\d+)*(?:\s*(?:천|만|억|[KMB]))?/gi) ?? [];
      return matches.sort((left, right) => right.length - left.length)[0]?.replace(/\s+/g, "") ?? "";
    };
    const metricFromControls = (controls) => {
      for (const control of controls) {
        let candidate = control;
        for (let depth = 0; candidate && depth < 5; depth += 1) {
          const token = metricToken(textOf(candidate));
          if (token) return token;
          candidate = candidate.parentElement;
        }
      }
      return "";
    };

    const pathSegmentsToSkip = new Set([
      "about", "accounts", "direct", "explore", "reel", "reels", "stories",
    ]);
    const profileAnchors = [...scope.querySelectorAll('a[href^="/"]')]
      .filter(visible)
      .map((element) => {
        const parts = element.getAttribute("href")?.split("/").filter(Boolean) ?? [];
        return { element, parts };
      })
      .filter(({ parts }) => parts.length === 1 && !pathSegmentsToSkip.has(parts[0].toLowerCase()))
      .filter(({ element }) => {
        if (!videoRect) return true;
        const rect = element.getBoundingClientRect();
        const y = rect.top + rect.height / 2;
        return y >= videoRect.top - 80 && y <= videoRect.bottom + 80;
      })
      .sort((left, right) => centerDistance(left.element, videoRect) - centerDistance(right.element, videoRect));
    const username = profileAnchors[0]?.parts[0] ?? "";

    const audioAnchors = [...scope.querySelectorAll('a[href*="/audio/"]')]
      .filter(visible)
      .sort((left, right) => centerDistance(left, videoRect) - centerDistance(right, videoRect));
    const allLines = textOf(scope).split(/\n+/).map((line) => line.trim()).filter(Boolean);
    const audioLine = allLines.find((line) => /(?:♬|🎵|원본\s*오디오|original\s*audio)/i.test(line)) ?? "";
    const audioName = (textOf(audioAnchors[0]) || audioLine)
      .replace(/^[\s♬🎵♫·•-]+/, "")
      .trim();
    const locationAnchors = [...scope.querySelectorAll('a[href*="/explore/locations/"]')]
      .filter(visible)
      .sort((left, right) => centerDistance(left, videoRect) - centerDistance(right, videoRect));
    const locationName = textOf(locationAnchors[0]).replace(/\s+/g, " ").trim();
    const adLabelPattern = /^(?:광고|후원됨|sponsored|paid\s+partnership(?:\s+with\s+.+)?)$/i;
    const ad = [...scope.querySelectorAll('span, a, button, [role="button"]')]
      .filter(visible)
      .some((element) => {
        const label = [
          textOf(element),
          element.getAttribute?.("aria-label") ?? "",
          element.getAttribute?.("title") ?? "",
        ].map((value) => value.trim().replace(/\s+/g, " "));
        return label.some((value) => adLabelPattern.test(value));
      }) || Boolean(scope.querySelector('[data-ad-id], [data-ad-preview], a[href*="/ads/"]'));

    const timeCandidates = [...scope.querySelectorAll("time"), ...document.querySelectorAll("main time")]
      .filter((element, index, array) => visible(element) && array.indexOf(element) === index)
      .sort((left, right) => centerDistance(left, videoRect) - centerDistance(right, videoRect));
    const timeElement = timeCandidates[0] ?? null;
    const currentIsReel = /^\/reels?\/[A-Za-z0-9_-]+\/?/i.test(location.pathname);
    const metadataPublishedAt = currentIsReel
      ? document.querySelector('meta[property="article:published_time"]')?.getAttribute("content") ?? ""
      : "";
    const uploadedAt = timeElement?.getAttribute("datetime")
      || timeElement?.getAttribute("title")
      || textOf(timeElement)
      || metadataPublishedAt;
    const uploadedAtText = textOf(timeElement);

    const excludedLabels = /^(?:팔로우|follow|좋아요|likes?|댓글|comments?|리포스트|reposts?|공유|share|send|저장|save|옵션|options?)$/i;
    const cleanCaption = (value) => value
      .split(/\n+/)
      .map((line) => line.trim().replace(/(?:…|\.\.\.)?\s*(?:더\s*보기|more)$/i, "").trim())
      .filter(Boolean)
      .filter((line) => (
        line !== username && line !== audioName && line !== uploadedAtText && line !== locationName
      ))
      .filter((line) => !(username && line.startsWith(username) && /(?:팔로우|follow)/i.test(line)))
      .filter((line) => !(audioName && line.includes(audioName)))
      .filter((line) => !/(?:♬|🎵|원본\s*오디오|original\s*audio)/i.test(line))
      .filter((line) => !excludedLabels.test(line))
      .filter((line) => !adLabelPattern.test(line))
      .filter((line) => !/^\d+(?:[.,]\d+)*(?:천|만|억|[KMB])?$/i.test(line))
      .join(" ")
      .replace(/\s+/g, " ")
      .trim();
    const titleCandidates = [...scope.querySelectorAll("span")]
      .filter(visible)
      .map((element) => ({ element, text: cleanCaption(textOf(element)) }))
      .filter(({ text }) => text && text.length <= 10_000)
      .filter(({ element }) => {
        if (!videoRect) return true;
        const rect = element.getBoundingClientRect();
        const y = rect.top + rect.height / 2;
        return y >= videoRect.top + videoRect.height * 0.45 && y <= videoRect.bottom + 100;
      })
      .sort((left, right) => {
        const hashtagBonus = (value) => (value.includes("#") ? 2_000 : 0);
        return right.text.length + hashtagBonus(right.text)
          - (left.text.length + hashtagBonus(left.text));
      });
    const title = titleCandidates[0]?.text ?? "";
    const hashtagTexts = [...scope.querySelectorAll('a[href*="/explore/tags/"]')]
      .filter(visible)
      .map((element) => textOf(element))
      .filter((text) => text.startsWith("#"));
    return {
      currentUrl: location.href,
      activeHref: activeLink?.href ?? "",
      username,
      title,
      hashtagTexts,
      audioName,
      locationName,
      ad,
      uploadedAt,
      likeText: metricFromControls(likeControls),
      commentText: metricFromControls(commentControls),
      repostText: metricFromControls(repostControls),
    };
  });
  const normalized = normalizeReelUrl(browserData.currentUrl) ?? normalizeReelUrl(browserData.activeHref);
  return normalized ? { ...browserData, ...normalized } : null;
}

async function appendRecord(csvPath, record) {
  await prepareCsv(csvPath);
  let fields = [...CSV_FIELDS];
  let rows = [];
  if (fs.existsSync(csvPath) && (await fsp.stat(csvPath)).size > 0) {
    const existing = await fsp.readFile(csvPath, "utf8");
    fields = parseCsvHeader(existing);
    rows = csvObjects(existing);
  }
  const result = integrateCollectedRecord(rows, fields, record);
  await writeCsvRecords(csvPath, rows, fields);
  return result;
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const projectDir = path.dirname(fileURLToPath(import.meta.url));
  loadDotEnvFile(path.join(projectDir, ".env"));
  await fsp.mkdir(options.dataDir, { recursive: true });
  const csvPath = path.join(options.dataDir, "reels_web.csv");
  const { chromium } = loadPlaywright();
  const refreshUrls = !options.followersOnly && options.urlsFile
    ? await loadReelUrls(options.urlsFile)
    : [];
  const startUrl = refreshUrls[0]
    ?? (options.hashtags[0] ? hashtagPageUrl(options.hashtags[0]) : options.startUrl);
  await fsp.mkdir(options.profileDir, { recursive: true });
  if (!options.followersOnly) await prepareCsv(csvPath);
  // Deduplicate only inside this run. A later run may revisit the same URL and
  // append a new engagement snapshot for time-series analysis.
  const seen = new Set();
  const executablePath = process.env.INSTAGRAM_BROWSER_EXECUTABLE;
  if (!executablePath || !fs.existsSync(executablePath)) {
    throw new Error("A supported Chrome or Edge executable was not found.");
  }

  const context = await chromium.launchPersistentContext(options.profileDir, {
    executablePath,
    headless: options.background,
    viewport: options.background ? { width: 1440, height: 1000 } : null,
    args: options.background ? [] : ["--start-maximized"],
  });
  // Always create the collection page after response listeners are ready.
  // Reusing Edge's restored tab can show a cached Reel without emitting the
  // matching metadata response, which makes caption/audio fields unavailable.
  const restoredPages = context.pages();
  const page = options.followersOnly ? null : await context.newPage();
  let followerRuntime = null;
  let followerEnricher = null;
  const startFollowerEnricher = async () => {
    followerRuntime = await createBackgroundFollowerRuntime({
      chromium,
      sourceContext: context,
      executablePath,
    });
    followerEnricher = new FollowerEnricher({
      dataDir: options.dataDir,
      concurrency: 1,
      cacheHours: options.followerCacheHours,
      lookupImpl: createSequentialWebFollowerLookup(
        followerRuntime.page,
        options.followerIntervalSeconds,
      ),
      source: "instagram_web",
      onProgress: ({ completed, queued, username, status, followerCount, error }) => {
        const outcome = status === "success"
          ? Number(followerCount).toLocaleString("en-US")
          : `${status}${error ? ` (${error})` : ""}`;
        console.log(`[Follower ${completed}/${queued}] @${username} -> ${outcome}`);
      },
    });
    return followerEnricher;
  };
  const reelMetadata = new Map();
  page?.on("response", async (response) => {
    try {
      const responseUrl = new URL(response.url());
      const contentType = response.headers()["content-type"] ?? "";
      if (!responseUrl.hostname.endsWith("instagram.com") || !contentType.includes("json")) return;
      collectReelMetadata(parseInstagramJson(await response.text()), reelMetadata);
    } catch {
      // Some response bodies are unavailable after navigation; they are optional fallbacks.
    }
  });
  await Promise.all(restoredPages.map((restoredPage) => restoredPage.close().catch(() => {})));
  if (options.followersOnly) {
    await startFollowerEnricher();
    await context.close();
    const queued = await followerEnricher.enqueueAll({ force: options.forceFollowers });
    console.log(`Follower web lookups queued: ${queued}`);
    const stats = await followerEnricher.drain();
    console.log(
      `Follower web lookups finished: success=${stats.success} unavailable=${stats.unavailable} failed=${stats.failed}`,
    );
    if (stats.stopStatus) {
      console.error(`Follower web lookup stopped (${stats.stopStatus}): ${stats.stopError}`);
      process.exitCode = 2;
    }
    const merged = await mergeFollowerDataIntoReels(
      csvPath,
      path.join(options.dataDir, "users.csv"),
    );
    console.log(`Follower data merged into reels_web.csv: ${merged}`);
    await followerRuntime.browser.close();
    return;
  }
  await page.goto(startUrl, { waitUntil: "domcontentloaded" });
  let prompt = null;
  if (options.background) {
    const expectedSurface = options.hashtags.length
      ? isInstagramHashtagSurface(page.url())
      : isInstagramReelsSurface(page.url());
    if (!expectedSurface) {
      await context.close();
      throw new Error(
        "Background mode needs a saved Instagram login. Run without --background, sign in once, and retry.",
      );
    }
    console.log("Background mode started with the saved Instagram browser profile.");
  } else {
    prompt = readline.createInterface({ input: process.stdin, output: process.stdout });
    console.log("브라우저에서 Instagram에 로그인하고 릴스 화면을 연 뒤 이 창으로 돌아오세요.");
    await prompt.question("준비가 끝났으면 Enter를 누르세요: ");
  }
  await startFollowerEnricher();

  let nextDelaySeconds = options.intervalSeconds;
  const captureCurrentReel = async () => {
    nextDelaySeconds = options.intervalSeconds;
    await expandVisibleCaption(page);
    const record = await extractVisibleReel(page);
    if (!record || seen.has(record.shortcode)) return null;
    let responseMetadata = await waitForReelMetadata(record.shortcode, reelMetadata);
    if (!responseMetadata.userId || !responseMetadata.username) {
      const embeddedJson = await page.locator('script[type="application/json"]').allTextContents()
        .catch(() => []);
      for (const raw of embeddedJson) {
        try {
          collectReelMetadata(parseInstagramJson(raw), reelMetadata);
        } catch {
          // Embedded JSON is an optional fallback for cached Reels.
        }
      }
      responseMetadata = reelMetadata.get(record.shortcode) ?? responseMetadata;
    }
    const collectedRecord = buildCollectedRecord(record, responseMetadata);
    nextDelaySeconds = hasCompleteReelCoreData(collectedRecord)
      ? FAST_SUCCESS_INTERVAL_SECONDS
      : options.intervalSeconds;
    if (!hasAnyHashtag(collectedRecord.hashtags, options.hashtags)) {
      seen.add(record.shortcode);
      return { ...record, filteredOut: true };
    }
    const userState = await followerEnricher.trackUser({
      userId: collectedRecord.user_id,
      username: collectedRecord.username,
      seenAt: collectedRecord.collected_at,
    });
    if (userState) {
      collectedRecord.follower_count = userState.follower_count ?? "";
      collectedRecord.follower_count_collected_at = userState.follower_count_collected_at ?? "";
      collectedRecord.follower_lookup_status = userState.lookup_status ?? "";
    }
    const stored = await appendRecord(csvPath, collectedRecord);
    seen.add(record.shortcode);
    return {
      ...record,
      snapshotLabel: stored.label,
      collectionComplete: hasCompleteReelCoreData(collectedRecord),
    };
  };

  const hashtagUrls = options.hashtags.length
    ? await collectHashtagReelUrls(page, options.hashtags, options.maxItems)
    : [];
  const directUrls = refreshUrls.length ? refreshUrls : hashtagUrls;
  if (refreshUrls.length || options.hashtags.length) {
    let captured = 0;
    for (let index = 0; index < directUrls.length; index += 1) {
      if (options.hashtags.length && captured >= options.maxItems) break;
      const url = directUrls[index];
      try {
        if (index > 0 || !isInstagramReelsSurface(page.url())) {
          await page.goto(url, { waitUntil: "domcontentloaded" });
        }
        if (options.manual) {
          await prompt.question("현재 릴스를 다시 수집하려면 Enter를 누르세요: ");
        } else {
          await page.waitForTimeout(nextDelaySeconds * 1000);
        }
        const record = await captureCurrentReel();
        if (record?.filteredOut) {
          console.log(`[${index + 1}/${directUrls.length}] Hashtag mismatch skipped: ${url}`);
          continue;
        }
        if (record) {
          captured += 1;
          console.log(
            `[${index + 1}/${directUrls.length}] 수집 (${record.snapshotLabel}): ${record.url}`,
          );
        } else {
          console.warn(`[${index + 1}/${directUrls.length}] 수집 실패: ${url}`);
        }
      } catch (error) {
        console.warn(
          `[${index + 1}/${directUrls.length}] 수집 실패: ${url} (${error instanceof Error ? error.message : String(error)})`,
        );
      }
    }
    if (options.hashtags.length) {
      console.log(
        `Hashtag OR collection finished: matched=${captured}, requested=${options.maxItems}`,
      );
    }
  } else {
    let captured = 0;
    let noNewItemCount = 0;
    let lastReelsUrl = page.url();
    while (captured < options.maxItems) {
      if (options.manual) {
        await prompt.question("현재 릴스를 저장하려면 Enter를 누르세요: ");
      } else {
        await page.waitForTimeout(nextDelaySeconds * 1000);
      }

      if (!isInstagramReelsSurface(page.url())) {
        console.warn("릴스 화면을 벗어나 마지막 릴스로 복귀합니다.");
        await page.goto(lastReelsUrl, { waitUntil: "domcontentloaded" });
        await page.waitForTimeout(500);
      }
      const record = await captureCurrentReel();
      if (record) {
        lastReelsUrl = record.url;
        captured += 1;
        noNewItemCount = 0;
        console.log(`[${captured}/${options.maxItems}] ${record.url}`);
      } else {
        noNewItemCount += 1;
        console.log("새 릴스 URL을 찾지 못했습니다. 화면이 릴스에 있는지 확인하세요.");
      }

      if (!options.manual && captured < options.maxItems) {
        await page.keyboard.press("ArrowDown");
        if (noNewItemCount >= 3) {
          await page.mouse.wheel(0, 900);
          noNewItemCount = 0;
        }
      }
    }
  }
  prompt?.close();
  await context.close();
  console.log("릴스 수집 창을 닫았습니다. 남은 팔로워 조회를 백그라운드에서 처리합니다.");
  const followerStats = await followerEnricher.drain();
  console.log(
    `Follower web: success=${followerStats.success} unavailable=${followerStats.unavailable} failed=${followerStats.failed}`,
  );
  if (followerStats.stopStatus) {
    console.error(
      `Follower web lookup stopped (${followerStats.stopStatus}): ${followerStats.stopError}`,
    );
  }
  const merged = await mergeFollowerDataIntoReels(
    csvPath,
    path.join(options.dataDir, "users.csv"),
  );
  console.log(`Follower data merged into reels_web.csv: ${merged}`);
  await followerRuntime.browser.close();
  console.log(`저장 완료: ${csvPath}`);
}

export {
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
  createSequentialWebFollowerLookup,
  daysSinceUpload,
  elapsedSnapshotLabel,
  extractHashtags,
  followerLookupDelaySeconds,
  hasAnyHashtag,
  hasCompleteReelCoreData,
  hashtagPageUrl,
  isCaptionMoreText,
  isInstagramHashtagSurface,
  isInstagramReelsSurface,
  isProfileInfoText,
  loadReelUrls,
  followerCountFromInstagramData,
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
};

if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url))) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  });
}
