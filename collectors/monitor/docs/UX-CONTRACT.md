# Collector Monitor interaction contract

Personal localhost tool. Existing collector files and explicit current user requests
are the domain evidence. No existing frontend components or framework were found.

| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
|---|---|---|---|---|
| Select/Listbox | Native select in index.html | This contract | OS-owned popup, labeled options | Browser popup and keyboard inspection |
| Date | app.js date formatters | Asia/Seoul, ko-KR | Read-only timestamps, no date input | Rendered source timestamps |
| Command guidance | index.html examples + app.js copy handler | collector.ps1 and collector CLI options | Read-only snippets, collapsible options, clipboard feedback | CLI help and browser copy inspection |
| Form | app.js validate + recommend.js / server handlers | Structured collector options and recommendation brief | Inline errors, fixed fields, server revalidation | Validation, preview and confirmation checks |
| Scrollbar | styles.css global rules | DESIGN.md | Thumb/track/hover/active, forced-colors | Rendered overflow inspection |
| Toast | app.js notify | This contract | Single live region, inline persistent failures | Feedback inspection |
| Table navigation | server.py /api/data and app.js | collected_at descending | Server pagination: 20/50/100 | Actual records and range inspection |
| Search | app.js searchChanged | This contract | IME-safe 300ms debounce, cancel, clear, reset page | Search state inspection |
| Dialog | index.html dialog + app.js | This contract | App-owned native modal, title, inert background | Escape and focus inspection |
| Local operations | server.py POST handlers + runner.py | Explicit local actions | Origin/token checks, fixed commands, confirmation | Source and command preview inspection; live collection actions require an intentional user click |
| RAG recommendation | recommendations.py + gemini_recommend.py | collectors/data/collector/reel_analysis.json only | Local search; explicit Gemini transmission; shared SheetsKeyPool | Source/syntax review and local UI observation, no automatic live generation |
| Editable metric charts | chart-board.js + scatter.js | Saved per-page board state and filtered rows | Collection / analysis; native 2/3/4 select; keyboard and pointer resize | Live add/swap/order/delete, shared sizes and reload checks |

Background polling keeps the refresh button visually steady. Only pressing Refresh
disables it until the current request finishes; a press during an automatic poll
shares that in-flight request. Automatic data updates continue every three seconds.

Web current-error display tracks source/run/error/failure-count identity separately
from recurring status heartbeats. A later actual save resolves that identity; an
unchanged old scheduler error does not reactivate it. New errors/counter increments
reactivate warnings. The new collector also records last_error_at and clears local
errors on actual save and scheduler errors on batch success. Logs remain unchanged.
Android pulse is gated in both API and renderer by device-health state and two
consecutive confirmed failures. UI unreadable and worker staleness are informational
status only. ADB probes never inject input, restart apps or reboot devices.

Navigation has identical task groups on all four pages: one collection overview entry,
then collection visualization/analysis visualization/planning. The overview entry stays
active across both sources and all in-page sections; its local anchor preserves source
and filters. Live data, history and command guidance remain sections, not sidebar entries.
The source selector is the sole mode switch, labeled 신규 수집 / 재수집 in Korean.
Both sources share the 수집 현황 heading. Internal source keys and command filenames stay unchanged.
The brief uses
three required fields (product/target/purpose) and
two optional fields (selling_point/production_constraints), each at most 200 characters.
All inputs persist locally without credentials. Empty optional inputs are omitted
from requests; selling point affects retrieval and production constraints guide
generation without keyword matching. Legacy mood-era workspaces/results remain
readable, with mood shown only in historical summaries. Search invalidates when fields
change; generating locks inputs. The request ID is stored before POST; lost ACKs
poll the same ID and never auto-create another job. A missing job unlocks the form
and states the uncertainty after server restart. Terminal failures retain inputs
and prior result. Untrusted corpus and model text are rendered with textContent.
Only locally assembled references are sent; the client cannot inject model context.
Secrets remain in the existing Analyzer .env and subprocess. Gemini failure never
falls back to a local key pool. The provider boundary can later accept a local model
with the same JSON result contract; that provider is not implemented in this slice.

Table query, dataset, filter, round, korean, size and page persist in the URL. Command examples are static and contain no credentials; run options persist in localStorage without credentials. File data is treated as text;
only HTTPS instagram.com links may be opened from a record.

The monitor observes every three seconds independently of browser tabs. It keeps
last successful data if reads fail, distinguishes absent data from no search results,
and reports connection loss rather than leaving a green online indicator.
Table data can be paused without pausing collectors. Actions never auto-retry.

Start is blocked when a collector is detected using the shared profile. Only
monitor-owned collector jobs get a stop button; a stop file requests the existing
SIGINT graceful-stop handler and the corresponding queue stop. The UI displays
stopping until an actual completion record is written. No forced kill is offered.

Theme ownership: theme.js and styles.css share one document-wide light/dark
setting. The header toggle persists only the theme name in localStorage. Storage
failures leave the current session toggle usable. Theme choice is independent of
collection state, filters, paging and run options.

The live table collection-round select uses server filtering before pagination, combines with search and Android filters, persists round in the URL, and resets page on selection. Options follow observed dataset rounds, with all and unknown variants.

Korean classification uses saved korean true/false/null via classification.py and korean-filter.js.
Country disclosure does not reclassify saved korean values. Undisclosed account_country is blank.
Unknown displays - and has its own 미확인 (-) filter. False requires confirmed foreign
provenance; saved korean=false remains foreign regardless of country disclosure. Global and table native selects
share one URL state, reset page and combine with other table filters before paging.
An older overview response cannot overwrite a newly selected category. Physical
process/queue/runtime/error observations stay global; only recorded metrics filter.
Visualization category choices persist with existing cached data; old snapshots
remain usable and receive a missing-classification note instead of invalidation.
Record details offer a native Korean/foreign/unknown select and explicit 구분 저장.
The POST /api/korean handler uses existing host/origin/token checks, resolves an
existing dataset and saved Reel, and persists its manual override across all rounds.
Pending saves disable duplicate submission; failures retain the choice for retry;
success stays in the dialog and refreshes the owning list and metric summaries.
A durable override with an export lock error shows saved-with-pending-export copy.

Event rows use keyboard-accessible native details for full raw messages; expanded IDs survive polling. Compact tables retain search, round/Android filters, URL state, pagination, and the existing record dialog.

Panel split ownership: app.js and .resizable-panels. Focusable role=separator exposes vertical orientation and current percentage, supports keyboard and pointer capture, and persists non-sensitive presentation ratios. Split controls are hidden when panels stack.

File visualization owns `/visualize.html`, visualize.js/css and visualization_data.py.
The file selection POST uses the existing Origin/host/token gate and a separate
20 MiB body limit; other POST limits remain unchanged. Parsing is read-only, memory-only,
bounded to 100,000 normalized records, 256 columns, 64 MiB expanded XLSX XML and
20 worksheets. Malformed/unsupported files receive actionable inline errors.
Metric summaries exclude missing/private/unavailable values from denominators and
include actual zero. Latest-per-Reel chooses the newest timestamp, using file order
only when timestamps are absent. Rows without URL are not silently deduplicated.
No data is fetched from Instagram. Sheet/round/scope/metric changes redraw locally.

Visualization cache ownership: workspace-cache.js (CollectorWorkspaceCache.action), visualize.js and browser IndexedDB database
`collector-monitor-visualization`, one schema-versioned `last` workspace. Cache writes
commit before showing saved status; quota/private-mode/storage errors are visible
and never discard the currently displayed data. New file reads supersede late cache
restores, and save/clear completion messages respect the latest operation revision.
Persist file data and filter selections together. File close preserves the cache;
Delete saved work removes it. Browser reload/navigation restores the last committed
workspace without reading or uploading the original file again.

Recommendation cache uses `collector-monitor-recommendation` with the same
workspace/last store/key and schema version 1. Save input, selected dataset, retrieval
snapshot, completed generation and active job ID together. Commit before reporting
saved; retain current output on storage failure. Restore never sends generation,
never restores transmission consent, and requires fresh retrieval before new
generation. In-flight recovery locks controls before configuration lookup finishes.
The delete dialog is modal, cancel-first, and prevents cancellation during its
transaction. Cache restore validates result/reference shapes and timing continuity;
cached URLs are never used as executable href values. Each plan links directly to
its original canonical Reel IDs, even if the currently displayed search has changed.

The generation action calls Antigravity directly for six parallel video plans,
without a Flash planning call. Each video owns its contiguous scene timing,
camera framing, action, dialogue, caption and source-scene attribution. The
native disclosure in each video card shows the script; it is text, not synthesized
audio. Completed videos/scripts survive sibling failures and restore together
from the existing workspace cache. No paid generation runs during restore.
Camera framing is resolved by reference_id plus source_scene, never by one
global torso template. Reviewed source-frame observations in
analyzers/reference_framing.json apply only to their exact Reel/scene keys.
Face exclusion adjusts the top boundary while retaining the original lower-body
range. Preserve mirror shots, product-down shots, sleeve inserts and flat lays
as distinct layouts. Reload saved video metadata through the read-only video-plan
endpoint so local rerenders and cached scene descriptions stay aligned.
New video guides use integer durations of 5–30 seconds. The legacy Flash plan
display remains readable for existing cached work.
The reference-country native select offers all, Korean, foreign and unknown.
Reuse classification.py and saved manual overrides; unknown is never foreign.
Filter the corpus before either semantic or lexical retrieval. Save the selection
with the workspace, invalidate retrieval consent on changes, and retain the
country scope on the retrieval snapshot without changing embedding input/cache keys.
Reference metrics join existing collector/refresh/Fashion Reel exports by shortcode,
with XLSX fallback and newer author follower observations from users.json. Keep
collection provenance; never rewrite analysis or fabricate zeros for missing data.
New transmissions omit unavailable metrics and mark private/missing fields. Zero
remains a valid observed count. An optional account follower input reranks the top
30 content candidates using 70% normalized content score and 30% logarithmic
account-size similarity. Missing followers get no size bonus. Keep original cosine
and BM25 values separately visible; follower changes do not change embedding keys.

Recommended duration is an integer 5–180 seconds in the legacy planning contract.
Each scene uses contiguous integer start/end seconds covering 0..duration_seconds.
Gemini output is rejected when timing is invalid; legacy cached plans without
duration remain readable with explicit copy. Reference duration metadata separates
collector values from approximate Analyzer scene-end values and missing values.

Analysis visualization owns analysis.html/js/css and analysis_data.py, reading only
the same fixed Fashion analysis file as recommendations. Reads are bounded to
32 MiB and 10,000 records; invalid records are excluded with counts, duplicate
shortcodes prefer the later file record. No original writes or AI calls occur.
One IndexedDB workspace in collector-monitor-analysis preserves the snapshot,
filters, chart drilldown and page. Cache is restored without automatic source refresh;
explicit reload reads the file and keeps the previous result on error. Text search
is IME-safe with a 300ms debounce. Every chart category is a native button; chosen
category has a clear action to remove it. Filter changes reset paging, and source
reload clamps an out-of-range page. Detailed evidence uses a native modal with
close-first focus, Escape and canonical shortcode-only Instagram links.

Collector launch and Android preparation forms live above job logs in the right execution panel.
Legacy #commands remains directly accessible; #controls redirects to #commands.
Copying a command never executes it and works independently of monitor API health.
Clipboard denial uses the shared toast and leaves selectable command text.
Prior monitor-owned jobs retain log and confirmed graceful-stop controls.

The right panel combines settings and jobs. Starting or preparing Android requires
explicit confirmation; polling and preview never start jobs. Busy/offline states
disable launch, and a pending request prevents duplicate submission. Settings are
restored from existing localStorage. Both management panels use natural height.

The scheduled launcher passes unlimited discovery (max_items=0) within the existing time budget. Removed saved-item count options are not restored from localStorage or emitted by preview/start APIs. Old browser storage may contain a window field; the form has no such named control and ignores it.

## Refresh separation

The source query parameter is collector by default and refresh for the dedicated tab. Overview datasets are filtered on the server; data/events requests with a conflicting source return no records. Switching source is a regular link navigation so prior page state remains in browser history. Automatic discovery includes collectors/data/refresh* roots and data* roots containing .collector/refresh_inbox; the configured collector root remains the existing default. Inbox statistics deduplicate URL sets and apply persistent cancellations. Registered counts are historical registrations, not completion or active-work counts. Capacity/reservations name their saved-state timestamp. Missing legacy inbox files display unknown counts with explanatory copy, and malformed queue files do not break collector status. Existing records, overrides, queue files and workbooks are never moved to populate this tab.

Source tabs and filters share a toolbar, using native link navigation and existing native selects. Navigation icons use identical SVG viewboxes and strokes on all four pages. Copy cleanup preserves every existing control ID, chart labels, error/empty/pending feedback and consent. Normal polls do not print repeated instructions; event detection timestamp semantics remain available on the time element tooltip. Detailed schedule dates remain in a native disclosure inside runtime, and missing runtime/inbox sections do not occupy empty cards.


Refresh runtime is recorded atomically by the refresh supervisor in .collector/refresh_status.json.
The record spans waiting and batch retries, retains actual interval/basis/test settings,
and freezes elapsed time at the recorded terminal timestamp. A missing terminal time
with a dead or unverified PID never keeps a live elapsed clock. Legacy data shows
only the last batch time under an explicit batch heading, with default intervals
marked as unrecorded defaults. The interval table uses actual saved custom stage
values or a single fixed/test interval; first-collected age and last-collected due
anchors are named separately. UI polling never starts or restarts a collector.


The Refresh overview omits the input-status panel. A dedicated guidance row places
intervals on the left and the read-only options/command guide on the right; it stacks
below 1000px. The command copy action remains explicit and never launches collection.
Queue files and backend admission/cancellation behavior are unchanged by panel removal.

Analysis preserves its category charts and uses the shared chart-board.js metric board below them. Defaults are six follower plots, six view plots and duration vs views/likes. The builder stays last; add, reorder, swap, delete, synchronized width/height and 2/3/4 columns follow collection behavior. Store board state with the analysis workspace independently from collection settings. The same filtered full row set drives every plot, never the paginated table. Join all seven metrics through the shared collector projection; prefer collected video duration and fall back to scene-end estimates. Reload old snapshots missing the added metric fields without discarding their filters or layout.

The analysis evidence table has a shortcode substring search at the top right. It combines with analysis filters but only narrows table rows, leaving graphs unchanged. Search is IME-safe, debounced, persisted in the analysis workspace and resets paging. Keep the page controls centered and the result range at the far right of the same footer row.

Guide videos distinguish the illustrative shooting setup from the recorded picture. Show the whole figure with a shaded floor, dim the off-camera area and mark the actual source-scene crop with a teal frame. Render the mannequin with solid 3D torso and articulated limb meshes; rotation preserves side depth and hides front details behind the body. The solid camera prop is an illustrative object, not a measured rig position. Context geometry must not mutate stored source framing or scripts; non-person detail shots keep their subject-specific view.

Camera angle and subject torso orientation are independent. New analyses use shared textual body/head/gaze directions and camera-viewpoint grades from scene_directions.py; absent legacy observations stay absent. Confidence is a textual level, never a new numeric angle or score. Guide rendering alone converts labels to consistent illustrative geometry presets; these are not source measurements. Legacy coarse facing and saved numeric guide poses remain readable. Unspecified body direction uses an illustrative three-quarter pose, never a claim about the source. The uploaded DeliKNPprGK Look 2 image supplies categorical three-quarter evidence for source scene 2 only. Each MP4 stacks the scene guide above a synchronized 3D camera diagram. Camera, subject, optical-axis aim and four frustum corners share world coordinates and projection; dashed lines connect the lens to the target and capture rectangle. Preserve high/low/side/overhead differences. Optional aim_height overrides frame-centre aiming. Distances/heights remain illustrative; do not imply measured source geometry. The scene script retains action/dialogue/captions in the adjacent text panel.

Guide results use one six-card list with title anchor navigation and per-card format/blend badges. Pair the video and download on the left with the open scene/script disclosure on the right. Scripts use natural document scrolling without an inner scroll region; the desktop video stays sticky within its card. Narrow cards stack with static video positioning. Keep reference links, descriptions and source features in a separate closed disclosure. Navigating, expanding or playing a guide never generates a request to a model.

Each numbered guide is an independent native disclosure, initially open. Its title toggles the entire guide, closing pauses its video, and overview links open their target before navigation. Compact scene rows retain timing, framing, action, dialogue and caption; show the saved reference shortcode link plus source-scene number directly below each scene. Resolve links only against that result's saved references and explicitly label unavailable links.
