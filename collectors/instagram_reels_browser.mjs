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

const REEL_SUCCESS_INTERVAL_SECONDS = 0.5;
const FOLLOWER_SUCCESS_INTERVAL_SECONDS = 0.3;
const FOLLOWER_PROFILE_SETTLE_MILLISECONDS = 700;
const FOLLOWER_PAGE_RECYCLE_LOOKUP_COUNT = 500;
const REEL_STORE_FLUSH_RECORD_COUNT = 100;
const REEL_PAGE_RECYCLE_ITEM_COUNT = 200;
const REEL_TRANSITION_TIMEOUT_SECONDS = 3;
const REEL_TRANSITION_POLL_MILLISECONDS = 100;
const REEL_TRANSITION_SETTLE_MILLISECONDS = 200;
const REEL_UNPRODUCTIVE_RECYCLE_THRESHOLD = 8;
const REEL_MAX_CONSECUTIVE_RECOVERY_FAILURES = 6;
const REEL_STATUS_WRITE_INTERVAL_MILLISECONDS = 60_000;
const RECOLLECT_COOLDOWN_MILLISECONDS = 6 * 60 * 60 * 1_000;

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
    followerIntervalSeconds: 8,
    maxUploadAgeDays: 0,
    followersAfterReels: false,
    pageRecycleItems: REEL_PAGE_RECYCLE_ITEM_COUNT,
    checkpointItems: REEL_STORE_FLUSH_RECORD_COUNT,
    transitionTimeoutSeconds: REEL_TRANSITION_TIMEOUT_SECONDS,
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
    else if (value === "--start-url") options.startUrl = argv[++index];
    else if (value === "--max-items") options.maxItems = Number(argv[++index]);
    else if (value === "--interval-seconds") options.intervalSeconds = Number(argv[++index]);
    else if (value === "--data-dir") options.dataDir = path.resolve(argv[++index]);
    else if (value === "--profile-dir") options.profileDir = path.resolve(argv[++index]);
    else if (value === "--follower-interval-seconds") options.followerIntervalSeconds = Number(argv[++index]);
    else if (value === "--max-upload-age-days") options.maxUploadAgeDays = Number(argv[++index]);
    else if (value === "--followers-after-reels") options.followersAfterReels = true;
    else if (value === "--page-recycle-items") options.pageRecycleItems = Number(argv[++index]);
    else if (value === "--checkpoint-items") options.checkpointItems = Number(argv[++index]);
    else if (value === "--transition-timeout-seconds") options.transitionTimeoutSeconds = Number(argv[++index]);
    else if (value === "--hashtag-query") options.hashtags = parseHashtagQuery(argv[++index]);
    else if (value === "--urls-file") options.urlsFile = path.resolve(argv[++index]);
    else throw new Error(`Unknown argument: ${value}`);
  }
  if (!Number.isInteger(options.maxItems) || options.maxItems < 0) {
    throw new Error("--max-items must be a non-negative integer.");
  }
  if (!Number.isFinite(options.intervalSeconds) || options.intervalSeconds < 1) {
    throw new Error("--interval-seconds must be at least 1.");
  }
  if (options.manual && options.background) {
    throw new Error("--manual cannot be combined with --background.");
  }
  if (options.maxItems === 0 && options.hashtags.length) {
    throw new Error("--max-items 0 is only supported for the Reels feed, not hashtag collection.");
  }
  if (!Number.isFinite(options.followerIntervalSeconds) || options.followerIntervalSeconds < 1) {
    throw new Error("--follower-interval-seconds must be at least 1.");
  }
  if (!Number.isFinite(options.maxUploadAgeDays) || options.maxUploadAgeDays < 0) {
    throw new Error("--max-upload-age-days must be 0 or greater.");
  }
  if (!Number.isInteger(options.pageRecycleItems) || options.pageRecycleItems < 0) {
    throw new Error("--page-recycle-items must be a non-negative integer.");
  }
  if (!Number.isInteger(options.checkpointItems) || options.checkpointItems < 1) {
    throw new Error("--checkpoint-items must be a positive integer.");
  }
  if (
    !Number.isFinite(options.transitionTimeoutSeconds)
    || options.transitionTimeoutSeconds < 0.5
    || options.transitionTimeoutSeconds > 30
  ) {
    throw new Error("--transition-timeout-seconds must be between 0.5 and 30.");
  }
  if (!/^https:\/\/(?:www\.)?instagram\.com\//i.test(options.startUrl)) {
    throw new Error("--start-url must be an https://www.instagram.com/ URL.");
  }
  return options;
}

function createBackgroundStopInputListener(requestStop) {
  let bufferedInput = "";
  return (chunk) => {
    bufferedInput += String(chunk ?? "");
    const lastNewline = bufferedInput.lastIndexOf("\n");
    if (lastNewline < 0) return;
    const submittedLines = bufferedInput.slice(0, lastNewline).split(/\r?\n/);
    bufferedInput = bufferedInput.slice(lastNewline + 1);
    if (submittedLines.some((line) => line.trim())) requestStop();
  };
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
    await page.waitForTimeout(FOLLOWER_PROFILE_SETTLE_MILLISECONDS);
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
  const context = typeof page.context === "function" ? page.context() : null;
  let activePage = page;
  let completedSinceRecycle = 0;
  let waitBeforeNextLookupMilliseconds = 0;
  const pageIsClosed = () => (
    typeof activePage?.isClosed === "function" && activePage.isClosed()
  );
  const replacePage = async () => {
    if (!context) throw new Error("Follower browser context is unavailable.");
    const previousPage = activePage;
    activePage = await context.newPage();
    await previousPage?.close().catch(() => {});
  };
  return async ({ username }) => {
    if (waitBeforeNextLookupMilliseconds) {
      if (!pageIsClosed() && typeof activePage?.waitForTimeout === "function") {
        await activePage.waitForTimeout(waitBeforeNextLookupMilliseconds);
      } else {
        await new Promise((resolve) => setTimeout(resolve, waitBeforeNextLookupMilliseconds));
      }
    }
    if (pageIsClosed()) {
      try {
        await replacePage();
      } catch (error) {
        return {
          status: "web_error",
          error: `Follower page recovery failed: ${String(error?.message ?? error).slice(0, 400)}`,
          source: "instagram_web",
        };
      }
    }
    let result = await requestWebFollowerCount({ page: activePage, username });
    if (result.status === "web_error") {
      try {
        await replacePage();
        result = await requestWebFollowerCount({ page: activePage, username });
      } catch (error) {
        result = {
          status: "web_error",
          error: `Follower page retry failed: ${String(error?.message ?? error).slice(0, 400)}`,
          source: "instagram_web",
        };
      }
    }
    completedSinceRecycle += 1;
    if (
      completedSinceRecycle >= FOLLOWER_PAGE_RECYCLE_LOOKUP_COUNT
      && !["rate_limited", "login_required", "challenge_required"].includes(result.status)
    ) {
      try {
        await replacePage();
        completedSinceRecycle = 0;
      } catch {
        // A failed preventative recycle is retried before the next lookup.
      }
    }
    waitBeforeNextLookupMilliseconds = followerLookupDelaySeconds(result, intervalSeconds) * 1_000;
    return result;
  };
}

function followerLookupDelaySeconds(result, fallbackSeconds) {
  return result?.status === "success" && parseMetricCount(result.followerCount) !== ""
    ? FOLLOWER_SUCCESS_INTERVAL_SECONDS
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

function isWithinUploadAgeDays(record, maxUploadAgeDays) {
  if (!(maxUploadAgeDays > 0)) return true;
  const uploaded = new Date(record?.uploaded_at).getTime();
  const collected = new Date(record?.collected_at).getTime();
  if (!Number.isFinite(uploaded) || !Number.isFinite(collected)) return false;
  return Math.max(0, collected - uploaded) <= maxUploadAgeDays * 86_400_000;
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

function latestCollectionTimestamp(row, fields) {
  const timestamps = [row.collected_at];
  for (const label of snapshotLabels(fields)) {
    timestamps.push(row[`${label}_collected_at`]);
  }
  return timestamps.reduce((latest, value) => {
    const timestamp = new Date(value).getTime();
    return Number.isFinite(timestamp) && timestamp > latest ? timestamp : latest;
  }, Number.NEGATIVE_INFINITY);
}

function collectedRecordCooldown(existing, fields, record) {
  if (!existing) return null;
  const recordTimestamp = new Date(record.collected_at).getTime();
  const previousTimestamp = latestCollectionTimestamp(existing, fields);
  if (
    Number.isFinite(recordTimestamp)
    && Number.isFinite(previousTimestamp)
    && recordTimestamp >= previousTimestamp
    && recordTimestamp - previousTimestamp < RECOLLECT_COOLDOWN_MILLISECONDS
  ) {
    return {
      addedRow: false,
      skipped: true,
      label: "Cooldown",
      secondsSincePreviousCollection: Math.floor((recordTimestamp - previousTimestamp) / 1_000),
    };
  }
  return null;
}

function integrateCollectedRecord(
  rows,
  fields,
  record,
  { enforceCooldown = true, rowByUrl = null } = {},
) {
  const existing = rowByUrl?.get(record.url) ?? rows.find((row) => row.url === record.url);
  if (!existing) {
    const initial = Object.fromEntries(CSV_FIELDS.map((field) => [field, record[field] ?? ""]));
    initial.days_since_upload = daysSinceUpload(initial.uploaded_at, initial.collected_at);
    populateReactionRates(initial, fields);
    rows.push(initial);
    rowByUrl?.set(record.url, initial);
    return { addedRow: true, label: "Initial" };
  }

  const cooldown = enforceCooldown ? collectedRecordCooldown(existing, fields, record) : null;
  if (cooldown) return cooldown;

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
  for (const row of rows) integrateCollectedRecord(collapsed, fields, row, { enforceCooldown: false });
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

async function writeJsonAtomic(destination, value) {
  await fsp.mkdir(path.dirname(destination), { recursive: true });
  const temporaryPath = path.join(
    path.dirname(destination),
    `.${path.basename(destination)}.${process.pid}.${Date.now()}.tmp`,
  );
  await fsp.writeFile(temporaryPath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
  await fsp.rename(temporaryPath, destination);
}

function createCollectorStatusReporter(dataDir, options) {
  const destination = path.join(dataDir, "collector_status.json");
  let status = {
    state: "starting",
    pid: process.pid,
    started_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    target_items: options.maxItems,
    captured: 0,
    duplicates: 0,
    missing: 0,
    filtered: 0,
    cooldown_skipped: 0,
    page_recycles: 0,
    recovery_failures: 0,
    last_reel_url: "",
    last_error: "",
  };
  let lastWriteAt = 0;
  let writeChain = Promise.resolve();

  const persist = ({ force = false } = {}) => {
    const now = Date.now();
    if (!force && now - lastWriteAt < REEL_STATUS_WRITE_INTERVAL_MILLISECONDS) {
      return writeChain;
    }
    lastWriteAt = now;
    status.updated_at = new Date(now).toISOString();
    const snapshot = { ...status };
    writeChain = writeChain.catch(() => {}).then(() => writeJsonAtomic(destination, snapshot));
    return writeChain;
  };

  return {
    destination,
    snapshot() {
      return { ...status };
    },
    update(patch, options = {}) {
      status = { ...status, ...patch };
      return persist(options);
    },
    async finish(state, patch = {}) {
      status = {
        ...status,
        ...patch,
        state,
        finished_at: new Date().toISOString(),
      };
      await persist({ force: true });
      await writeChain;
    },
  };
}

function processIsAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code === "EPERM";
  }
}

async function acquireCollectorLock(dataDir) {
  const lockPath = path.join(dataDir, "collector.lock.json");
  const lock = {
    pid: process.pid,
    started_at: new Date().toISOString(),
    data_dir: path.resolve(dataDir),
  };
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      await fsp.writeFile(lockPath, `${JSON.stringify(lock, null, 2)}\n`, {
        encoding: "utf8",
        flag: "wx",
      });
      return async () => {
        try {
          const current = JSON.parse(await fsp.readFile(lockPath, "utf8"));
          if (current?.pid === process.pid) await fsp.rm(lockPath, { force: true });
        } catch (error) {
          if (error?.code !== "ENOENT") throw error;
        }
      };
    } catch (error) {
      if (error?.code !== "EEXIST") throw error;
      let existing = null;
      try {
        existing = JSON.parse(await fsp.readFile(lockPath, "utf8"));
      } catch {
        // A malformed lock is stale and can be replaced.
      }
      if (existing?.pid && processIsAlive(Number(existing.pid))) {
        throw new Error(
          `Another collector is already using this data directory (PID ${existing.pid}).`,
        );
      }
      await fsp.rm(lockPath, { force: true });
    }
  }
  throw new Error(`Could not acquire collector lock: ${lockPath}`);
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

async function readActiveReelIdentity(page) {
  const browserData = await page.evaluate(() => {
    const visible = (element) => {
      const rect = element.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < innerHeight;
    };
    const activeHref = [...document.querySelectorAll('a[href*="/reel/"], a[href*="/reels/"]')]
      .filter(visible)
      .map((element) => {
        const rect = element.getBoundingClientRect();
        return {
          href: element.href,
          distance: Math.abs(rect.top + rect.height / 2 - innerHeight / 2),
        };
      })
      .sort((left, right) => left.distance - right.distance)[0]?.href ?? "";
    return { currentUrl: location.href, activeHref };
  });
  const active = normalizeReelUrl(browserData.activeHref);
  const current = normalizeReelUrl(browserData.currentUrl);
  return active ?? current;
}

async function waitForActiveReelChange(page, previousShortcode, timeoutMilliseconds) {
  const deadline = Date.now() + timeoutMilliseconds;
  let latest = null;
  while (Date.now() < deadline) {
    latest = await readActiveReelIdentity(page).catch(() => null);
    if (latest?.shortcode && latest.shortcode !== previousShortcode) {
      await page.waitForTimeout(REEL_TRANSITION_SETTLE_MILLISECONDS);
      return { changed: true, ...latest };
    }
    await page.waitForTimeout(REEL_TRANSITION_POLL_MILLISECONDS);
  }
  return { changed: false, ...(latest ?? {}) };
}

async function advanceToNextReel(page, previousShortcode, timeoutMilliseconds) {
  const previous = previousShortcode || (await readActiveReelIdentity(page).catch(() => null))?.shortcode || "";
  await page.keyboard.press("ArrowDown");
  let transition = await waitForActiveReelChange(page, previous, timeoutMilliseconds);
  if (transition.changed) return transition;
  await page.mouse.wheel(0, 900);
  transition = await waitForActiveReelChange(page, previous, Math.max(500, timeoutMilliseconds / 2));
  return transition;
}

function crawlerAccessError(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

function assertInstagramPageAccess(page, response = null, { allowLogin = false } = {}) {
  if (response?.status?.() === 429) {
    throw crawlerAccessError("rate_limited", "Instagram returned HTTP 429.");
  }
  const currentUrl = page.url();
  if (!allowLogin && /\/accounts\/login/i.test(currentUrl)) {
    throw crawlerAccessError("login_required", "Instagram login is required.");
  }
  if (!allowLogin && /\/(?:challenge|checkpoint)\//i.test(currentUrl)) {
    throw crawlerAccessError("challenge_required", "Instagram requested an account check.");
  }
}

async function navigateWithRetries(page, url, { attempts = 3, allowLogin = false } = {}) {
  let lastError = null;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const response = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30_000 });
      assertInstagramPageAccess(page, response, { allowLogin });
      return response;
    } catch (error) {
      if (["rate_limited", "login_required", "challenge_required"].includes(error?.code)) throw error;
      lastError = error;
      if (attempt < attempts) await page.waitForTimeout(attempt * 1_000);
    }
  }
  throw lastError ?? new Error(`Failed to open ${url}`);
}

function attachReelMetadataCollector(page, reelMetadata) {
  page.on("response", async (response) => {
    try {
      const responseUrl = new URL(response.url());
      const contentType = response.headers()["content-type"] ?? "";
      if (!responseUrl.hostname.endsWith("instagram.com") || !contentType.includes("json")) return;
      collectReelMetadata(parseInstagramJson(await response.text()), reelMetadata);
    } catch {
      // Some response bodies are unavailable after navigation; they are optional fallbacks.
    }
  });
}

async function createCollectionPage(context, url, reelMetadata, { allowLogin = false } = {}) {
  const page = await context.newPage();
  attachReelMetadataCollector(page, reelMetadata);
  try {
    await navigateWithRetries(page, url, { allowLogin });
    await page.waitForTimeout(500);
    return page;
  } catch (error) {
    await page.close().catch(() => {});
    throw error;
  }
}

async function recycleCollectionPage({ context, page, url, reelMetadata }) {
  await page?.close().catch(() => {});
  reelMetadata.clear();
  return createCollectionPage(context, url, reelMetadata);
}

async function createReelStore(csvPath, { flushRecordCount = REEL_STORE_FLUSH_RECORD_COUNT } = {}) {
  await prepareCsv(csvPath);
  let fields = [...CSV_FIELDS];
  let rows = [];
  if (fs.existsSync(csvPath) && (await fsp.stat(csvPath)).size > 0) {
    const existing = await fsp.readFile(csvPath, "utf8");
    fields = parseCsvHeader(existing);
    rows = csvObjects(existing);
  }
  const rowByUrl = new Map(rows.filter((row) => row.url).map((row) => [row.url, row]));
  const journalPath = `${csvPath}.pending.jsonl`;
  let dirty = false;
  let pendingRecordCount = 0;

  if (fs.existsSync(journalPath) && (await fsp.stat(journalPath)).size > 0) {
    const journalText = await fsp.readFile(journalPath, "utf8");
    const invalidLines = [];
    let recovered = 0;
    for (const line of journalText.split(/\r?\n/).filter(Boolean)) {
      try {
        const record = JSON.parse(line);
        const result = integrateCollectedRecord(rows, fields, record, { rowByUrl });
        if (!result.skipped) recovered += 1;
      } catch {
        invalidLines.push(line);
      }
    }
    if (recovered) {
      await writeCsvRecords(csvPath, rows, fields);
      console.log(`비정상 종료 전 임시 저장 릴스 복구 완료: ${recovered}개`);
    }
    if (invalidLines.length) {
      const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
      const corruptPath = `${journalPath}.corrupt_${stamp}`;
      await fsp.rename(journalPath, corruptPath);
      console.warn(`손상된 임시 저장 줄 ${invalidLines.length}개를 보관했습니다: ${corruptPath}`);
    } else {
      await fsp.rm(journalPath, { force: true });
    }
  }

  const flush = async ({ force = false } = {}) => {
    if (!dirty || (!force && pendingRecordCount < flushRecordCount)) {
      return false;
    }
    const savedRecordCount = pendingRecordCount;
    await writeCsvRecords(csvPath, rows, fields);
    await fsp.rm(journalPath, { force: true });
    dirty = false;
    pendingRecordCount = 0;
    console.log(
      force
        ? `남은 릴스 저장 완료: ${savedRecordCount}개`
        : `릴스 ${flushRecordCount}개 체크포인트 저장 완료: ${savedRecordCount}개`,
    );
    return true;
  };

  return {
    async append(record) {
      const cooldown = collectedRecordCooldown(rowByUrl.get(record.url), fields, record);
      if (cooldown) return cooldown;
      await fsp.appendFile(journalPath, `${JSON.stringify(record)}\n`, "utf8");
      const result = integrateCollectedRecord(rows, fields, record, {
        enforceCooldown: false,
        rowByUrl,
      });
      dirty = true;
      pendingRecordCount += 1;
      await flush();
      return result;
    },
    async flush() {
      return flush({ force: true });
    },
    stats() {
      return { rows: rows.length, pending: pendingRecordCount, journalPath };
    },
  };
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  let stopRequested = false;
  let interruptCount = 0;
  let statusReporter = null;
  let reelStore = null;
  let context = null;
  let followerRuntime = null;
  let prompt = null;
  let releaseCollectorLock = null;
  const requestGracefulStop = (source) => {
    if (stopRequested) return false;
    stopRequested = true;
    console.warn(
      `${source}: no new Reels will be collected. Queued follower lookups will finish, then CSV/XLSX will be saved.`,
    );
    return true;
  };
  const handleInterrupt = () => {
    interruptCount += 1;
    if (interruptCount === 1) {
      requestGracefulStop("Stop requested");
      console.warn("Press Ctrl+C again to force exit.");
      return;
    }
    console.warn("Force exit requested. Pending follower lookups may not be saved.");
    process.exit(130);
  };
  const backgroundStopInputListener = options.background && process.stdin.isTTY
    ? createBackgroundStopInputListener(() => requestGracefulStop("Input received"))
    : null;
  process.on("SIGINT", handleInterrupt);
  if (backgroundStopInputListener) {
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", backgroundStopInputListener);
    console.log("To stop Reels collection and finish queued followers, type any text then press Enter (for example: q).");
  }
  try {
    await fsp.mkdir(options.dataDir, { recursive: true });
    releaseCollectorLock = await acquireCollectorLock(options.dataDir);
    statusReporter = createCollectorStatusReporter(options.dataDir, options);
    const updateStatus = async (patch, reporterOptions = {}) => {
      try {
        await statusReporter.update(patch, reporterOptions);
      } catch (error) {
        console.warn(`상태 파일 저장 실패: ${error instanceof Error ? error.message : String(error)}`);
      }
    };
    await updateStatus({ state: options.followersOnly ? "followers" : "collecting" }, { force: true });

    const csvPath = path.join(options.dataDir, "reels_web.csv");
    const { chromium } = loadPlaywright();
    const refreshUrls = !options.followersOnly && options.urlsFile
      ? await loadReelUrls(options.urlsFile)
      : [];
    const startUrl = refreshUrls[0]
      ?? (options.hashtags[0] ? hashtagPageUrl(options.hashtags[0]) : options.startUrl);
    await fsp.mkdir(options.profileDir, { recursive: true });
    reelStore = options.followersOnly
      ? null
      : await createReelStore(csvPath, { flushRecordCount: options.checkpointItems });
    const seen = new Set();
    const deferredFollowerUsers = new Map();
    const executablePath = process.env.INSTAGRAM_BROWSER_EXECUTABLE;
    if (!executablePath || !fs.existsSync(executablePath)) {
      throw new Error("A supported Chrome or Edge executable was not found.");
    }

    context = await chromium.launchPersistentContext(options.profileDir, {
      executablePath,
      headless: options.background,
      viewport: options.background ? { width: 1440, height: 1000 } : null,
      args: options.background ? [] : ["--start-maximized"],
    });
    const restoredPages = context.pages();
    await Promise.all(restoredPages.map((restoredPage) => restoredPage.close().catch(() => {})));

    let followerEnricher = null;
    const ensureFollowerRuntime = async () => {
      if (followerRuntime) return createSequentialWebFollowerLookup(
        followerRuntime.page,
        options.followerIntervalSeconds,
      );
      followerRuntime = await createBackgroundFollowerRuntime({
        chromium,
        sourceContext: context,
        executablePath,
      });
      const lookupImpl = createSequentialWebFollowerLookup(
        followerRuntime.page,
        options.followerIntervalSeconds,
      );
      followerEnricher?.setLookupImpl(lookupImpl);
      return lookupImpl;
    };
    const startFollowerEnricher = async ({ deferRuntime = false } = {}) => {
      const lookupImpl = deferRuntime
        ? async () => { throw new Error("Follower lookup runtime has not started yet."); }
        : await ensureFollowerRuntime();
      followerEnricher = new FollowerEnricher({
        dataDir: options.dataDir,
        concurrency: 1,
        lookupImpl,
        source: "instagram_web",
        onProgress: ({ completed, queued, username, status, followerCount, error }) => {
          const outcome = status === "success"
            ? Number(followerCount).toLocaleString("en-US")
            : `${status}${error ? ` (${error})` : ""}`;
          console.log(`[Follower ${completed}/${queued}] @${username} -> ${outcome}`);
          void statusReporter.update({
            follower_completed: completed,
            follower_queued: queued,
            follower_last_status: status,
            follower_last_username: username,
          }).catch(() => {});
        },
      });
      return followerEnricher;
    };

    if (options.followersOnly) {
      await startFollowerEnricher();
      await context.close();
      context = null;
      const queued = await followerEnricher.enqueueAll();
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
      followerRuntime = null;
      await statusReporter.finish(stats.stopStatus ? "completed_with_errors" : "completed", {
        follower_success: stats.success,
        follower_failed: stats.failed,
      });
      return;
    }

    const reelMetadata = new Map();
    let page = await createCollectionPage(context, startUrl, reelMetadata, {
      allowLogin: !options.background,
    });
    if (options.background) {
      const expectedSurface = options.hashtags.length
        ? isInstagramHashtagSurface(page.url())
        : isInstagramReelsSurface(page.url());
      if (!expectedSurface) {
        throw crawlerAccessError(
          "login_required",
          "Background mode needs a saved Instagram login. Run without --background, sign in once, and retry.",
        );
      }
      console.log("Background mode started with the saved Instagram browser profile.");
    } else {
      prompt = readline.createInterface({ input: process.stdin, output: process.stdout });
      console.log("브라우저에서 Instagram에 로그인하고 릴스 화면을 연 뒤 이 창으로 돌아오세요.");
      await prompt.question("준비가 끝났으면 Enter를 누르세요: ");
      assertInstagramPageAccess(page);
      const expectedSurface = options.hashtags.length
        ? isInstagramHashtagSurface(page.url())
        : isInstagramReelsSurface(page.url());
      if (!expectedSurface) await navigateWithRetries(page, startUrl);
    }
    await startFollowerEnricher({ deferRuntime: options.followersAfterReels });

    let captured = 0;
    let duplicateCount = 0;
    let missingCount = 0;
    let filteredCount = 0;
    let cooldownSkippedCount = 0;
    let pageRecycleCount = 0;
    let transitionStallCount = 0;
    let recoveryFailureCount = 0;
    let nextDelaySeconds = options.intervalSeconds;

    const progressPatch = (lastReelUrl = "") => ({
      captured,
      duplicates: duplicateCount,
      missing: missingCount,
      filtered: filteredCount,
      cooldown_skipped: cooldownSkippedCount,
      page_recycles: pageRecycleCount,
      transition_stalls: transitionStallCount,
      recovery_failures: recoveryFailureCount,
      ...(lastReelUrl ? { last_reel_url: lastReelUrl } : {}),
    });

    const captureCurrentReel = async () => {
      nextDelaySeconds = options.intervalSeconds;
      await expandVisibleCaption(page);
      const record = await extractVisibleReel(page);
      if (!record) return null;
      if (seen.has(record.shortcode)) {
        nextDelaySeconds = REEL_SUCCESS_INTERVAL_SECONDS;
        return { ...record, duplicateInRun: true };
      }
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
      reelMetadata.delete(record.shortcode);
      nextDelaySeconds = hasCompleteReelCoreData(collectedRecord)
        ? REEL_SUCCESS_INTERVAL_SECONDS
        : options.intervalSeconds;
      if (!hasAnyHashtag(collectedRecord.hashtags, options.hashtags)) {
        seen.add(record.shortcode);
        return { ...record, filteredOut: true };
      }
      if (!isWithinUploadAgeDays(collectedRecord, options.maxUploadAgeDays)) {
        seen.add(record.shortcode);
        return {
          ...record,
          uploadAgeFilteredOut: true,
          uploadAgeDays: collectedRecord.days_since_upload,
        };
      }
      const followerPayload = {
        userId: collectedRecord.user_id,
        username: collectedRecord.username,
        seenAt: collectedRecord.collected_at,
      };
      const userState = await followerEnricher.trackUser({
        ...followerPayload,
        enqueue: !options.followersAfterReels,
      });
      if (options.followersAfterReels && followerPayload.username) {
        const key = followerPayload.userId || followerPayload.username.toLowerCase();
        deferredFollowerUsers.set(key, followerPayload);
      }
      if (userState) {
        collectedRecord.follower_count = userState.follower_count ?? "";
        collectedRecord.follower_count_collected_at = userState.follower_count_collected_at ?? "";
        collectedRecord.follower_lookup_status = userState.lookup_status ?? "";
      }
      const stored = await reelStore.append(collectedRecord);
      seen.add(record.shortcode);
      return {
        ...record,
        snapshotLabel: stored.label,
        cooldownSkipped: Boolean(stored.skipped),
        collectionComplete: hasCompleteReelCoreData(collectedRecord),
      };
    };

    const hashtagUrls = options.hashtags.length
      ? await collectHashtagReelUrls(page, options.hashtags, options.maxItems)
      : [];
    const directUrls = refreshUrls.length ? refreshUrls : hashtagUrls;
    if (refreshUrls.length || options.hashtags.length) {
      for (let index = 0; index < directUrls.length; index += 1) {
        if (options.hashtags.length && captured >= options.maxItems) break;
        const url = directUrls[index];
        try {
          if (index > 0 || !isInstagramReelsSurface(page.url())) {
            await navigateWithRetries(page, url);
          }
          if (options.manual) {
            await prompt.question("현재 릴스를 다시 수집하려면 Enter를 누르세요: ");
          } else {
            await page.waitForTimeout(nextDelaySeconds * 1000);
          }
          const record = await captureCurrentReel();
          if (record?.duplicateInRun) {
            duplicateCount += 1;
            console.log(`[${index + 1}/${directUrls.length}] 실행 중 중복 건너뜀: ${record.url}`);
          } else if (record?.filteredOut) {
            filteredCount += 1;
            console.log(`[${index + 1}/${directUrls.length}] Hashtag mismatch skipped: ${url}`);
          } else if (record?.uploadAgeFilteredOut) {
            filteredCount += 1;
            const age = record.uploadAgeDays === "" ? "unknown" : `${record.uploadAgeDays}day`;
            console.log(
              `[${index + 1}/${directUrls.length}] Upload age skipped (${age}, max ${options.maxUploadAgeDays}day): ${url}`,
            );
          } else if (record?.cooldownSkipped) {
            cooldownSkippedCount += 1;
            console.log(`[${index + 1}/${directUrls.length}] 6시간 이내 중복 건너뜀: ${record.url}`);
          } else if (record) {
            captured += 1;
            console.log(`[${index + 1}/${directUrls.length}] 수집 (${record.snapshotLabel}): ${record.url}`);
          } else {
            missingCount += 1;
            console.warn(`[${index + 1}/${directUrls.length}] 수집 실패: ${url}`);
          }
          await updateStatus(progressPatch(record?.url));
        } catch (error) {
          if (["rate_limited", "login_required", "challenge_required"].includes(error?.code)) throw error;
          missingCount += 1;
          console.warn(
            `[${index + 1}/${directUrls.length}] 수집 실패: ${url} (${error instanceof Error ? error.message : String(error)})`,
          );
          await updateStatus({ ...progressPatch(url), last_error: String(error?.message ?? error) });
        }
      }
      if (options.hashtags.length) {
        console.log(`Hashtag OR collection finished: matched=${captured}, requested=${options.maxItems}`);
      }
    } else {
      let itemsSinceRecycle = 0;
      let viewsSinceRecycle = 0;
      let consecutiveUnproductive = 0;
      let consecutiveRecoveryFailures = 0;
      let lastReelsUrl = page.url();
      const transitionTimeoutMilliseconds = options.transitionTimeoutSeconds * 1_000;

      const recyclePage = async (reason) => {
        console.warn(`수집 탭 재생성 (${reason}): 진행=${captured}, 중복=${duplicateCount}, 누락=${missingCount}`);
        page = await recycleCollectionPage({
          context,
          page,
          url: options.startUrl,
          reelMetadata,
        });
        pageRecycleCount += 1;
        itemsSinceRecycle = 0;
        viewsSinceRecycle = 0;
        consecutiveUnproductive = 0;
        lastReelsUrl = page.url();
        await updateStatus({ ...progressPatch(), state: "collecting", recycle_reason: reason }, { force: true });
      };

      while (!stopRequested && (options.maxItems === 0 || captured < options.maxItems)) {
        let record = null;
        try {
          if (options.manual) {
            await prompt.question("현재 릴스를 저장하려면 Enter를 누르세요: ");
          } else {
            await page.waitForTimeout(nextDelaySeconds * 1000);
          }

          if (!isInstagramReelsSurface(page.url())) {
            assertInstagramPageAccess(page);
            console.warn("릴스 화면을 벗어나 마지막 릴스로 복귀합니다.");
            await navigateWithRetries(page, lastReelsUrl || options.startUrl);
            await page.waitForTimeout(500);
          }
          record = await captureCurrentReel();
          viewsSinceRecycle += 1;
          if (record?.uploadAgeFilteredOut) {
            filteredCount += 1;
            consecutiveUnproductive = 0;
            const age = record.uploadAgeDays === "" ? "unknown" : `${record.uploadAgeDays}day`;
            console.log(`Upload age skipped (${age}, max ${options.maxUploadAgeDays}day): ${record.url}`);
          } else if (record?.cooldownSkipped) {
            cooldownSkippedCount += 1;
            consecutiveUnproductive = 0;
            console.log(`6시간 이내 저장 중복 건너뜀: ${record.url}`);
          } else if (record?.duplicateInRun) {
            duplicateCount += 1;
            consecutiveUnproductive = 0;
            console.log(`실행 중 재노출 릴스 건너뜀 (누적 ${duplicateCount}): ${record.url}`);
          } else if (record) {
            lastReelsUrl = record.url;
            captured += 1;
            itemsSinceRecycle += 1;
            consecutiveUnproductive = 0;
            console.log(`[${captured}/${options.maxItems === 0 ? "unlimited" : options.maxItems}] ${record.url}`);
          } else {
            missingCount += 1;
            consecutiveUnproductive += 1;
            console.warn(`현재 화면에서 릴스 URL 추출 실패 (${consecutiveUnproductive}/${REEL_UNPRODUCTIVE_RECYCLE_THRESHOLD})`);
          }
          if (record?.url) lastReelsUrl = record.url;
          consecutiveRecoveryFailures = 0;
          await updateStatus({ ...progressPatch(record?.url), last_error: "" });

          const reachedTarget = options.maxItems !== 0 && captured >= options.maxItems;
          if (stopRequested || reachedTarget) break;
          if (
            options.pageRecycleItems > 0
            && itemsSinceRecycle >= options.pageRecycleItems
          ) {
            await recyclePage(`${itemsSinceRecycle} items`);
            continue;
          }
          if (
            options.pageRecycleItems > 0
            && viewsSinceRecycle >= options.pageRecycleItems * 4
          ) {
            await recyclePage(`${viewsSinceRecycle} viewed Reels`);
            continue;
          }
          if (consecutiveUnproductive >= REEL_UNPRODUCTIVE_RECYCLE_THRESHOLD) {
            await recyclePage(`${consecutiveUnproductive} unproductive views`);
            continue;
          }
          if (!options.manual) {
            const transition = await advanceToNextReel(
              page,
              record?.shortcode ?? "",
              transitionTimeoutMilliseconds,
            );
            if (!transition.changed) {
              transitionStallCount += 1;
              consecutiveUnproductive += 1;
              console.warn(`다음 릴스 전환 지연 (${consecutiveUnproductive}/${REEL_UNPRODUCTIVE_RECYCLE_THRESHOLD})`);
              if (consecutiveUnproductive >= REEL_UNPRODUCTIVE_RECYCLE_THRESHOLD) {
                await recyclePage(`${consecutiveUnproductive} transition stalls`);
              }
            }
          }
        } catch (error) {
          if (["rate_limited", "login_required", "challenge_required"].includes(error?.code)) throw error;
          recoveryFailureCount += 1;
          consecutiveRecoveryFailures += 1;
          const message = error instanceof Error ? error.message : String(error);
          console.warn(
            `일시적 수집 오류 자동 복구 (${consecutiveRecoveryFailures}/${REEL_MAX_CONSECUTIVE_RECOVERY_FAILURES}): ${message}`,
          );
          await reelStore.flush();
          await updateStatus({
            ...progressPatch(record?.url),
            state: "recovering",
            last_error: message.slice(0, 500),
          }, { force: true });
          if (consecutiveRecoveryFailures >= REEL_MAX_CONSECUTIVE_RECOVERY_FAILURES) {
            throw new Error(`Repeated collection recovery failure: ${message}`);
          }
          await new Promise((resolve) => setTimeout(
            resolve,
            Math.min(5_000, 500 * (2 ** (consecutiveRecoveryFailures - 1))),
          ));
          await recyclePage(`transient error ${consecutiveRecoveryFailures}`);
        }
      }
    }

    await reelStore.flush();
    prompt?.close();
    prompt = null;
    if (options.followersAfterReels && deferredFollowerUsers.size) {
      console.log(`릴스 수집 완료. 팔로워 조회 ${deferredFollowerUsers.size}개를 순차 처리합니다.`);
      await updateStatus({ ...progressPatch(), state: "followers" }, { force: true });
      await ensureFollowerRuntime();
      for (const user of deferredFollowerUsers.values()) {
        await followerEnricher.trackUser({ ...user, enqueue: true });
      }
    } else {
      console.log("릴스 수집을 멈췄습니다. 남은 팔로워 조회를 마친 뒤 브라우저를 닫습니다.");
    }
    await context.close();
    context = null;
    const followerStats = await followerEnricher.drain();
    console.log(
      `Follower web: success=${followerStats.success} unavailable=${followerStats.unavailable} failed=${followerStats.failed}`,
    );
    if (followerStats.stopStatus) {
      console.error(
        `Follower web lookup stopped (${followerStats.stopStatus}): ${followerStats.stopError}`,
      );
      process.exitCode = 2;
    }
    const merged = await mergeFollowerDataIntoReels(
      csvPath,
      path.join(options.dataDir, "users.csv"),
    );
    console.log(`Follower data merged into reels_web.csv: ${merged}`);
    await followerRuntime?.browser?.close();
    followerRuntime = null;
    console.log(`저장 완료: ${csvPath}`);
    await statusReporter.finish(
      followerStats.stopStatus
        ? "completed_with_errors"
        : stopRequested ? "stopped" : "completed",
      {
        ...progressPatch(),
        follower_success: followerStats.success,
        follower_failed: followerStats.failed,
        follower_unavailable: followerStats.unavailable,
      },
    );
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    await reelStore?.flush().catch(() => {});
    if (statusReporter) {
      await statusReporter.finish("failed", {
        last_error: message.slice(0, 500),
        failure_code: error?.code ?? "collector_error",
      }).catch(() => {});
    }
    throw error;
  } finally {
    prompt?.close();
    await context?.close().catch(() => {});
    await followerRuntime?.browser?.close().catch(() => {});
    await releaseCollectorLock?.().catch(() => {});
    process.off("SIGINT", handleInterrupt);
    if (backgroundStopInputListener) process.stdin.off("data", backgroundStopInputListener);
  }
}

export {
  CSV_FIELDS,
  acquireCollectorLock,
  advanceToNextReel,
  createBackgroundStopInputListener,
  createReelStore,
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
  isWithinUploadAgeDays,
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
  waitForActiveReelChange,
};

if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url))) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  });
}
