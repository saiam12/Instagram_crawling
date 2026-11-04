---
version: alpha
name: Collector Monitor
description: A personal Korean operations desk for a local web and Android collector.
colors:
  background: "#edf2f7"
  surface: "#ffffff"
  ink: "#173047"
  muted: "#617386"
  line: "#dbe4ed"
  primary: "#117d88"
  android: "#526fbd"
  success: "#157452"
  warning: "#8c5a13"
  danger: "#ad3e45"
typography:
  sans:
    fontFamily: '"Segoe UI Variable Text", "Malgun Gothic", "Apple SD Gothic Neo", sans-serif'
  display:
    fontFamily: '"Segoe UI Variable Display", "Malgun Gothic", sans-serif'
  mono:
    fontFamily: 'Consolas, "Cascadia Code", monospace'
rounded:
  DEFAULT: "14px"
  control: "8px"
spacing:
  section: "24px"
  sidebar: "218px"
components:
  button:
    minHeight: "40px"
  table:
    maxHeight: "575px"
  dialog:
    maxWidth: "680px"
---

# Collector Monitor

Classification filters use saved `korean` values: true means 한국, false means 외국,
and missing/unknown means 미확인. Country disclosure never overrides this filter.
Undisclosed `account_country` values (공유되지 않음 / Not shared) are blank.

## Overview

A private operations screen for the owner of this Windows Instagram collector.
Korean labels and Asia/Seoul times; desktop-first with a narrow mobile layout.
The visual reference is a two-channel instrument panel: web discovery and Android
enrichment each have their own status and activity strip. Status must be explainable
from files and process observations; never invent progress or treat a live PID as success.
There is no sibling web UI in this repository. This folder owns the new UI context.

## Colors

The YAML role names map directly to `:root` variables in `styles.css`, using
`--role-name`. The light slate workspace contains white data surfaces and a navy
navigation rail. Teal identifies web/primary actions; blue identifies Android.
Green, amber and red always include text labels. Supporting tints and hover shades
are derived in the same stylesheet; no runtime theme adapter or external theme exists.

## Typography

Locally installed Segoe UI for controls/display, Malgun Gothic for Korean, Consolas
for metrics and commands. No remote fonts. Tabular numerals keep changing counts stable.

## Layout

One naturally scrolling document, with in-page navigation. Status panels are paired
at desktop widths. Only the data table and event list own bounded internal scrolling.
At 820px the rail becomes a top navigation; at 540px status panels stack.
Command guides remain in normal document flow and never inherit the table's height.
The runtime panel distinguishes configured duration, elapsed wall time since start
(including waiting/retries), and time until scheduled end. Activity charts use fixed independent Y-axes: web 0/20/40 and Android 0/35/70,
with explicit counts per clock-aligned five-minute interval and a zero baseline.
Both charts share fixed time boundaries (00, 05, 10 minutes, etc.) and show 12
intervals including the current partial interval. KST ticks and tooltips identify
actual intervals; only the current interval accumulates until the next boundary. Bars are capped
at the axis maximum; their titles retain the actual counts. Zero bins have zero height; numerical ticks remain visible on narrow screens.

## Elevation & Depth

Thin separators and white surfaces define grouping. Shadows are reserved for dialog
and transient feedback. No decorative backgrounds or fake live motion.

## Shapes

14px panels, 8px controls, compact badges. All scroll regions use the global scrollbar
tokens; forced-colors restores OS colors. Reduced motion removes smooth scrolling.

## Components

Native select, input and table semantics; app-owned native dialog for details and
action confirmation. `app.js` owns shared API requests, validation, toast, badges and
formatters. The source of interaction rules is `UX-CONTRACT.md`.

## Do's and Don'ts

- Distinguish missing, zero, private, waiting and confirmed stopped states.
- Keep old data visible during connection/file errors, with an explicit warning.
- Show log occurrence times separately from monitor detection times.
- Keep options structured; never accept arbitrary commands or stop unrelated processes.
- Do not animate a heartbeat that is not backed by a successful server response.

## Theme

The header dark-mode toggle owns a document-wide data-theme. The external
blocking theme.js restores collector-monitor-theme from localStorage before CSS
paints; without a saved choice it follows the initial OS color preference.
The toggle has a stable Korean label and aria-pressed state. Dark mode retains
the existing type, spacing, teal web and blue Android identity while changing
surface, text, semantic tones, controls, table, dialogs and scrollbar colors.

회차별 수집 릴스 패널은 선택 데이터셋의 저장 기록을 collection_number별로 집계하고 3초마다 갱신합니다. 새 회차는 자동 추가하며 회차 없는 기록은 미지정으로 표시합니다. Android 보강 완료 건수와는 별개입니다.

## Compact list density

Use compact shared spacing for the operations desk: 48px data rows, bounded
620px table scrolling, fixed proportional columns with a 540px minimum. Narrow
screens group the search row above three filter selects. Keep all metrics visible
when width permits, preserve horizontal scrolling below the minimum, and open
full record content through the existing detail dialog. Event summaries collapse
raw log messages by default; native details preserve expanded event IDs across
polls. Forms remain in document flow. Light/dark themes share density rules.

The main area uses all available width without a maximum-width cap. At widths of 1280px or more and aspect ratios of at least 3:2, collection rounds/runtime form the left column and the paired web/Android status panels form the right column. Taller large windows put rounds and runtime side by side above the status panels. Smaller windows retain the stacked layout, with status panels stacking below 540px. Table and forms retain their own scrolling and natural height contracts.

On wide landscape screens, the four summary tiles occupy the left column above rounds/runtime. The paired status charts span those two left-side rows and share the summary tiles top edge. Summary and round-panel outer widths are identical.

Live data and event history share the first resizable row (65/35 initially). Command guides and monitor-owned jobs share the next row (50/50 initially). Each native separator supports pointer drag, arrow keys, Home/End and double-click reset. Ratios clamp to 30–75% left and persist per row in localStorage. Narrow windows stack panels and hide separators; only table/event content scrolls internally.

In side-by-side rows, left panels alone determine row height. Right panels stretch to the same height with contained overflow; event history uses its existing internal list scroller. Stacked layouts retain natural independent heights.

Android interruption: a cached, read-only ADB probe supplies an alert only after
the same device is unavailable, its input dispatcher is blocked, or the current
focused window is an app ANR/error dialog for two consecutive probes. Worker
exit/staleness, unreadable UI XML, retries and ADB probe timeout alone never blink.
Web-only, paused and explicit collection-stop modes suppress the alert. The panel
uses a persistent message and slow two-second full-panel red pulse; reduced motion
keeps a static red warning. A successful device probe clears the interruption alert.

Summary tiles run left to right: stored collection records, records in the last
hour, recollection backlog, Android pending. The third tile replaces unique Reel count.
It uses the scheduler's most recent due_recollections value: due but not yet saved,
including any active unsaved items; future jobs are excluded. Its tooltip names
the source update time. Missing scheduling data stays unknown (—), distinct from zero.
The four-tile layout, typography and theme tokens stay shared with the other tiles.

## File visualization

`/visualize.html` is a separate navigation tab in this operations desk. It shares
styles.css tokens, sidebar, typography and theme.js with the live monitor.
A compact file panel precedes four summary tiles and a responsive two-column chart
grid: collection rounds, collection timeline, followers versus views and
followers versus likes (all valid selected records). The metric selector applies
to summaries and preview; round and latest-per-Reel/all-records scope apply to
relationship charts and summaries; round/timeline charts count the selected
collection records before latest-per-Reel reduction. Captions make this distinction explicit.
Native file input accepts CSV, JSON and XLSX, one file up to 20 MiB. The local server
reads it in request memory only; the server never saves uploads or sends them outside localhost.
XLSX sheets remain separate options, and wide snapshot columns become long records.
Unknown/private/unavailable counts stay missing, separate from observed zero.
UTF-8 and CP949 CSV are supported. Only canonical collector columns and common
Korean aliases are recognized; no arbitrary column-mapping workflow is required.
SVG charts share primary/Android colors, include text values and accessible captions.
Loading can be cancelled; replacement errors keep the previous result with clear copy.
No collector action or original-file write is available on this page. IndexedDB
keeps one versioned last workspace: normalized datasets, filename, warnings and
sheet/round/scope/metric/time-bucket selections. Reopening the page restores it
with an explicit cached-data message and saved time; a new file read takes priority
over a late restore. Save failure retains current charts and reports the failure.
Close file hides current data while keeping the saved workspace; Delete saved work
removes the browser cache and closes the current workspace. No raw file or absolute
source path is stored. This is a snapshot, not a watcher of the original file.

The sidebar has two consistent groups on every page: 수집 관리 contains overview,
live data, event history and command guidance; 데이터 활용 contains collection visualization,
analysis visualization and video planning. Shared nav rows use a fixed icon column, centered icons,
one text start, consistent padding and minimum height. Narrow horizontal navigation
uses the same alignment rules, including the first item.

## Reference-grounded video planning

Video planning follows file visualization in the shared navigation on every page.
The existing teal/slate token palette, Korean system typography and compact panels
continue. The distinguishing element is an editorial storyboard: a left-aligned
numbered scene list with reference IDs connecting the plan to real retrieved Reels.
On wide screens the brief/reference column and plan form a 0.8:1.5 split; under
900px they stack. No main-content width cap or new font/network asset is introduced.
The user searches locally, inspects reference payloads, and explicitly enables
Gemini transmission before generating. Empty/loading/error/completed states occupy
the same panel. No fabricated plan, model output or virality score appears as real data.
The form uses native labels/select, owned inline validation and aria-live feedback.
Product, target and purpose are required. Selling point and production constraints
are optional native text inputs with explicit Korean 선택 labels, each limited to
200 characters. Blank optional fields do not block search or generation. Historical
mood remains in old result summaries only, not in the new form or requests.
The sole recommendation source is collectors/data/collector/reel_analysis.json.
Only valid analyzed records are retrieved; collection rows and other Analyzer outputs
are not merged. The file path remains visible, and a missing/invalid file is an error.
Cached searches from a different source require a new search before generation.

Recommendation cache uses the same shared IndexedDB owner as visualization, with
its own versioned last-workspace database. A quiet cache status/action row sits
under the page header. Stored references/results are explicit snapshots; restored
searches must refresh before generation, and transmission consent is never restored.
Deleting a workspace uses the shared native-modal visual language and cancel-first
focus. The plan highlights recommended integer seconds and shows each scene's
contiguous start/end times. Original plan references stay attached even after a new
search, with the plan's own input conditions printed above the storyboard.

## Analyzer visualization

`/analysis.html` shares theme and cache primitives with file visualization. A compact
source strip, three filters, four summary cards and six chart panels lead to a
paginated 20-row evidence table. Use two chart columns on desktop, three above
1600px in landscape and one below 650px. Native bar buttons drill into a category;
all bars show counts and percent of the current selected Reel population. Audience
and appeal categories may overlap, but each category counts one Reel once.
Keep free-form source categories separate; never split 20s_30s into guessed ages.
Scene-end duration is approximate. AI Hook strength and target recommendations
must not look like measured engagement, predicted performance or viewer demographics.
Detail dialog retains original summary, Hook, audience/appeal evidence and scenes.

## 한국·외국 지표 선택

Use the existing native select and compact controls for `릴스 구분`: 전체, 한국,
외국, 미확인 (-). `korean-filter.js` and classification.py own the three-state mapping.
Only confirmed foreign values belong to 외국; unknown values display - and can be
selected separately. Saved korean=false remains foreign regardless of country disclosure. Do not infer classification from
generated Korean analysis. Main-page selectors stay synchronized and persist
`korean` in the URL. Saved-record summaries, rounds, activity bars and table apply
the selection; process health, runtime, queues and last-success times remain global
and are explicitly labeled. Android bins use the Reel URL's latest classification.
File/analysis visualization preserve category in their existing workspaces without
discarding older cached data. Record details reuse the native select and save button
with inline pending/success/error feedback. The saved choice applies to every round
of the same Reel and persists in the dataset override sidecar. Missing-classification cache notes direct the user to
reselect the source file/reload analysis. File metric sampling chooses latest per
Reel within a round before category filtering; record charts filter individual
snapshots. Preserve current colors, density and responsive wrapping.

## Command guidance

The live monitor pairs read-only PowerShell examples and collapsible option
descriptions with an integrated execution form and job list. Command boxes reuse the existing
monospace type, border and theme tokens; copy buttons use the shared toast.
Guidance stays in normal document flow, wraps long options, and remains usable
without API connectivity. Previous monitor-owned job logs and stop controls stay
in the right panel; terminal launches are observed in the overview.

## Integrated execution panel

Command guidance remains on the left. The right execution panel combines the
existing launch form, device preparation, command preview and monitor-owned job
logs. Both management panels keep natural height; the form never inherits a
height cap or internal scroll from its read-only sibling. Existing split controls
and narrow stacking remain unchanged. Form fields use two columns, one below 540px.

Scheduled discovery has no per-invocation saved-item target or per-window saved-item cap. The execution form, command examples and help omit both removed count options. Discovery duration and candidate-search controls remain independent.

## Collector and Refresh views

The existing operations desk is reused through `?source=collector` and `?source=refresh`. A native link navigation within the dataset toolbar names the two programs; one sidebar entry opens 수집 현황 on every page; the toolbar alone switches 신규 수집 / 재수집. The selected link uses aria-current=page. Each view keeps dataset, filters, pages, metrics and events scoped through the same API and rendering functions. Refresh adds one compact input/status panel using existing round-count cards and theme tokens. Its command guide is read-only; scheduled-collector launch controls are hidden there. Unknown legacy inbox counts remain an em dash, separate from zero.

## Spacing and copy cleanup

Use 16px panel insets and section spacing, 12px related-panel gaps and a 41px toolbar baseline. Source links sit on the same row as dataset/filter controls and wrap before those controls on narrow screens. The overview owns its two-column grid; document siblings never rely on fixed row numbers. Sidebar links share 18px SVG icons in one fixed icon column and consistent row height. Static English eyebrows, repeated introductory text and idle status instructions are omitted. Retain actionable errors, privacy/zero distinctions, stale-file notices and Gemini consent. Runtime details live under a collapsed schedule disclosure; panels with no runtime or inbox data are hidden instead of filling space with dashes.

Legacy Refresh datasets without an inbox use a single compact source/guide bar. Actual inbox statistics and read errors retain the full panel. The wide overview groups summaries and rounds in one left column, preventing status cards on the right from inserting gaps between left panels.


Refresh keeps runtime and its compact two-column interval table alongside round
counts, using the existing runtime panel and typography. Runtime distinguishes a
whole supervisor session from a legacy batch; defaults are identified when the
original settings were not recorded. The interval table stays visible, while
exact start/end timestamps remain in the native schedule disclosure.


Refresh guidance occupies a separate full-width row under the overview: the compact
interval table takes the left column and the command/options guide the wider right.
The former input-status bar is removed. At narrow widths the guide follows the table;
advanced interval/count options use the existing native disclosure.

Numeric relationship charts use the shared scatter.js renderer: duration (seconds) or follower count on X, observed views/likes on Y. Include every valid selected record, real zero values, excluded missing counts, and an expandable complete value list. Analysis joins the latest collected metrics by Reel ID; scene duration remains an estimate.

All metric summaries (views, likes, comments, shares, reposts, saves, followers) appear together in one comparison table, with all metrics in the record preview. Six follower-versus-engagement charts are shown simultaneously without a metric selector. Shared scatter axes use log10(1 + value) to retain observed zeros, with original-value tick labels and details.

Metric comparison owns its six-column geometry (12% name, 17% per numeric column, 20% final counts), independent of record title/date column widths. The summary panel is capped at 760px to reduce horizontal distance between columns; all table headings and values are centered. Retain the shared 48px row height and 12px vertical padding. Show mean, P30, P70, coverage and present/missing counts; percentiles use inclusive linear interpolation of sorted observed values, preserve zero and exclude missing values.

The collected-data relationship view now owns a single full-width chart with independent native X/Y metric selectors. Start both axes unselected on each page visit (do not restore axes from the workspace cache). Render only when both axes are selected; reset clears both axes and returns focus to X. Keep all seven summaries visible above it. Round, category and record scope filters still apply.

Collection visualization uses local Metrics and Collection trends tabs with shared filters. Metrics is the default and contains the count, percentile summary, axis comparison and record preview; trends contains only round/time collection charts. Native tab semantics support arrow/Home/End navigation. Axis selectors are compact inline controls, reset never owns a full row. Scatter geometry follows the available card width; the ten-column preview owns a colgroup and internal horizontal scrolling, with unbroken numeric cells.

Log scatter axes use natural powers of ten as major ticks and 2–9 intermediate ticks per decade, rather than four equal screen-space subdivisions with arbitrary inverse-log values. Labels prioritize major ticks and adapt to available pixel spacing; intermediate tick marks stay visible without crowding labels. Underlying log10(1+value) coordinates and zero handling are unchanged.

The chart toolbar includes a Round criterion synchronized with the shared round filter and workspace state. Scatter chrome shows only the plotted point count at the upper right; remove surrounding explanatory copy and expandable values. Preserve axis labels, numeric tick labels and per-point SVG titles. The main chart title stays available to assistive technology but is visually hidden; before both axes are chosen the chart remains blank with a zero point count.

View-count scatter axes start at 10 (inclusive); records below 10 are omitted only from the chart when view_count is selected on either axis. Other axes retain their zero-inclusive scale. Point badges count only plotted records. Graph-local All/Korean/Foreign/Unknown tabs synchronize with the shared category selector and workspace state; unknown remains a separate classification.

Replace the statistical summary table entirely with six fixed relationship plots: X=follower_count, Y=views/likes/comments/shares/reposts/saves. Use a three-column desktop grid (three plots per row), two columns at 1100px and below, and one column at 650px and below. Keep the independent X/Y-selectable chart intact below the fixed grid. All seven plots share the selected round/category/scope, log ticks, view-count minimum of 10 and upper-right point badge. No means, percentiles or coverage table remains in collection visualization.

Scatter plot data regions use a height of 0.7 times their width; remove wide-screen SVG height caps so browser aspect-ratio preservation does not letterbox plots. Selecting a point highlights the same Reel ID across every scatter plot, including different rounds and URL /reel/ versus /reels/ variants, using accent fill, larger radius and an outline. Keep selected points above ordinary points and preserve selection on redraw. Click again or Escape clears selection; Enter/Space supports keyboard activation. Missing/filtered records do not create placeholder points.

Above the six fixed plots, share one selection row: a compact 200px selected-record count card on the left and sheet/round/category/scope controls on the right (four columns on desktop). The filters still drive the same selected records across all plots. At 1000px use two filter columns and at 650px stack the count over the filters. Filters remain available in trends, where the count card is hidden.

The category filter has a separate Korean highlight checkbox: it never changes the selected records or point counts. When enabled, only records whose saved normalized korean value is true receive the primary/teal fill in all six fixed plots and the configurable plot; false and unknown keep ordinary colors. Explicitly selected Reel points retain the warning/gold highlight with priority. Persist the checkbox with the workspace independently of the category filter.

The user search field highlights matching usernames without filtering records, counts, trends or previews. Match case-insensitive substrings ignoring a leading @. Draw matching user points above ordinary and Korean-highlighted points, using the danger/pink accent and a larger radius; clicked Reel selection retains warning/gold priority. Debounce by 300ms with IME support; Enter applies immediately and clear removes the user highlight. Persist the query with the workspace.

On desktop, the compact 170px count card and five filter fields (sheet, round, category/highlight, scope, publisher search) share a single horizontal row. Search occupies one grid column alongside the selectors, with equal control height. Keep the existing two-column filter fallback at 1000px and stacked count at 650px.

Analysis duration charts use a linear X axis in seconds with evenly spaced nice-step ticks (1/2/5 times powers of ten). Their view/like Y axes remain logarithmic. Count-based collection-chart axes remain logarithmic.

Below the six follower charts, split the comparison row 2:1: configurable chart on the left, collected video_duration_seconds versus view_count on the right. Duration uses a linear seconds X axis and log views Y axis, sharing all filters and highlights. Collapse the split below 1000px. Upload normalization preserves the numeric duration field (and base duration across wide snapshots); old cached uploads without that field prompt the user to reselect the source.


Collection comparison board: keep six follower-X charts and duration-versus-views, and place six view-X charts before the custom-axis builder. Use one continuous three-column grid (two at 1100px, one at 650px). The builder occupies one cell; selecting both axes appends a persistent chart immediately before the builder and clears the selectors for another chart. Each chart exposes minus to remove, a drag handle, previous/next buttons and Alt+arrow reordering. Removal compacts the grid across rows. No plus or zoom action. Chart order, deleted defaults and custom comparisons share the existing workspace cache; old workspaces receive the default board. Shared filters and Reel selection still apply to every chart.

The collection chart builder is always the last grid cell and offers only X/Y selectors. It is excluded from saved chart ordering and cannot be displaced by graph moves. New charts append immediately before it. At viewport widths of 1600px and above the comparison board uses four columns; retain three/two/one columns at smaller breakpoints. Old cached builder entries are discarded during restoration.

The collection comparison grid offers a compact two/three/four-column selector and direct shared sizing, without explanatory copy or visible resize text. Selecting a column preset fits graph widths to the screen; dragging width switches to custom sizing. Persist the last preset along with shared dimensions. All graph cards and the final axis builder share one height (default 420px, bounded 320–900px). Any graph's bottom line separator changes the whole board height; its right-edge line separator changes the whole board width (240–1000px). Keep descriptive accessible names but no visible captions or tooltips. Both support pointer/touch dragging, axis-appropriate arrow keys (20px), and Home/End. Double-click restores the corresponding default. Direct width uses auto-fill tracks capped at container width; default layout uses responsive three/two/one columns. Resize boxes live and recompute scatter geometry on release without changing data, axes, identity, or order. Persist shared dimensions in the existing workspace; old workspaces retain data and default dimensions.

Each comparison card includes a swap-axes button. Swapping exchanges the saved x/y fields without changing card identity or order; title, accessible control names and plot labels update together. Duration uses a linear scale on either axis, count metrics retain log10(1+value), and view_count retains its minimum of 10 on either axis.

Axis swaps redraw only the selected card and retain its minimum height during SVG replacement. Do not rebuild or reappend the whole chart grid for a swap; preserve viewport position and focus without scrolling.

Analysis content categories canonicalize lookbook/fashion_lookbook/daily_lookbook/outfit_lookbook/streetwear_lookbook aliases to lookbook before chart grouping, filter options and record rendering. Apply this to cached snapshots and saved category filters as well as fresh API responses. Retain the original content_type_raw in projections without rewriting source analysis files or invoking Gemini. Preserve historical unknown values and mixed formats such as product_demo_lookbook; daily, outfit and streetwear lookbooks share the lookbook category. New Gemini responses use the shared finite content-type vocabulary with other as the fallback.

Recommendation generation calls Antigravity directly for six guide animations and their individual scene scripts, without a Flash planning stage. Include the brief and full reference scene_details in each guide prompt. Variants 1–3 follow R1/R2/R3 separately; 4–6 blend multiple references with explicit source features. Reserve six independent Sheets workloads sequentially, serialize Sheets bookkeeping, then POST all six agent requests concurrently. Preserve partial successes, stable variant order and each video's script; never automatically retry ambiguous generations. Show scene timing, camera framing, action, dialogue, captions and source-scene attribution inside a native disclosure per video card. Keep legacy cached common plans readable.

Recommendation errors retain an allowlisted provider error code through the adapter and job service. Show a specific HTTP 503 temporary-unavailability message instead of suggesting missing dependencies or credentials. Never expose raw provider exceptions or automatically replay an ambiguous paid generation.

New guides validate bounded JSON timelines and motion coordinates, then render local MP4 with Pillow/FFmpeg; never execute generated code. Keep response/motion/MP4 artifacts under analyzers/output/guides. Display six guides in one continuous list inside a collapsed disclosure, with a title-link overview and mode badges instead of two fixed groups. Each desktop card pairs a bounded video column with its open scene/script column; keep source descriptions in a closed native disclosure. Stack the card on narrow screens and retain download links and source features. Video URLs use validated UUID names, local-only paths and HTTP byte ranges; cache metadata in the browser and do not call models on restore or playback. Legacy PNG/SVG guides remain readable.

Legacy cached guide images retain their original storyboard heading, scene sizes and arrangement. Wrap the complete guide image, download link and token-limit note in native details/summary, closed by default. Summary switches between expand/collapse wording; pointer and keyboard toggles never call an API. Cache derived PNGs and a version/source-hash manifest beside the response; browser workspace cache retains the image across reloads. Migrate old saved guides through the local saved-image endpoint without model calls, and persist the migrated image after workspace restoration completes. Browser task deletion leaves server artifacts intact and states that scope in its existing dialog.

Reference search embeds analysis text and the brief using gemini-embedding-2 at 768 dimensions. Combine cosine and BM25 rankings with reciprocal rank fusion (k=60); show rank, cosine similarity and lexical score separately. Disclose transmission beside search, retain changed-text-only SQLite caching, and visibly fall back to BM25 on provider/cache failure. Restored workspaces never initiate embedding requests. First-time search may take longer; keep existing plans and guide cache intact during search.

Each guide-video card resolves its reference_ids against that saved result's retrieval references. Show source Reel IDs, title or analysis summary and safe Instagram links directly on the card; blend variants list all used sources. Do not resolve against a later search or call a model when restoring source attribution.

Collection and analysis numeric plot boards share chart-board.js and the existing comparison-grid styles. Keep their manipulation rules identical while persisting state in separate workspaces. Analysis retains categorical drilldowns above the metric board and propagates every active filter to its full point set. The analysis builder also exposes scene count; do not replace existing category evidence with numeric proxies.
