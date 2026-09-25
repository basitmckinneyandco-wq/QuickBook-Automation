/* Dashboard logic: talk to the local Python app, draw each view, send batches. */

const state = {
  records: [],
  activity: [],
  health: null,
  selected: new Set(),
  editing: null,   // source_id of the row currently open for editing
  invoicing: null, // {sourceId, kind} while the invoice picker is open
  sending: new Set(),  // the row QuickBooks is working on right now
  queued: new Set(),   // rows waiting their turn in the current batch
  sendingOperation: "",  // which action the batch is running, so the progress
                         // badge appears in that column and no other
  search: "",
  status: "all",
  invoiceStatus: "all",
  invoiceKind: "all",
  // Rows sent one at a time: source_id -> {phase, operation, label}. Held in
  // state so the badge survives the table being rebuilt when another row lands.
  rowJobs: new Map(),
  // Rows kept on screen a moment after they stop matching the filters, so a
  // result can be read before the row leaves: source_id -> "hold" | "leaving".
  lingering: new Map(),
  source: "all",
  dateRange: "all",
  dateFrom: "",
  dateTo: "",
  historyFilter: "all",
  historySearch: "",
  historyAction: "all",
  historyWhen: "all",
  view: "dashboard",
};

const $ = (id) => document.getElementById(id);
const DONE = new Set(["completed", "already_exists"]);
// Settled, but not by being created: deliberately marked as never wanted. Kept
// apart from DONE so it is never counted as work achieved, and apart from the
// pending states so it is never counted as work outstanding.
const NOT_NEEDED = "not_needed";

/** Is this kind of invoice still owed on this row? */
function invoiceOutstanding(record, kind) {
  const status = record[`${kind.key}_status`];
  return Boolean(kind.operation) && !DONE.has(status) && status !== NOT_NEEDED;
}

// The sheet has thousands of rows; painting them all locks the browser up.
const RENDER_CAP = 300;

// The sheet's verified accounting block, in the order it appears in Google Sheets.
// `from` reads a mapped field off the record; anything else comes from record.extra.
const SHEET_COLUMNS = [
  // Labels are the tidy spelling; the sheet's own headers (BULLING, Verfied,
  // Commision) are still what the importer matches on.
  { key: "customer_name", label: "Corporation Name", from: true, sticky: true },
  { key: "policy_type", label: "Policy type" },
  { key: "billing", label: "Billing" },
  { key: "carrier_type", label: "Carrier type" },
  { key: "sheet_status", label: "Status" },
  { key: "invoice_date", label: "EFF", from: true, type: "date" },
  { key: "item", label: "Carrier", from: true },
  { key: "job_name", label: "Line of Business", from: true },
  { key: "invoice_number", label: "Policy No#", from: true, mono: true },
  { key: "premium", label: "Pure Premium", type: "money" },
  { key: "total_premium", label: "Total Premium", type: "money" },
  { key: "mckinney_fee", label: "McKinney Fee", type: "money" },
  { key: "commission_rate", label: "Commission %", type: "percent" },
  { key: "amount", label: "Commission", from: true, type: "money" },
  { key: "description", label: "Comm Inv", from: true, wide: true },
  { key: "net_due", label: "Net due", type: "money" },
  { key: "prem_inv", label: "Prem inv", wide: true },
  { key: "fees_invoice", label: "Fees invoice" },
  { key: "policy_class", label: "Class" },
  { key: "lead_source", label: "Lead source" },
];

/* The app's own columns sit in the same reorderable list as the sheet's, so
   they can be dragged around too. They build their own cell rather than just
   printing a value, hence `render`. */
const EXTRA_COLUMNS = [
  {
    key: "__type",
    label: "Type",
    fit: true,
    render: (record) => {
      const cell = td();
      cell.className = "fit";
      cell.dataset.key = "__type";
      if (record.source_tab) cell.append(tag(record.source_tab, ""));
      return cell;
    },
  },
  {
    key: "__status",
    label: "Customers/Jobs",
    fit: true,
    render: (record, view) => {
      const cell = td();
      cell.className = "fit";
      cell.dataset.key = "__status";
      // A batch in flight speaks for itself: this row is being worked on now,
      // or is waiting its turn behind the others.
      const live = batchBadge(record.source_id, "customer");
      if (live) {
        cell.append(live);
        return cell;
      }
      const badge = view.simulated
        ? tag(`${STATUS_LABEL[record.status]} (safe test)`, "simulated")
        : tag(STATUS_LABEL[record.status] || record.status, view.status);
      if (view.detail) {
        badge.title = view.detail;
        badge.append(document.createTextNode(" ⓘ"));
      }
      if (record.status_elsewhere) markFromSheet(badge);
      cell.append(badge);

      // Inline actions: act on this one policy without selecting it first.
      const actions = document.createElement("div");
      actions.className = "row-actions";
      if (!view.blocked && !view.confirmedInQb) {
        actions.append(
          rowActionButton("Send", (btn) => actOnPolicy(record.source_id, "create_customer", btn))
        );
      }
      if (view.confirmedInQb) {
        actions.append(
          rowActionButton("Update", (btn) => actOnPolicy(record.source_id, "update_customer", btn))
        );
      }
      if (actions.children.length) cell.append(actions);
      return cell;
    },
  },
  {
    key: "__invoice",
    label: "Invoice",
    fit: true,
    render: (record, view) => {
      const cell = td();
      cell.className = "fit";
      cell.dataset.key = "__invoice";
      const live = batchBadge(record.source_id, "invoice");
      if (live) {
        cell.append(live);
        return cell;
      }
      // One badge per kind this row can raise, each prefixed so several
      // greens are still readable. Fees only appear where there is a fee.
      for (const kind of invoiceKindsFor(record).filter((entry) => entry.operation)) {
        const status = record[`${kind.key}_status`] || "ready";
        const label = `${kind.short} ${STATUS_LABEL[status] || status}`;
        const badge = view.live || !DONE.has(status)
          ? tag(label, status)
          : tag(`${label} (safe test)`, "simulated");
        const reasons = record[`${kind.key}_reasons`] || [];
        const error = record[`${kind.key}_error`];
        // A finished invoice carries no warning. The reasons describe what
        // would stop it being raised, and once it has been raised they are
        // simply out of date - a green "Created" beside a warning icon reads
        // as though something went wrong when nothing did.
        const detail = error && status === "failed"
          ? error
          : (DONE.has(status) || status === NOT_NEEDED ? "" : reasons.join(" "));
        if (detail) {
          badge.title = detail;
          badge.append(document.createTextNode(" ⓘ"));
        }
        // Done on someone else's dashboard, and read back from the sheet. Worth
        // marking: this machine has no History entry for it, and without the
        // mark that looks like a fault rather than someone else's work.
        if (record[`${kind.key}_elsewhere`]) markFromSheet(badge);
        cell.append(badge);
      }

      // Same shape as the customer column: Send while there is nothing there,
      // Update once every invoice this row can raise has been raised.
      const outstanding = invoiceKindsFor(record).some(
        (kind) => invoiceOutstanding(record, kind)
      );
      const actions = document.createElement("div");
      actions.className = "row-actions";
      actions.append(
        rowActionButton(outstanding ? "Invoice" : "Update", () =>
          openInvoicePicker(record.source_id)
        )
      );
      cell.append(actions);
      return cell;
    },
  },
];

/* "Sending…" or "Queued" while a batch passes over this row - but only in the
   column the batch is actually working on. Showing it in both made sending
   customers look as though invoices were being raised too. */
function batchBadge(sourceId, column) {
  // A row sent on its own is tracked here rather than in the button, because
  // any other row finishing rebuilds the whole table and would wipe a badge
  // that lived only in the markup.
  const own = state.rowJobs.get(sourceId);
  if (own && operationColumn(own.operation) === column) {
    if (own.phase === "sending") {
      const badge = tag("", "sending");
      const spinner = document.createElement("span");
      spinner.className = "spinner-sm";
      badge.append(spinner, document.createTextNode(own.label || "Sending…"));
      return badge;
    }
    return tag("Queued", "queued");
  }

  if (batchColumn() !== column) return null;
  if (state.sending.has(sourceId)) {
    const badge = tag("", "sending");
    const spinner = document.createElement("span");
    spinner.className = "spinner-sm";
    badge.append(spinner, document.createTextNode("Sending…"));
    return badge;
  }
  if (state.queued.has(sourceId)) return tag("Queued", "queued");
  return null;
}

/** Mark a badge as something another dashboard did, read back from the sheet. */
function markFromSheet(badge) {
  const said = badge.title ? `${badge.title}  ` : "";
  badge.title = `${said}Done on another computer — read from the Google Sheet, `
              + `so this machine has no History entry for it.`;
  const mark = document.createElement("span");
  mark.className = "sheet-mark";
  mark.textContent = "↗";
  mark.setAttribute("aria-label", "from the sheet");
  badge.append(mark);
}

/** Which column the running batch belongs to: "customer", "invoice" or "". */
function batchColumn() {
  return operationColumn(state.sendingOperation);
}

/** Which column an operation belongs to: "customer", "invoice" or "". */
function operationColumn(operation) {
  if (!operation) return "";
  return INVOICE_KINDS.some((kind) => kind.operation === operation)
    ? "invoice"
    : "customer";
}

/** Mark one row as queued or in flight, and redraw it. */
function markRow(sourceId, phase, operation, label = "") {
  state.rowJobs.set(sourceId, { phase, operation, label });
  refreshRow(sourceId);
}

function clearRow(sourceId) {
  state.rowJobs.delete(sourceId);
}

/* Redraw one row in place. Clicking Send changes a single badge, and
   rebuilding all three hundred rows to show it made the click itself feel
   heavy - the work happened before the browser could paint the press. */
function refreshRow(sourceId) {
  const row = document.querySelector(`.sheet-grid tbody tr[data-id="${CSS.escape(sourceId)}"]`);
  if (!row) return;
  const record = state.records.find((entry) => entry.source_id === sourceId);
  if (!record) return;
  row.replaceWith(buildRow(record));
}

/** The live Invoice button for a row, re-found after every re-render. */
function rowInvoiceButton(sourceId) {
  const row = document.querySelector(`.grid tbody tr[data-id="${CSS.escape(sourceId)}"]`);
  return row ? row.querySelector('[data-key="__invoice"] .mini-btn') : null;
}

/* The invoice popup's choices, ticked in any combination. Only the premium
   invoice reaches QuickBooks so far; the other two are listed so the picker
   shows the whole shape of the job and says plainly what is not ready yet.

   A fees invoice only makes sense when there is a fee to bill, so that choice
   appears only on rows whose McKinney Fee is above zero. */
const INVOICE_KINDS = [
  {
    key: "premium",
    short: "Prem",
    label: "Premium invoice",
    note: "Net due, on the Premium A/R account, item from Carrier Type.",
    operation: "premium_invoice",
    amount: (record) => record.premium_amount,
    applies: () => true,
  },
  {
    key: "commission",
    short: "Comm",
    label: "Commission invoice",
    note: "Commission, on the Commission A/R account, item is the carrier.",
    operation: "commission_invoice",
    amount: (record) => record.commission_amount,
    // No commission earned, no commission invoice. Same rule as fees: the
    // choice is not offered rather than offered and refused.
    applies: (record) => moneyValue(record.commission_amount) > 0,
  },
  {
    key: "fees",
    short: "Fees",
    label: "Fees invoice",
    note: "McKinney Fee, on the Handling Fees A/R account, item is Fees.",
    operation: "fees_invoice",
    amount: (record) => record.fees_amount,
    applies: (record) => moneyValue(record.fees_amount) > 0,
  },
];

function moneyValue(raw) {
  const number = Number(String(raw ?? "").replace(/[^0-9.-]/g, ""));
  return Number.isFinite(number) ? number : 0;
}

function invoiceKindsFor(record) {
  return INVOICE_KINDS.filter((kind) => kind.applies(record));
}

const COLUMN_ORDER_KEY = "qb-column-order";
let savedColumnOrder = null;

function movableColumns() {
  return [...EXTRA_COLUMNS, ...SHEET_COLUMNS.slice(1)];   // [0] is pinned
}

const COLUMN_BY_KEY = new Map(
  [...EXTRA_COLUMNS, ...SHEET_COLUMNS].map((column) => [column.key, column])
);

/** The user's saved order, reconciled against the columns that exist today. */
function columnOrder() {
  if (savedColumnOrder) return savedColumnOrder;
  const fallback = movableColumns().map((column) => column.key);
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem(COLUMN_ORDER_KEY) || "null");
  } catch {
    saved = null;
  }
  if (!Array.isArray(saved)) {
    savedColumnOrder = fallback;
    return savedColumnOrder;
  }
  // Drop columns that no longer exist, then append any added since it was saved.
  const known = new Set(fallback);
  const kept = saved.filter((key) => known.has(key));
  savedColumnOrder = [...kept, ...fallback.filter((key) => !kept.includes(key))];
  return savedColumnOrder;
}

function saveColumnOrder(order) {
  savedColumnOrder = order;
  try {
    localStorage.setItem(COLUMN_ORDER_KEY, JSON.stringify(order));
  } catch {
    /* private window - the order just won't persist */
  }
}

const STATUS_LABEL = {
  ready: "Pending",
  review: "Needs review",
  needs_review: "Needs review",
  completed: "Created",
  already_exists: "Already there",
  failed: "Failed",
  not_needed: "Not needed",
};

const PAGES = {
  dashboard: ["Dashboard", "Overview of your queue"],
  sheet: ["Policies", "Review your policies and send them to QuickBooks"],
  status: ["Status", "Connection and safety settings"],
  history: ["History", "Every action this app has taken"],
};

/* ═══ theme ═══════════════════════════════════════════════════════════════ */

const THEMES = ["light", "dark"];

function readTheme() {
  try {
    const saved = localStorage.getItem("qb-theme");
    return THEMES.includes(saved) ? saved : "light";
  } catch {
    return "light";
  }
}

/* Swapping the theme repaints every colour at once, which cuts rather than
   fades. Transitioning each element instead is worse: with a few hundred log
   entries on screen that is a few hundred simultaneous animations, and the
   page stutters while text fades through a stage where it is invisible.

   A view transition avoids both. The browser takes a picture of the page
   before and after and cross-fades the two as images, on the compositor - one
   animation whatever the page holds. Where it is not supported the swap is
   instant, which is what it always was. */
function applyTheme(theme, { animate = false } = {}) {
  const root = document.documentElement;
  const swap = () => {
    root.dataset.theme = theme;
    $("themeToggle").checked = theme === "dark";
  };
  const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (animate && !still && typeof document.startViewTransition === "function") {
    // Tags, tiles and buttons carry their own colour transitions for hover.
    // Left alone they run a second time underneath the cross-fade, each on its
    // own timing, which reads as borders lagging behind the rest of the page.
    // For the length of the swap they are told to change instantly; the
    // cross-fade is doing the animating.
    const transition = document.startViewTransition(() => {
      root.classList.add("theme-instant");
      swap();
    });
    transition.finished.finally(() => root.classList.remove("theme-instant"));
  } else {
    swap();
  }
  try {
    localStorage.setItem("qb-theme", theme);
  } catch {
    /* private window — the toggle still works for this visit */
  }
}

/* ═══ sidebar ═════════════════════════════════════════════════════════════ */

const SIDEBAR_KEY = "qb-sidebar";

function sidebarCollapsed() {
  try {
    return localStorage.getItem(SIDEBAR_KEY) === "collapsed";
  } catch {
    return false;
  }
}

function applySidebar(collapsed) {
  document.documentElement.dataset.sidebar = collapsed ? "collapsed" : "open";
  const button = $("sidebarToggle");
  const label = collapsed ? "Show sidebar" : "Hide sidebar";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.setAttribute("aria-expanded", String(!collapsed));
  try {
    localStorage.setItem(SIDEBAR_KEY, collapsed ? "collapsed" : "open");
  } catch {
    /* private window - it just won't be remembered */
  }
}

/* ═══ navigation ══════════════════════════════════════════════════════════ */

function goto(view) {
  if (!PAGES[view]) return;
  state.view = view;
  for (const section of document.querySelectorAll(".view")) {
    section.hidden = section.id !== `view-${view}`;
  }
  for (const item of document.querySelectorAll(".nav-item")) {
    item.classList.toggle("is-active", item.dataset.view === view);
  }
  const [title, subtitle] = PAGES[view];
  $("pageTitle").textContent = title;
  $("pageSub").textContent = subtitle;
  window.scrollTo({ top: 0 });
  // Read afresh each time: QuickBooks may have been opened, closed or pointed
  // at a different company since this page was last looked at.
  if (view === "status") loadCompanies();

  playEntrance($(`view-${view}`));
}

/* Replay the cards' 3D entrance for the page being opened. Removing the class
   and forcing a reflow before adding it back is what restarts a CSS animation
   on elements that were never destroyed. */
function playEntrance(section) {
  if (!section) return;
  section.classList.remove("enter");
  void section.offsetWidth;
  section.classList.add("enter");
}

/* ═══ server calls ════════════════════════════════════════════════════════ */

/* Every call gets a deadline. Without one a single stalled reply leaves the
   page waiting for ever - the work itself may well have finished, and the
   dashboard should go and find out rather than sit on a spinner. */
/* The most policies that may be selected, and so sent, at once. The server
   refuses a bigger batch too - this stops one click on the header checkbox
   from aiming thousands of rows at QuickBooks. */
const MAX_SELECTION = 50;

const API_TIMEOUT_MS = 30000;
const SEND_TIMEOUT_MS = 180000;   // writes wait on QuickBooks, so far longer
const RECORDS_TIMEOUT_MS = 90000; // the whole policy list is a large reply

class TimedOut extends Error {}

async function api(path, options, timeoutMs = API_TIMEOUT_MS) {
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), timeoutMs);
  let response;
  try {
    response = await fetch(path, { ...options, signal: abort.signal });
  } catch (error) {
    if (error.name === "AbortError") {
      throw new TimedOut("The app did not answer in time.");
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    throw new Error("The local app sent a reply we could not read.");
  }
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status}).`);
  return payload;
}

let pollTimer = null;

async function refresh() {
  try {
    // 2,000+ rows is a big payload; give it more than the ordinary deadline.
    const [data, history] = await Promise.all([
      api("/api/records", {}, RECORDS_TIMEOUT_MS),
      api("/api/activity", {}, RECORDS_TIMEOUT_MS),
    ]);
    state.records = data.records || [];
    state.health = data.health || null;
    state.activity = history.activity || [];
    for (const id of [...state.selected]) {
      if (!state.records.some((record) => record.source_id === id)) state.selected.delete(id);
    }
    setLink(true, "Connected");
    renderAll();

    // The sheet downloads in the background at startup; keep checking until it lands.
    clearTimeout(pollTimer);
    if (state.health && state.health.loading) pollTimer = setTimeout(refresh, 1500);
  } catch (error) {
    setLink(false, "Not connected");
    toast(error.message, true);
  }
}

/* ═══ watching the sheet ══════════════════════════════════════════════════
   The server re-reads the Google Sheet every minute. This asks it, often and
   cheaply, whether anything actually changed - and only pulls the rows when
   the answer is yes, so the numbers count up and the new rows slide in
   instead of the whole table blinking every few seconds. */

// How often the page asks whether anything has changed. The answer is a few
// hundred bytes and usually "no", so this can be brisk - it is half of how
// long a colleague's status takes to appear, and the slower half is the read
// from Google.
const WATCH_MS = 6000;

function startWatching() {
  setInterval(checkForChanges, WATCH_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) checkForChanges();   // catch up the moment you come back
  });
}

/** True while the page must be left alone: mid-edit, or mid-send. */
function pageIsBusy() {
  return (
    queueDepth > 0 ||
    $("editDialog").open ||
    $("invoiceDialog").open ||
    $("askDialog").open ||
    document.hidden
  );
}

async function checkForChanges() {
  if (pageIsBusy()) return;
  let health;
  try {
    health = await api("/api/health");
  } catch {
    setLink(false, "Not connected");
    return;
  }
  setLink(true, "Connected");
  const known = state.health ? state.health.revision : undefined;
  state.health = health;

  // Redrawn even when the rows themselves have not changed. "Last read" and
  // the feed's state move every minute whether or not the sheet does, and
  // returning early left the Status page frozen at whatever it said when it
  // was opened - which reads as the app having stopped refreshing.
  renderHealth();
  if (state.view === "status") renderStatus();

  if (health.revision === known || known === undefined) return;

  // Something in the sheet moved, so pull the rows and let the usual card and
  // row animations show what changed.
  try {
    const before = state.records.length;
    const data = await api("/api/records", {}, RECORDS_TIMEOUT_MS);
    if (pageIsBusy()) return;   // the user started something while we were fetching
    state.records = data.records || [];
    state.health = data.health || health;
    for (const id of [...state.selected]) {
      if (!state.records.some((record) => record.source_id === id)) state.selected.delete(id);
    }
    renderAll();
    const added = state.records.length - before;
    if (added > 0) toast(`${added} new polic${added === 1 ? "y" : "ies"} from the sheet.`);
  } catch {
    /* leave what is on screen; the next tick tries again */
  }
}

/* ═══ rendering ═══════════════════════════════════════════════════════════ */

function renderAll() {
  renderHealth();
  renderDashboard();
  renderTable();
  renderStatus();
  renderActivity();
  renderSelection();
}

function counts() {
  const result = { ready: 0, review: 0, done: 0 };
  for (const record of state.records) {
    if (DONE.has(record.status)) result.done += 1;
    else if (record.status === "review" || record.status === "needs_review") result.review += 1;
    else result.ready += 1;
  }
  return result;
}

function renderHealth() {
  const health = state.health;
  if (!health) return;
  const live = Boolean(health.writes_enabled);
  const label = live ? "LIVE" : "SAFE MODE";

  for (const id of ["modePill", "modeBadge"]) {
    const badge = $(id);
    badge.classList.toggle("live", live);
    // A pulsing dot beside the word, so "LIVE" reads as something currently
    // switched on rather than a static label.
    const dot = document.createElement("i");
    dot.className = "pulse";
    dot.setAttribute("aria-hidden", "true");
    badge.replaceChildren(dot, document.createTextNode(label));
  }

  // No standing badge for an open Customer Centre: it sat there whenever
  // QuickBooks happened to have that window open, which is most of the time
  // and almost never about to matter. The check that counts still runs at the
  // moment of sending, in quickBooksIsReady().

  $("sideCompany").textContent = health.company.display_name || "No company set";
  $("navStatusDot").classList.toggle("on", !live);

  const select = $("sourceFilter");
  const tabs = health.tabs || [];
  if (tabs.length && select.options.length !== tabs.length + 1) {
    const current = state.source;
    select.replaceChildren(
      ...[["all", "All"], ...tabs.map((name) => [name, name])].map(([value, label]) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        return option;
      })
    );
    select.value = tabs.includes(current) || current === "all" ? current : "all";
    state.source = select.value;
  }
  select.disabled = Boolean(health.loading);

  const stateLine = $("modeState");
  stateLine.textContent = live ? "Writes are ON" : "Writes are OFF";
  stateLine.classList.toggle("live", live);
  stateLine.classList.toggle("safe", !live);
  $("modeExplain").textContent = live
    ? `Rows you send are created for real in ${health.company.display_name}.`
    : `Nothing reaches QuickBooks. ${capitalise(health.blocked_reason || "")}`;
}

function renderDashboard() {
  const health = state.health;
  const tally = counts();
  // Counted up rather than snapped, to match the cards on the Policies page.
  countTo("tileLoaded", state.records.length, whole);
  countTo("tileReady", tally.ready, whole);
  countTo("tileReview", tally.review, whole);
  countTo("tileDone", tally.done, whole);
  $("tileSource").textContent =
    health && health.source_name ? `Bound & done on the sheet` : "No file yet";

  // "21" on its own said nothing. Spell out what was counted: those 21
  // policies became 21 jobs under however many distinct customers.
  const live = Boolean(health && health.writes_enabled);
  const customers = new Set();
  for (const record of state.records) {
    if (isInQuickBooks(record, live) && record.customer_name) {
      customers.add(record.customer_name.toLowerCase());
    }
  }
  $("tileDoneSub").textContent = tally.done
    ? `${customers.size} customer${customers.size === 1 ? "" : "s"} · ` +
      `${tally.done} job${tally.done === 1 ? "" : "s"}`
    : "nothing sent yet";

  const blocked = countBlockedForInvoices();
  $("tileReadySub").textContent = blocked
    ? `${blocked.toLocaleString()} still need details to invoice`
    : "customer, job and invoices";
  $("tileReviewSub").textContent = "cannot be sent yet";

  $("navSheetCount").textContent = state.records.length || "";
  $("navHistoryCount").textContent = state.activity.length || "";

  renderMix();
  renderInvoiceBars();
  renderBlockers();
  renderUpcoming();
  drawActivity($("dashActivity"), state.activity.slice(0, 5));
}

/* ═══ dashboard panels ════════════════════════════════════════════════════ */

/* Of the rows counted as ready to send, how many cannot be invoiced yet.
   Scoped to exactly the rows the tile above it counts, so the smaller number
   can never exceed the bigger one. */
function countBlockedForInvoices() {
  let blocked = 0;
  for (const record of state.records) {
    if (DONE.has(record.status)) continue;                // already sent
    if ((record.review_reasons || []).length) continue;   // counted as needs-review
    const stuck = INVOICE_KINDS.some(
      (kind) =>
        kind.operation &&
        kind.applies(record) &&
        (record[`${kind.key}_reasons`] || []).length
    );
    if (stuck) blocked += 1;
  }
  return blocked;
}

/* The reasons are written as full sentences for the row tooltips, which is too
   long for a bar label. These trim them to the point being made. */
const SHORT_REASON = [
  [/^Carrier Type is empty/i, "Carrier Type missing"],
  [/^Class is empty/i, "Class missing"],
  [/^Carrier is empty/i, "Carrier missing"],
  [/^Commission must be greater/i, "Commission is zero"],
  [/^Net due must be greater/i, "Net due is zero"],
  [/^McKinney Fee must be greater/i, "McKinney Fee is zero"],
  [/^Amount must be greater/i, "Amount is zero"],
  [/^Policy number is missing/i, "Policy number missing"],
  [/^Effective date is missing/i, "Effective date missing"],
  [/^Invoice date is missing/i, "Effective date missing"],
  [/^Customer name is missing/i, "Corporation Name missing"],
  [/^Item is missing/i, "Carrier missing"],
  [/^Job name is longer/i, "Line of Business too long"],
];

function shortReason(reason) {
  for (const [pattern, label] of SHORT_REASON) {
    if (pattern.test(reason)) return label;
  }
  return reason.split(",")[0].replace(/\.$/, "");
}

/** New Biz against Renewal, as one bar plus the numbers behind it. */
function renderMix() {
  const tally = new Map();
  for (const record of state.records) {
    const key = record.source_tab || "Unknown";
    tally.set(key, (tally.get(key) || 0) + 1);
  }
  const total = state.records.length || 1;
  const order = ["New Biz", "Renewal", ...[...tally.keys()].filter(
    (key) => key !== "New Biz" && key !== "Renewal"
  )];
  const parts = order.filter((key) => tally.has(key));

  $("mixBar").replaceChildren(
    ...parts.map((key, index) => {
      const slice = document.createElement("span");
      slice.className = `mix-slice tone-${index}`;
      slice.style.width = `${(tally.get(key) / total) * 100}%`;
      slice.title = `${key}: ${tally.get(key).toLocaleString()}`;
      return slice;
    })
  );

  $("mixLegend").replaceChildren(
    ...parts.map((key, index) => {
      const item = document.createElement("div");
      item.className = "legend-item";
      const dot = document.createElement("i");
      dot.className = `tone-${index}`;
      const name = document.createElement("span");
      name.textContent = key;
      const count = document.createElement("strong");
      const share = Math.round((tally.get(key) / total) * 100);
      count.textContent = `${tally.get(key).toLocaleString()} · ${share}%`;
      item.append(dot, name, count);
      return item;
    })
  );
}

/** How far through each kind of invoice we are, with a bar you can read. */
function renderInvoiceBars() {
  const live = Boolean(state.health && state.health.writes_enabled);
  const rows = INVOICE_KINDS.filter((kind) => kind.operation).map((kind) => {
    let due = 0;
    let made = 0;
    for (const record of state.records) {
      if (!kind.applies(record)) continue;
      // A kind marked as not needed is not owed, so it is not counted as due -
      // otherwise the bar could never reach the end.
      if (record[`${kind.key}_status`] === NOT_NEEDED) continue;
      due += 1;
      if (live && DONE.has(record[`${kind.key}_status`])) made += 1;
    }
    return { label: kind.label, made, due };
  });
  $("invoiceBars").replaceChildren(...rows.map(progressRow));
}

function progressRow({ label, made, due }) {
  const item = document.createElement("div");
  item.className = "bar-row";

  const head = document.createElement("div");
  head.className = "bar-head";
  const name = document.createElement("span");
  name.textContent = label;
  const value = document.createElement("strong");
  value.textContent = `${made.toLocaleString()} / ${due.toLocaleString()}`;
  head.append(name, value);

  const track = document.createElement("div");
  track.className = "bar-track";
  const fill = document.createElement("span");
  // A hair of width on anything above zero, so "1 of 2116" is still visible.
  const share = due ? (made / due) * 100 : 0;
  fill.style.width = made ? `${Math.max(share, 1.5)}%` : "0%";
  track.append(fill);

  item.append(head, track);
  return item;
}

/* Every reason a row is held back, counted across the sheet. This is the most
   actionable thing on the page: it names the one sheet column to fill in to
   unblock the most policies. */
function renderBlockers() {
  const counts = new Map();
  const bump = (reason) => {
    const label = shortReason(reason);
    counts.set(label, (counts.get(label) || 0) + 1);
  };
  for (const record of state.records) {
    const seen = new Set();
    for (const reason of record.review_reasons || []) seen.add(reason);
    for (const kind of INVOICE_KINDS) {
      // A policy with no fee is not "blocked" on its fees invoice - it simply
      // does not need one, and counting it would bury the real blockers.
      if (!kind.operation || !kind.applies(record)) continue;
      for (const reason of record[`${kind.key}_reasons`] || []) seen.add(reason);
    }
    for (const reason of seen) bump(reason);
  }
  const worst = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 5);
  const total = state.records.length || 1;

  if (!worst.length) {
    $("blockerList").replaceChildren(
      div("muted", "Nothing is blocked — every policy has what it needs.")
    );
    return;
  }
  $("blockerList").replaceChildren(
    ...worst.map(([label, count]) => progressRow({
      label,
      made: count,
      due: total,
    }))
  );
}

/* When the loaded policies take effect. A bound policy usually started some
   time ago, so this is not a to-do list - it says how current the sheet is,
   and surfaces the few that start in the days ahead. */
function renderUpcoming() {
  const today = new Date();
  const dated = state.records.filter((record) => record.invoice_date);
  const windows = [
    ["Starts later", shiftDays(today, 1), "9999-12-31"],
    ["Starts this week", shiftDays(today, -6), shiftDays(today, 0)],
    ["Last 30 days", shiftDays(today, -30), shiftDays(today, -7)],
    ["Earlier", "0000-01-01", shiftDays(today, -31)],
  ];
  const rows = windows.map(([label, start, end]) => ({
    label,
    made: dated.filter(
      (record) => record.invoice_date >= start && record.invoice_date <= end
    ).length,
    due: dated.length || 1,
  }));
  $("upcomingList").replaceChildren(...rows.map(progressRow));
  const undated = state.records.length - dated.length;
  $("upcomingNote").textContent = undated
    ? `By effective date. ${undated.toLocaleString()} policies have no date, so they are left out.`
    : "By effective date.";
}

function shiftDays(from, days) {
  const moved = new Date(from);
  moved.setDate(moved.getDate() + days);
  return toISODate(moved);
}

const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const COUNT_MS = 480;
const frames = new Map();

/** Tween a card's number to its new value so a filter change is visible. */
function countTo(id, target, format) {
  const element = $(id);
  if (!element) return;   // a kind with no card of its own is simply not shown
  const from = Number(element.dataset.value || 0);
  if (from === target) return;
  element.dataset.value = String(target);

  cancelAnimationFrame(frames.get(id));
  const card = element.closest(".mini, .tile");
  if (card) {
    card.classList.add("bump");
    setTimeout(() => card.classList.remove("bump"), 600);
  }

  if (REDUCED_MOTION) {
    element.textContent = format(target);
    return;
  }
  const started = performance.now();
  const step = (now) => {
    const progress = Math.min(1, (now - started) / COUNT_MS);
    const eased = 1 - Math.pow(1 - progress, 3); // ease-out cubic
    if (progress < 1) {
      element.textContent = format(from + (target - from) * eased);
      frames.set(id, requestAnimationFrame(step));
    } else {
      element.textContent = format(target);
      frames.delete(id);
    }
  };
  frames.set(id, requestAnimationFrame(step));
}

const whole = (value) => Math.round(value).toLocaleString();

/** Really in QuickBooks - a safe-mode "completed" was only ever simulated.
    Shared so the cards and the row badges can never disagree. */
function isInQuickBooks(record, live) {
  const simulated = !live && DONE.has(record.status);
  return record.in_quickbooks === true || (DONE.has(record.status) && !simulated);
}

/** Cards describe the rows currently passing the filters, not the whole sheet. */
function renderSheetCards(rows) {
  const health = state.health;
  const live = Boolean(health && health.writes_enabled);
  let ready = 0;
  let review = 0;
  // Many policies share one corporation, so count distinct names rather than
  // rows - otherwise "Customers" would just restate the policy count.
  const customers = new Set();
  const jobs = new Set();
  // Each kind gets its own card: a premium invoice and a fees invoice are not
  // interchangeable, and only the rows that need a kind count towards it.
  const invoices = new Map(
    INVOICE_KINDS.map((kind) => [kind.key, { made: 0, due: 0 }])
  );

  for (const record of rows) {
    if (record.status === "review" || record.status === "needs_review") review += 1;
    else if (!DONE.has(record.status)) ready += 1;

    if (isInQuickBooks(record, live)) {
      if (record.customer_name) customers.add(record.customer_name.toLowerCase());
      if (record.job_name) jobs.add((record.job_full_name || "").toLowerCase());
    }

    for (const kind of invoiceKindsFor(record)) {
      if (!kind.operation) continue;
      if (record[`${kind.key}_status`] === NOT_NEEDED) continue;   // not owed
      const tally = invoices.get(kind.key);
      tally.due += 1;
      if (live && DONE.has(record[`${kind.key}_status`])) tally.made += 1;
    }
  }

  countTo("cardRows", rows.length, whole);
  countTo("cardReadyN", ready, whole);
  countTo("cardReviewN", review, whole);
  countTo("cardCustomers", customers.size, whole);
  countTo("cardJobs", jobs.size, whole);
  // These two are separate counts, not a ratio - unlike the invoice cards,
  // where the slash really does mean "out of". Spell it out so the two kinds
  // of card can never be read the same way.
  $("cardCustomersSub").textContent =
    `${customers.size} customer${customers.size === 1 ? "" : "s"} · ` +
    `${jobs.size} job${jobs.size === 1 ? "" : "s"} created`;
  for (const [key, tally] of invoices) {
    const card = key.charAt(0).toUpperCase() + key.slice(1);
    countTo(`card${card}Made`, tally.made, whole);
    countTo(`card${card}Due`, tally.due, whole);
  }

  const filtered = rows.length !== state.records.length;
  $("cardRowsSub").textContent =
    health && health.loading
      ? "loading…"
      : filtered
        ? `filtered from ${state.records.length.toLocaleString()}`
        : "from the Google Sheet";
}

function dateWindow() {
  const range = state.dateRange;
  if (range === "all") return null;
  if (range === "custom") {
    if (!state.dateFrom && !state.dateTo) return null;
    return { from: state.dateFrom || "0000-01-01", to: state.dateTo || "9999-12-31" };
  }
  const today = new Date();
  if (range === "ytd") {
    return { from: `${today.getFullYear()}-01-01`, to: "9999-12-31" };
  }
  const days = Number(range);
  if (!Number.isFinite(days)) return null;
  const start = new Date(today);
  start.setDate(start.getDate() - days);
  return { from: toISODate(start), to: "9999-12-31" };
}

/* ═══ date fields ═════════════════════════════════════════════════════════
   A typed date and a picked date should be equally easy. The browser's own
   date input refuses anything but its segmented mm/dd/yyyy boxes and brings a
   calendar that cannot be styled, so both halves are ours: a plain text box
   that accepts what people actually type, and a calendar that matches the
   rest of the app. Values are held as ISO internally and shown as M/D/YYYY. */

const MONTHS = ["January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December"];
const WEEKDAYS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];

/** Read a typed date. Accepts 6/1/2026, 06/01/2026, 2026-06-01 and 1 Jun 2026. */
function parseTypedDate(text) {
  const value = String(text || "").trim();
  if (!value) return "";

  const iso = value.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
  if (iso) return buildISO(+iso[1], +iso[2], +iso[3]);

  const slashed = value.match(/^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})$/);
  if (slashed) {
    let year = +slashed[3];
    if (year < 100) year += year < 70 ? 2000 : 1900;
    return buildISO(year, +slashed[1], +slashed[2]);   // month first, as typed here
  }

  const worded = value.match(/^(\d{1,2})\s+([A-Za-z]{3,})\s+(\d{4})$/);
  if (worded) {
    const month = MONTHS.findIndex((name) =>
      name.toLowerCase().startsWith(worded[2].toLowerCase().slice(0, 3))
    );
    if (month >= 0) return buildISO(+worded[3], month + 1, +worded[1]);
  }

  // Straight off the number pad, no separators: 04152026, or 041526.
  const digits = value.match(/^(\d{6}|\d{8})$/);
  if (digits) {
    const raw = digits[1];
    let year = +raw.slice(4);
    if (raw.length === 6) year += year < 70 ? 2000 : 1900;
    return buildISO(year, +raw.slice(0, 2), +raw.slice(2, 4));
  }
  return "";
}

/** Slashes typed in as you go: 0415 shows as 04/15. */
function autoSlash(value) {
  const digits = value.replace(/\D/g, "").slice(0, 8);
  if (digits.length <= 2) return digits;
  if (digits.length <= 4) return `${digits.slice(0, 2)}/${digits.slice(2)}`;
  return `${digits.slice(0, 2)}/${digits.slice(2, 4)}/${digits.slice(4)}`;
}

/** Only a date that really exists: 31 Feb comes back empty, not 3 March. */
function buildISO(year, month, day) {
  if (month < 1 || month > 12 || day < 1 || day > 31) return "";
  const made = new Date(year, month - 1, day);
  if (made.getFullYear() !== year || made.getMonth() !== month - 1 || made.getDate() !== day) {
    return "";
  }
  return toISODate(made);
}

function displayDate(iso) {
  if (!iso) return "";
  const [year, month, day] = iso.split("-");
  return `${month}/${day}/${year}`;   // already zero-padded in ISO
}

/** Wire one text box plus its calendar button. onPick gets an ISO date or "". */
function setupDateField(inputId, onPick) {
  const input = $(inputId);
  const panel = $(`${inputId}Cal`);
  const opener = $(`${inputId}Open`);
  let viewing = null;   // which month the calendar is showing

  const commit = (iso) => {
    input.value = displayDate(iso);
    input.classList.remove("bad");
    onPick(iso);
  };

  const close = () => {
    panel.hidden = true;
    opener.setAttribute("aria-expanded", "false");
  };

  const open = () => {
    const current = parseTypedDate(input.value);
    viewing = current ? new Date(`${current}T00:00:00`) : new Date();
    // Only one calendar at a time.
    for (const other of document.querySelectorAll(".cal")) other.hidden = true;
    panel.hidden = false;
    opener.setAttribute("aria-expanded", "true");
    draw();
  };

  const draw = () => {
    drawCalendar(panel, viewing, parseTypedDate(input.value), {
      onPick: (iso) => { commit(iso); close(); },
      onMove: (months) => {
        viewing = new Date(viewing.getFullYear(), viewing.getMonth() + months, 1);
        draw();
      },
      onSet: (pickedYear, pickedMonth) => {
        viewing = new Date(pickedYear, pickedMonth, 1);
        draw();
      },
      onClear: () => { commit(""); close(); },
      onToday: () => { commit(toISODate(new Date())); close(); },
    });
  };

  input.addEventListener("input", () => {
    // Someone typing straight off the number pad gets the slashes for free,
    // but only while the caret is at the end - otherwise fixing a middle
    // digit would have the text rearrange itself under the cursor.
    const atEnd = input.selectionStart === input.value.length;
    if (atEnd && /^[\d/]*$/.test(input.value)) {
      const tidied = autoSlash(input.value);
      if (tidied !== input.value) {
        input.value = tidied;
        input.setSelectionRange(tidied.length, tidied.length);
      }
    }

    const iso = parseTypedDate(input.value);
    // Half-typed dates are not wrong yet, so only a finished one is judged.
    input.classList.toggle("bad", Boolean(input.value.trim()) && !iso);
    if (iso) {
      onPick(iso);
      if (!panel.hidden) { viewing = new Date(`${iso}T00:00:00`); draw(); }
    } else if (!input.value.trim()) {
      onPick("");
    }
  });

  // Tidy what was typed once they move on: 6/1/26 becomes 6/1/2026.
  input.addEventListener("blur", () => {
    const iso = parseTypedDate(input.value);
    if (iso) commit(iso);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { input.blur(); close(); }
    if (event.key === "Escape") close();
  });

  opener.addEventListener("click", () => (panel.hidden ? open() : close()));
  panel.addEventListener("click", (event) => event.stopPropagation());
  document.addEventListener("click", (event) => {
    if (!panel.hidden && !panel.parentElement.contains(event.target)) close();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") close();
  });

  return { set: (iso) => { input.value = displayDate(iso); } };
}

function drawCalendar(panel, viewing, selectedIso, handlers) {
  const year = viewing.getFullYear();
  const month = viewing.getMonth();
  const todayIso = toISODate(new Date());

  const head = div("cal-head", "");
  const back = calButton("‹", "Previous month", () => handlers.onMove(-1));
  const next = calButton("›", "Next month", () => handlers.onMove(1));

  // Month and year are choosable, so a date years away is two clicks rather
  // than dozens of taps on the arrows.
  const monthPick = document.createElement("select");
  monthPick.className = "cal-pick";
  monthPick.setAttribute("aria-label", "Month");
  for (const [index, name] of MONTHS.entries()) {
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = name;
    option.selected = index === month;
    monthPick.append(option);
  }
  monthPick.addEventListener("change", () => handlers.onSet(year, +monthPick.value));

  const yearPick = document.createElement("select");
  yearPick.className = "cal-pick year";
  yearPick.setAttribute("aria-label", "Year");
  const thisYear = new Date().getFullYear();
  const years = new Set([year]);
  for (let step = thisYear - 12; step <= thisYear + 6; step += 1) years.add(step);
  for (const value of [...years].sort((a, b) => a - b)) {
    const option = document.createElement("option");
    option.value = String(value);
    option.textContent = String(value);
    option.selected = value === year;
    yearPick.append(option);
  }
  yearPick.addEventListener("change", () => handlers.onSet(+yearPick.value, month));

  const pickers = div("cal-pickers", "");
  pickers.append(monthPick, yearPick);
  head.append(back, pickers, next);

  const names = div("cal-week", "");
  for (const name of WEEKDAYS) names.append(div("cal-name", name));

  const grid = div("cal-grid", "");
  const first = new Date(year, month, 1);
  const start = new Date(year, month, 1 - first.getDay());
  for (let index = 0; index < 42; index += 1) {
    const day = new Date(start.getFullYear(), start.getMonth(), start.getDate() + index);
    const iso = toISODate(day);
    const cell = document.createElement("button");
    cell.type = "button";
    cell.className = "cal-day";
    cell.textContent = String(day.getDate());
    if (day.getMonth() !== month) cell.classList.add("outside");
    if (iso === todayIso) cell.classList.add("today");
    if (iso === selectedIso) cell.classList.add("picked");
    cell.addEventListener("click", () => handlers.onPick(iso));
    grid.append(cell);
  }

  const foot = div("cal-foot", "");
  foot.append(
    calLink("Clear", handlers.onClear),
    calLink("Today", handlers.onToday)
  );
  panel.replaceChildren(head, names, grid, foot);
}

function calButton(glyph, label, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "cal-nav";
  button.textContent = glyph;
  button.setAttribute("aria-label", label);
  button.addEventListener("click", onClick);
  return button;
}

function calLink(text, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "linky";
  button.textContent = text;
  button.addEventListener("click", onClick);
  return button;
}

function toISODate(value) {
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${value.getFullYear()}-${month}-${day}`;
}

/* ═══ letting a finished row be read before it goes ═══════════════════════
   On a "Pending" filter a row stops matching the moment it succeeds, so it
   used to vanish in the same frame its result arrived - the one thing the
   person was waiting to see. Each finished row is held in place for a beat
   showing Created, then fades out on its own. */

const LINGER_MS = 650;      // long enough to read "Created", no longer
const FADE_MS = 340;

function holdThenDrop(sourceIds) {
  // Ask the filters once, with the linger list set aside so it cannot answer
  // "still visible" about the very rows being tested.
  const held = state.lingering;
  state.lingering = new Map();
  const visible = new Set(visibleRecords().map((record) => record.source_id));
  state.lingering = held;

  const known = new Set(state.records.map((record) => record.source_id));
  // Only rows the filters would now hide; anything still matching just stays.
  const leaving = sourceIds.filter((id) => known.has(id) && !visible.has(id));
  if (!leaving.length) return;

  for (const id of leaving) state.lingering.set(id, "hold");
  renderTable();

  setTimeout(() => {
    // The fade is put straight onto the rows already on screen. Re-rendering
    // to start it would rebuild every row in the table and restart their
    // animations underneath this one, which is what made it stutter.
    for (const id of leaving) {
      state.lingering.set(id, "leaving");
      const row = document.querySelector(`.sheet-grid tbody tr[data-id="${CSS.escape(id)}"]`);
      if (row) row.classList.add("row-leaving");
    }
    setTimeout(() => {
      for (const id of leaving) state.lingering.delete(id);
      renderTable();                     // the list closes up, once
    }, REDUCED_MOTION ? 0 : FADE_MS);
  }, LINGER_MS);
}

/* "Already there" and "Created" both mean the work is done, so the filters
   treat them as one thing rather than splitting a distinction nobody acts on. */
function filterStatus(value) {
  if (value === "needs_review") return "review";
  if (value === "already_exists") return "completed";
  return value || "ready";
}

function visibleRecords() {
  const needle = state.search.trim().toLowerCase();
  const span = dateWindow();
  return state.records.filter((record) => {
    // A row that has just succeeded is held for a moment even though it no
    // longer matches - so the result can be read before it goes. See
    // holdThenDrop().
    if (state.lingering.has(record.source_id)) return true;
    if (state.status !== "all" && filterStatus(record.status) !== state.status) return false;
    // Two filters working together: which kind of invoice to look at, and what
    // state it must be in. "Premium only" + "Created" answers the question the
    // office actually asks - which policies have had their premium raised.
    const kinds = invoiceKindsFor(record).filter(
      (kind) => kind.operation
        && (state.invoiceKind === "all" || kind.key === state.invoiceKind)
    );
    if (state.invoiceKind !== "all" && !kinds.length) return false;
    if (
      state.invoiceStatus !== "all" &&
      !kinds.some((kind) => filterStatus(record[`${kind.key}_status`]) === state.invoiceStatus)
    ) {
      return false;
    }
    if (state.source !== "all" && record.source_tab !== state.source) return false;
    if (span) {
      // Rows with no effective date cannot satisfy a date window.
      if (!record.invoice_date) return false;
      if (record.invoice_date < span.from || record.invoice_date > span.to) return false;
    }
    if (!needle) return true;
    return [record.customer_name, record.job_name, record.invoice_number, record.item, record.description]
      .join(" ")
      .toLowerCase()
      .includes(needle);
  });
}

/* When the filters change what is listed, rows that survive slide from where
   they were to where they now belong, and rows that are new rise in behind
   them. This is the FLIP technique: measure First, swap, measure Last, Invert
   with a transform, then Play by removing it.

   The inline transform is always cleared once it lands - a transform left on a
   <tr> creates a containing block and would break the sticky first column. */
let lastRowSignature = null;
const STAGGER_ROWS = 10;   // beyond this the delay stops growing
const STAGGER_STEP = 30;   // ms between consecutive rows - the cascade
// Only rows near the top are worth animating: the rest are below the fold,
// and 300 simultaneous transforms would stutter for no visible gain.
const ANIMATE_ROWS = 28;
// Remember where EVERY rendered row was, not just the top few: after a filter
// change most survivors come from far down the old list, and without their old
// position they fall back to a plain fade instead of visibly travelling.
const MEASURE_ROWS = RENDER_CAP;
// A row promoted from #200 to #2 would otherwise fly thousands of pixels in a
// third of a second, which just reads as a flicker. Cap the distance so it
// slides up from just off-screen instead.
const MAX_SHIFT = 340;
const SLIDE_MS = 360;

function playRowTransition(built, before) {
  // LAST: read every new position first, in one pass, so the writes below
  // don't force a re-layout per row.
  const moves = [];
  for (let index = 0; index < built.length && index < ANIMATE_ROWS; index += 1) {
    const tr = built[index];
    const previous = before.get(tr.dataset.id);
    if (previous === undefined) {
      moves.push({ tr, index, shift: null });      // brand new row
    } else {
      const travelled = previous - tr.getBoundingClientRect().top;
      const shift = Math.max(-MAX_SHIFT, Math.min(MAX_SHIFT, travelled));
      if (Math.abs(shift) > 1) moves.push({ tr, index, shift });
    }
  }

  // INVERT: put the survivors back where they came from.
  for (const { tr, index, shift } of moves) {
    if (shift === null) {
      tr.classList.add("row-enter");
      tr.style.animationDelay = `${Math.min(index, STAGGER_ROWS) * STAGGER_STEP}ms`;
    } else {
      tr.style.transition = "none";
      tr.style.transform = `translateY(${shift}px)`;
    }
  }

  // PLAY: next frame, let them travel - each one a beat after the last, so
  // you can follow rows arriving into 1st, 2nd, 3rd place one by one instead
  // of the whole table shifting at once.
  requestAnimationFrame(() => {
    for (const { tr, index, shift } of moves) {
      if (shift === null) continue;
      const delay = Math.min(index, STAGGER_ROWS) * STAGGER_STEP;
      tr.style.transition = `transform ${SLIDE_MS}ms cubic-bezier(.21, .68, .32, 1) ${delay}ms`;
      tr.style.transform = "";
      // Strip the inline styles once it arrives, so no transform lingers.
      tr.addEventListener(
        "transitionend",
        () => {
          tr.style.transition = "";
          tr.style.transform = "";
        },
        { once: true }
      );
    }
  });
}

function renderTable() {
  renderHead();
  const rows = visibleRecords();
  const shown = rows.slice(0, RENDER_CAP);
  renderSheetCards(rows);
  $("emptyState").hidden = rows.length > 0;
  renderEmptyState();

  const signature = shown.map((record) => record.source_id).join("|");
  const listChanged = signature !== lastRowSignature;
  lastRowSignature = signature;

  const body = $("rows");
  const animate = listChanged && !REDUCED_MOTION;

  // FIRST: where every row sits right now, before the list is swapped.
  const before = new Map();
  if (animate) {
    let seen = 0;
    for (const tr of body.children) {
      if (seen >= MEASURE_ROWS) break;
      before.set(tr.dataset.id, tr.getBoundingClientRect().top);
      seen += 1;
    }
  }

  const built = shown.map(buildRow);
  body.replaceChildren(...built);
  if (animate) playRowTransition(built, before);

  const cap = $("rowCap");
  cap.hidden = rows.length <= RENDER_CAP;
  cap.textContent = `Showing the first ${RENDER_CAP} of ${rows.length} matching policies — narrow the filters to see the rest. "Select all" still covers all ${rows.length}.`;

  $("filterCount").textContent =
    rows.length === state.records.length
      ? `${rows.length} policies`
      : `${rows.length} of ${state.records.length} policies`;

  const picked = rows.filter((record) => state.selected.has(record.source_id)).length;
  const all = $("selectAll");
  // Ticked once the cap is reached, since that is as many as may be sent.
  all.checked = picked > 0 && (picked >= MAX_SELECTION || picked === rows.length);
  all.indeterminate = picked > 0 && !all.checked;
  all.disabled = rows.length === 0;
  all.title = `Select the first ${MAX_SELECTION} shown`;
}

/* ═══ row editing ═════════════════════════════════════════════════════════ */

const ICON = {
  pencil: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>',
  save: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>',
  cancel: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>',
};

function iconButton(kind, label, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `row-btn ${kind}`;
  button.innerHTML = ICON[kind];
  button.title = label;
  button.setAttribute("aria-label", label);
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    onClick();
  });
  return button;
}

/** Raw value for the editor — not the formatted one shown in the cell. */
function rawValue(record, field) {
  const column = SHEET_COLUMNS.find((entry) => entry.key === field);
  if (!column) return "";
  const value = column.from ? record[field] : (record.extra || {})[field];
  return value === undefined || value === null ? "" : String(value);
}

/** The pencil opens this dialog with every column of the row laid out as a form. */
function openEditor(sourceId) {
  const record = state.records.find((entry) => entry.source_id === sourceId);
  if (!record) return;
  state.editing = sourceId;

  const status = record.status === "needs_review" ? "review" : record.status;
  $("editTitle").textContent = record.customer_name || `Row ${record.row_number}`;
  $("editSubtitle").textContent = [
    record.source_tab,
    record.job_name,
    record.invoice_number && `Policy ${record.invoice_number}`,
  ]
    .filter(Boolean)
    .join("  ·  ");

  const reasons = record.review_reasons || [];
  const warning = $("editWarning");
  const detail = record.error_message && status === "failed"
    ? record.error_message
    : reasons.join(" ");
  warning.hidden = !detail;
  warning.textContent = detail;

  const edited = new Set(record.edited || []);
  $("editFields").replaceChildren(
    ...SHEET_COLUMNS.map((column) => {
      const field = document.createElement("label");
      field.className = column.wide || column.sticky ? "edit-field wide" : "edit-field";
      field.dataset.field = column.key;

      const label = document.createElement("span");
      label.textContent = column.label;
      if (edited.has(column.key)) {
        const mark = document.createElement("em");
        mark.className = "edited-mark";
        mark.textContent = "edited";
        label.append(mark);
      }

      const input = document.createElement("input");
      input.className = "cell-input";
      input.dataset.field = column.key;
      input.value = rawValue(record, column.key);
      if (column.type === "date") input.type = "date";
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          saveRow(record);
        }
      });

      field.append(label, input);
      return field;
    })
  );

  wireCommissionMath();

  const dialog = $("editDialog");
  dialog.showModal();
  const first = dialog.querySelector("input.cell-input");
  if (first) {
    first.focus();
    first.select();
  }
}

/* Commision = Pure Premium x Commission %. Recalculated live while you change
   either input, until you type your own amount — then yours wins. */

function toNumber(text) {
  const cleaned = String(text).replace(/[^0-9.\-]/g, "");
  if (!cleaned || cleaned === "-" || cleaned === ".") return null;
  const value = Number(cleaned);
  return Number.isFinite(value) ? value : null;
}

function commissionAmount(premiumText, rateText) {
  const premium = toNumber(premiumText);
  let rate = toNumber(rateText);
  if (premium === null || rate === null) return null;
  // The sheet stores 0.175 for 17.5%, but someone may type "17.5" instead.
  if (Math.abs(rate) > 1) rate /= 100;
  return premium * rate;
}

function editorInput(field) {
  return $("editFields").querySelector(`input[data-field="${field}"]`);
}

function wireCommissionMath() {
  const premium = editorInput("premium");
  const rate = editorInput("commission_rate");
  const amount = editorInput("amount");
  if (!premium || !rate || !amount) return;

  const holder = $("editFields").querySelector('.edit-field[data-field="amount"]');
  const hint = document.createElement("p");
  hint.className = "field-hint";
  const text = document.createElement("span");
  const relink = document.createElement("button");
  relink.type = "button";
  relink.className = "linky";
  relink.textContent = "recalculate";
  relink.hidden = true;
  hint.append(text, relink);
  holder.append(hint);

  let manual = false;

  const recalc = (force = false) => {
    if (manual && !force) return;
    const value = commissionAmount(premium.value, rate.value);
    if (value === null) {
      text.textContent = "Enter a premium and rate to calculate.";
      return;
    }
    amount.value = value.toFixed(2);
    amount.classList.add("flash");
    setTimeout(() => amount.classList.remove("flash"), 500);
    manual = false;
    relink.hidden = true;
    text.textContent = "Auto: Pure Premium × Commission %";
  };

  const setManual = () => {
    manual = true;
    relink.hidden = false;
    text.textContent = "Your own amount — ";
  };

  text.textContent = "Change the premium or rate to recalculate";
  premium.addEventListener("input", () => recalc());
  rate.addEventListener("input", () => recalc());
  amount.addEventListener("input", setManual);
  relink.addEventListener("click", () => recalc(true));
}

function closeEditor() {
  state.editing = null;
  const dialog = $("editDialog");
  if (dialog.open) dialog.close();
}

/* ═══ the invoice picker ══════════════════════════════════════════════════ */

/** The Invoice button opens this: which invoice does this policy need? */
/** The picker, for one row or for everything selected. */
function openInvoicePicker(sourceIds) {
  const ids = Array.isArray(sourceIds) ? sourceIds : [sourceIds];
  const records = ids
    .map((id) => state.records.find((entry) => entry.source_id === id))
    .filter(Boolean);
  if (!records.length) return;

  // A kind is offered when at least one chosen row can raise it - so Fees is
  // hidden when nobody has a fee, and offered when even one row does.
  const kinds = INVOICE_KINDS.filter((kind) => records.some((record) => kind.applies(record)));
  // Start with whatever is still outstanding, so the common case is one click.
  const runnable = kinds.filter((kind) => kind.operation);
  const outstanding = runnable.filter((kind) =>
    invoiceTargets(kind, records).some((record) => invoiceOutstanding(record, kind))
  );
  const preselect = outstanding.length ? outstanding : runnable;
  state.invoicing = {
    sourceIds: records.map((record) => record.source_id),
    kinds: new Set(preselect.map((kind) => kind.key)),
  };

  const many = records.length > 1;
  $("invoiceTitle").textContent = outstanding.length ? "Create invoice" : "Update invoice";
  $("invoiceSubtitle").textContent = many
    ? `${records.length} policies selected`
    : records[0].job_full_name || records[0].customer_name;

  $("invoiceChoices").replaceChildren(
    ...kinds.map((kind) => invoiceChoice(kind, records))
  );
  renderInvoiceChoice();
  $("invoiceDialog").showModal();
}

/** The chosen rows this kind actually applies to - fees skip the fee-less. */
function invoiceTargets(kind, records) {
  return records.filter((record) => kind.applies(record));
}

/** The records currently in the picker. */
function invoicingRecords() {
  const ids = (state.invoicing || {}).sourceIds || [];
  return ids
    .map((id) => state.records.find((entry) => entry.source_id === id))
    .filter(Boolean);
}

/** What stops the ticked invoices from being raised, without repeating itself. */
function invoiceBlockers(records, kinds) {
  const seen = new Set();
  for (const kind of kinds) {
    for (const record of invoiceTargets(kind, records)) {
      for (const reason of record[`${kind.key}_reasons`] || []) seen.add(reason);
    }
  }
  return [...seen];
}

function invoiceChoice(kind, records) {
  // Not every policy earns all three invoices. A kind can be marked as not
  // needed, which takes it out of the counts and the filters for good - rather
  // than sitting as "Pending" forever and being mistaken for work outstanding.
  const targets = invoiceTargets(kind, records);
  const skipped = targets.length > 0
    && targets.every((record) => record[`${kind.key}_status`] === "not_needed");

  const label = document.createElement("label");
  label.className = "invoice-choice" + (skipped ? " is-skipped" : "");
  label.dataset.kind = kind.key;

  const input = document.createElement("input");
  input.type = "checkbox";
  input.value = kind.key;
  input.checked = !skipped && Boolean((state.invoicing || {}).kinds?.has(kind.key));
  input.disabled = !kind.operation || skipped;
  input.addEventListener("change", () => {
    const chosen = (state.invoicing || {}).kinds;
    if (!chosen) return;
    if (input.checked) chosen.add(kind.key);
    else chosen.delete(kind.key);
    renderInvoiceChoice();
  });

  const text = document.createElement("div");
  const title = document.createElement("strong");
  title.textContent = kind.label;
  if (skipped) title.append(tag("Not needed", "not_needed"));
  const note = document.createElement("span");
  note.className = "muted";
  note.textContent = skipped
    ? "Marked as not needed, so it will not be created."
    : (kind.operation ? invoiceChoiceNote(kind, records) : kind.note);
  text.append(title, note);

  label.append(input, text);
  // Premium only. Commission and fees already answer "is this wanted?" from the
  // row itself - no commission or no fee means no invoice - so marking them by
  // hand would be a second, competing answer to a question already settled.
  if (kind.key === "premium" && kind.operation) {
    label.append(notNeededButton(kind, targets, skipped));
  }
  return label;
}

/** The small link that marks a kind as not needed, or brings it back. */
function notNeededButton(kind, targets, skipped) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "linky skip-link";
  button.textContent = skipped ? "Need it after all" : "Not needed";
  button.title = skipped
    ? `Put ${kind.label.toLowerCase()} back on the list for these policies`
    : `Never create ${kind.label.toLowerCase()} for these policies`;
  // Inside a <label>, so a click would otherwise toggle the checkbox too.
  button.addEventListener("click", async (event) => {
    event.preventDefault();
    event.stopPropagation();
    const created = targets.filter((record) => DONE.has(record[`${kind.key}_status`]));
    if (!skipped && created.length) {
      await ask({
        title: "Already in QuickBooks",
        sub: `${created.length} of these already has this invoice, so it cannot be `
           + "marked as not needed. Delete it in QuickBooks first.",
        ok: "Close",
        tone: "danger",
      });
      return;
    }
    button.disabled = true;
    try {
      for (const record of targets) {
        const payload = await api("/api/invoice-needed", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            source_id: record.source_id, kind: kind.key, needed: skipped,
          }),
        }, API_TIMEOUT_MS);
        Object.assign(record, payload.record || {});
      }
      if (!skipped) (state.invoicing || { kinds: new Set() }).kinds.delete(kind.key);
      redrawInvoiceChoices();
      renderTable();
      renderSheetCards(visibleRecords());
      toast(skipped
        ? `${kind.label} is back on the list.`
        : `${kind.label} marked as not needed.`);
    } catch (error) {
      await ask({
        title: "Could not change that",
        detail: error.message,
        ok: "Close",
        tone: "danger",
      });
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

/** Rebuild the choices in place, so a "not needed" change shows immediately. */
function redrawInvoiceChoices() {
  const records = invoicingRecords();
  const kinds = INVOICE_KINDS.filter((kind) => records.some((record) => kind.applies(record)));
  $("invoiceChoices").replaceChildren(...kinds.map((kind) => invoiceChoice(kind, records)));
  renderInvoiceChoice();
}

/** One line saying what this choice will do to the rows in hand. */
function invoiceChoiceNote(kind, records) {
  const targets = invoiceTargets(kind, records);
  if (records.length === 1) {
    const prefix = DONE.has(records[0][`${kind.key}_status`])
      ? "Already created — will be updated. "
      : "";
    return `${prefix}${kind.note} Amount: ${money(kind.amount(records[0]))}.`;
  }
  // Said plainly, because it is the whole point of the fees rule: a kind only
  // reaches the rows it applies to, never the rest of the selection.
  const total = targets.reduce((sum, record) => sum + moneyValue(kind.amount(record)), 0);
  const scope =
    targets.length === records.length
      ? `All ${records.length} policies`
      : `${targets.length} of ${records.length} policies`;
  return `${scope} · ${money(total)} in total. ${kind.note}`;
}

/** Keep the footer honest about what the ticked choices will actually do. */
function renderInvoiceChoice() {
  const choice = state.invoicing || { kinds: new Set() };
  const records = invoicingRecords();
  const many = records.length > 1;
  const runnable = [...choice.kinds]
    .map((key) => INVOICE_KINDS.find((kind) => kind.key === key))
    .filter((kind) => kind && kind.operation);
  const blockers = invoiceBlockers(records, runnable);
  // How many invoices this will actually try to raise, across all ticked kinds.
  const sends = runnable.reduce(
    (total, kind) => total + invoiceTargets(kind, records).length,
    0
  );
  // In bulk, rows that are not ready are held back rather than blocking the
  // whole send - one missing Class should not stop the other nine policies.
  const readySends = runnable.reduce(
    (total, kind) =>
      total +
      invoiceTargets(kind, records).filter(
        (record) => !(record[`${kind.key}_reasons`] || []).length
      ).length,
    0
  );

  // Only the ticked kinds' problems are shown, so unticking one clears its own.
  const warning = $("invoiceWarning");
  warning.hidden = blockers.length === 0;
  warning.textContent = blockers.length
    ? many
      ? `${sends - readySends} of ${sends} will be held back: ${blockers.join(" ")}`
      : `This policy is not ready yet: ${blockers.join(" ")}`
    : "";

  const create = $("invoiceCreate");
  create.disabled = readySends === 0;
  create.textContent = readySends > 1 ? `Create ${readySends} invoices` : "Create invoice";
  $("invoiceNote").textContent = !choice.kinds.size
    ? "Tick at least one invoice to create."
    : runnable.length === 0
      ? "That invoice is not set up yet."
      : readySends === 0
        ? "Fix the missing details above first, then try again."
        : "Raised on each policy's job in QuickBooks.";

  for (const label of document.querySelectorAll(".invoice-choice")) {
    label.classList.toggle("selected", choice.kinds.has(label.dataset.kind));
  }
}

function closeInvoicePicker() {
  state.invoicing = null;
  const dialog = $("invoiceDialog");
  if (dialog.open) dialog.close();
}

async function createInvoice() {
  const choice = state.invoicing;
  if (!choice) return;
  const kinds = INVOICE_KINDS.filter(
    (kind) => choice.kinds.has(kind.key) && kind.operation
  );
  const records = invoicingRecords();
  if (!kinds.length || !records.length) return;
  if (state.health && state.health.writes_enabled && !(await quickBooksIsReady())) return;

  // Each kind goes only to the rows it applies to, so ticking Fees on a mixed
  // selection bills the policies that have a fee and leaves the rest alone.
  const batches = kinds
    .map((kind) => ({
      kind,
      ids: invoiceTargets(kind, records).map((record) => record.source_id),
    }))
    .filter((batch) => batch.ids.length);
  closeInvoicePicker();
  if (!batches.length) return;

  if (records.length === 1) {
    const sourceId = records[0].source_id;
    // One at a time: QuickBooks tolerates a single session, and actOnPolicy
    // already queues. The button is re-found each pass because every send
    // re-renders the row.
    for (const batch of batches) {
      const current = state.records.find((entry) => entry.source_id === sourceId);
      const busy =
        current && DONE.has(current[`${batch.kind.key}_status`]) ? "Updating…" : "Creating…";
      await actOnPolicy(sourceId, batch.kind.operation, rowInvoiceButton(sourceId), busy);
    }
    return;
  }

  for (const batch of batches) {
    await processBatch(batch.kind.operation, batch.ids, "invoiceButton", {
      noun: `${batch.ids.length} ${batch.kind.label.toLowerCase()}${
        batch.ids.length === 1 ? "" : "s"
      }`,
      label: "Send Invoice",
    });
  }
}

async function saveRow(record) {
  const changed = {};
  for (const input of $("editFields").querySelectorAll("input.cell-input")) {
    const field = input.dataset.field;
    if (input.value !== rawValue(record, field)) changed[field] = input.value;
  }
  if (!Object.keys(changed).length) {
    closeEditor();
    return;
  }

  const button = $("editSave");
  button.disabled = true;
  button.textContent = "Saving…";
  try {
    const payload = await api("/api/edit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_id: record.source_id, values: changed }),
    });
    const index = state.records.findIndex((entry) => entry.source_id === record.source_id);
    if (index >= 0) state.records[index] = payload.record;
    closeEditor();
    renderTable();
    renderSelection();
    const count = Object.keys(changed).length;
    toast(`Saved ${count} change${count === 1 ? "" : "s"}.`);
  } catch (error) {
    toast(error.message, true);   // leave the dialog open so nothing is lost
  } finally {
    button.disabled = false;
    button.textContent = "Save changes";
  }
}

function renderEmptyState() {
  const health = state.health;
  const title = $("emptyTitle");
  const text = $("emptyText");
  if (health && health.loading) {
    title.textContent = "Loading the Google Sheet…";
    text.textContent = "This takes a few seconds the first time.";
  } else if (health && health.load_error) {
    title.textContent = "The Google Sheet could not be read";
    text.textContent = health.load_error;
  } else if (state.records.length) {
    title.textContent = "No policies match";
    text.textContent = "Clear the search box or change the status filter.";
  } else {
    title.textContent = "No policies here";
    text.textContent = "Nothing here carries a verified Corporation Name.";
  }
}

let renderedOrder = null;

function renderHead() {
  const head = $("sheetHead");
  const current = columnOrder();
  const signature = current.join("|");
  if (head.children.length && renderedOrder === signature) return;
  renderedOrder = signature;

  const check = document.createElement("th");
  check.className = "check";
  const all = document.createElement("input");
  all.type = "checkbox";
  all.id = "selectAll";
  all.setAttribute("aria-label", "Select all shown policies");
  all.addEventListener("change", (event) => {
    const shown = visibleRecords();
    if (!event.target.checked) {
      for (const record of shown) state.selected.delete(record.source_id);
    } else {
      // Select-all means "as many as may be sent at once", not everything -
      // the limit is there so one mistake cannot reach thousands of rows.
      state.selected.clear();
      for (const record of shown.slice(0, MAX_SELECTION)) {
        state.selected.add(record.source_id);
      }
      if (shown.length > MAX_SELECTION) {
        toast(
          `Selected the first ${MAX_SELECTION} of ${shown.length.toLocaleString()} — ` +
          `${MAX_SELECTION} is the most that can be sent at once.`
        );
      }
    }
    renderTable();
    renderSelection();
  });
  check.append(all);

  const headCell = (column, movable) => {
    const cell = document.createElement("th");
    cell.textContent = column.label;
    cell.className = headClasses(column);
    if (movable) makeDraggable(cell, column);
    return cell;
  };

  // Corporation Name leads the data, pinned next to the checkbox so both
  // survive sideways scrolling - and for the same reason it cannot be moved.
  // replaceChildren, not append: this reruns whenever the order changes.
  head.replaceChildren(
    check,
    headCell(SHEET_COLUMNS[0], false),
    ...current.map((key) => headCell(COLUMN_BY_KEY.get(key), true))
  );
}

/* ═══ column order ════════════════════════════════════════════════════════ */

/* Pointer events rather than HTML5 drag-and-drop: native DnD is unreliable on
   position:sticky headers inside a horizontally scrolling container, which is
   exactly what this table is. */
function makeDraggable(cell, column) {
  cell.dataset.key = column.key;
  cell.classList.add("movable");
  cell.title = `${column.label} — drag to move this column`;
  cell.addEventListener("pointerdown", (event) => {
    if (event.button === 0) startColumnDrag(event, cell, column);
  });
}

function clearDragMarks() {
  const marks = ["col-dragging", "col-drop-before", "col-drop-after"];
  for (const el of document.querySelectorAll(marks.map((m) => `.${m}`).join(", "))) {
    el.classList.remove(...marks);
  }
}

/** Add or remove a class across a whole column - its header and every cell. */
function markColumn(key, className, on) {
  if (!key) return;
  const selector = `#sheetHead th[data-key="${key}"], #rows td[data-key="${key}"]`;
  for (const el of document.querySelectorAll(selector)) {
    el.classList.toggle(className, on);
  }
}

function startColumnDrag(event, cell, column) {
  const startX = event.clientX;
  let dragging = false;
  let target = null;
  let dropClass = null;

  const onMove = (move) => {
    // Ignore a plain click: only treat it as a drag past a small threshold.
    if (!dragging) {
      if (Math.abs(move.clientX - startX) < 5) return;
      dragging = true;
      // Lift the entire column, not just the header, so it is obvious what
      // is being carried.
      markColumn(column.key, "col-dragging", true);
      document.body.classList.add("dragging-column");
    }
    move.preventDefault();

    const under = document.elementFromPoint(move.clientX, move.clientY);
    const over = under && under.closest("#sheetHead th.movable");
    const nextKey = over && over !== cell ? over.dataset.key : null;
    if (target && target.dataset.key !== nextKey) {
      markColumn(target.dataset.key, dropClass, false);
    }
    target = nextKey ? over : null;
    if (target) {
      // Mark the edge the column will actually land on: dragging rightwards it
      // goes after the target, leftwards it goes before it. A line always on
      // the left would point at the wrong gap half the time.
      const current = columnOrder();
      dropClass = current.indexOf(column.key) < current.indexOf(nextKey)
        ? "col-drop-after"
        : "col-drop-before";
      markColumn(nextKey, dropClass, true);
    }
  };

  const onUp = () => {
    document.removeEventListener("pointermove", onMove);
    document.removeEventListener("pointerup", onUp);
    document.removeEventListener("pointercancel", onUp);
    document.body.classList.remove("dragging-column");
    const dropped = dragging && target ? target.dataset.key : null;
    clearDragMarks();
    if (dropped) moveColumn(column.key, dropped);
  };

  document.addEventListener("pointermove", onMove);
  document.addEventListener("pointerup", onUp);
  document.addEventListener("pointercancel", onUp);
}

function moveColumn(fromKey, toKey) {
  if (!fromKey || fromKey === toKey) return;
  const current = columnOrder();
  const from = current.indexOf(fromKey);
  const to = current.indexOf(toKey);
  if (from < 0 || to < 0) return;

  const [moved] = current.splice(from, 1);
  // Look the target up again - removing the dragged column shifts everything
  // after it left by one. Dropping takes the target's slot: coming from the
  // left that means landing after it, from the right, before it.
  const target = current.indexOf(toKey);
  current.splice(from < to ? target + 1 : target, 0, moved);

  saveColumnOrder(current);
  renderTable();
}

function headClasses(column) {
  const classes = [];
  if (column.type === "money" || column.type === "percent") classes.push("money");
  if (column.wide) classes.push("wide");
  if (column.sticky) classes.push("sticky-col");
  if (column.fit) classes.push("fit");
  return classes.join(" ");
}

function bodyClasses(column) {
  const classes = headClasses(column);
  return column.mono ? `${classes} mono`.trim() : classes;
}

function cellText(record, column) {
  const raw = column.from ? record[column.key] : (record.extra || {})[column.key];
  const text = raw === undefined || raw === null ? "" : String(raw);
  if (!text) return "—";
  if (column.type === "money") {
    const number = Number(text);
    return Number.isFinite(number) ? money(number) : text;
  }
  if (column.type === "percent") {
    const number = Number(text);
    if (!Number.isFinite(number)) return text;
    // The sheet stores 0.175 for 17.5%.
    return `${(number * (Math.abs(number) <= 1 ? 100 : 1)).toFixed(2)}%`;
  }
  if (column.type === "date") return usDate(text);
  return text;
}

/* Dates are held as 2026-01-06 - the one spelling that sorts, compares and
   travels without ambiguity - but that is not how the office reads a date.
   Shown as 01/06/2026, the same way the sheet and QuickBooks show it. */
function usDate(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value).trim());
  if (!match) return value;
  const [, year, month, day] = match;
  return `${month}/${day}/${year}`;
}

function buildRow(record) {
  const status = record.status === "needs_review" ? "review" : record.status;

  const tr = document.createElement("tr");
  tr.dataset.id = record.source_id;
  // Finished, and on its way off a filter it no longer matches.
  const linger = state.lingering.get(record.source_id);
  if (linger === "hold") tr.classList.add("row-settled");
  else if (linger === "leaving") tr.classList.add("row-leaving");

  const check = document.createElement("td");
  check.className = "check";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = state.selected.has(record.source_id);
  // Selectable even once done - resending an already-created policy is safe
  // (the server just reports "already exists"), and selecting it is useful
  // for review or for a bulk Update after an edit.
  box.setAttribute("aria-label", `Select ${record.customer_name || "row " + record.row_number}`);
  box.addEventListener("change", () => {
    if (!box.checked) {
      state.selected.delete(record.source_id);
    } else if (state.selected.size >= MAX_SELECTION) {
      box.checked = false;   // refuse rather than silently exceed the limit
      toast(
        `${MAX_SELECTION} policies is the most that can be sent at once. ` +
        "Send these, then pick the next batch.",
        true
      );
      return;
    } else {
      state.selected.add(record.source_id);
    }
    renderSelection();
  });
  check.append(box);

  // The pencil sits in the row's corner and opens the edit dialog.
  check.append(iconButton("pencil", "Edit this policy", () => openEditor(record.source_id)));

  const live = Boolean(state.health && state.health.writes_enabled);
  const simulated = !live && DONE.has(record.status);
  // Known in QuickBooks because this app created it - that's the only signal
  // we have now that the explicit "Check QuickBooks" sync has been removed.
  const confirmedInQb = isInQuickBooks(record, live);
  const reasons = record.review_reasons || [];
  const edited = new Set(record.edited || []);

  // Mirrors WorkRecord.customer_reasons() server-side — a missing policy
  // number blocks Send too, not just invoicing, so it's never offered.
  const blocked = reasons.some((reason) =>
    reason.startsWith("Customer name") || reason.startsWith("Job name") || reason.startsWith("Policy number")
  );
  const detail = simulated
    ? "Safe mode is on, so this was only simulated — nothing was written to QuickBooks."
    : record.error_message && status === "failed"
      ? record.error_message
      // Same as the invoice badges: once the customer and job exist, the
      // reasons they could not be created are history, not a warning.
      : (DONE.has(status) ? "" : reasons.join(" "));

  const view = { status, simulated, confirmedInQb, reasons, edited, blocked, detail, live };

  const bodyCell = (column) => {
    const cell = td(cellText(record, column));
    cell.className = bodyClasses(column);
    cell.dataset.key = column.key;   // lets a drag highlight the whole column
    // Edited cells look like any other; the tooltip is the only hint.
    if (edited.has(column.key)) cell.title = "Edited here — not changed in the Google Sheet";
    return cell;
  };

  const cellFor = (key) => {
    const column = COLUMN_BY_KEY.get(key);
    if (!column) return td("");
    return column.render ? column.render(record, view) : bodyCell(column);
  };

  tr.append(check, bodyCell(SHEET_COLUMNS[0]), ...columnOrder().map(cellFor));
  return tr;
}

function renderStatus() {
  const health = state.health;
  if (!health) return;
  const rows = [
    ["Mode", health.mode === "quickbooks" ? "quickbooks (live)" : "dry_run (safe)", null],
    ["Writes to QuickBooks", health.writes_enabled ? "ON" : "OFF", health.writes_enabled ? "on" : "off"],
    ["Why", health.blocked_reason ? capitalise(health.blocked_reason) : "All safety switches are on.", null],
    ["Company", health.company.display_name, null],
    ["Company file", health.company.file_path || "(not set)", null],
    ["File found on disk", health.company.file_exists ? "Yes" : "No", health.company.file_exists ? "on" : "off"],
    ["QuickBooks SDK", health.sdk_registered ? "Installed" : "Not found", health.sdk_registered ? "on" : "off"],
    ["App name shown in QB", health.app_name, null],
    ["qbXML version", health.qbxml_version, null],
    ["Loaded file", health.source_name || "none", null],
    ["Watching the sheet", health.watching ? "Every minute" : "Off", health.watching ? "on" : "off"],
  ];
  $("statusList").replaceChildren(
    ...rows.map(([label, value, tone]) => {
      const row = document.createElement("div");
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value;
      if (tone) dd.className = tone;
      row.append(dt, dd);
      return row;
    })
  );
  const company = health.company || {};
  const companyRows = [
    ["Working in", company.display_name || "not set", null],
    ["Company file", company.file_path || "(not set)", null],
    ["File found on disk", company.file_exists ? "Yes" : "No", company.file_exists ? "on" : "off"],
  ];
  $("companyList").replaceChildren(
    ...companyRows.map(([label, value, tone]) => {
      const row = document.createElement("div");
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value;
      if (tone) dd.className = tone;
      row.append(dt, dd);
      return row;
    })
  );

  const sourceRows = [
    ["Rows loaded", String(health.loaded_rows ?? 0), null],
    ["Tabs found", (health.tabs || []).join(", ") || "none", null],
    ["Last read", health.imported_at
      ? new Date(health.imported_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })
      : "not yet", null],
    ["Refreshing", health.watching ? "Every minute" : "Off", health.watching ? "on" : "off"],
  ];
  $("sourceList").replaceChildren(
    ...sourceRows.map(([label, value, tone]) => {
      const row = document.createElement("div");
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value;
      if (tone) dd.className = tone;
      row.append(dt, dd);
      return row;
    })
  );
  // Never overwrite a link being worked on. This now runs every twenty seconds,
  // so "not focused right now" is not enough - a pasted link left while the
  // person reads it over would be wiped. While the panel is unlocked the box
  // belongs to them.
  const box = $("sourceUrl");
  const unlocked = !document.querySelector('[data-lock="source"]').classList.contains("locked");
  if (document.activeElement !== box && !unlocked) {
    box.value = health.data_source_url || "";
  }
  const note = $("sourceNote");
  note.hidden = !health.load_error;
  if (health.load_error) note.textContent = `Last read failed: ${health.load_error}`;

  $("unlockPanel").hidden = Boolean(health.writes_enabled);
}

function renderActivity() {
  const needle = (state.historySearch || "").trim().toLowerCase();
  const since = historyCutoff();

  const events = state.activity.filter((event) => {
    const filter = state.historyFilter;
    if (filter !== "all") {
      // "Problems" is what someone opens when something went wrong, so it
      // gathers both kinds: refused before sending, and failed while sending.
      const matches = filter === "problems"
        ? event.status === "failed" || event.status === "needs_review"
        : event.status === filter;
      if (!matches) return false;
    }
    if (state.historyAction !== "all" && event.operation !== state.historyAction) return false;
    if (since && event.created_at < since) return false;
    if (!needle) return true;
    // The error is searchable too: pasting a QuickBooks message finds every
    // row that hit it.
    return [event.label, event.message, event.error, event.source_id]
      .join(" ")
      .toLowerCase()
      .includes(needle);
  });
  drawActivity($("activity"), events, true);

  const failed = state.activity.filter((event) => event.status === "failed").length;
  const review = state.activity.filter((event) => event.status === "needs_review").length;
  const filtered = events.length !== state.activity.length;
  $("historySummary").textContent = state.activity.length
    ? (filtered ? `${events.length} of ${state.activity.length} entries` : `${state.activity.length} entries`) +
      ` · ${failed} failed · ${review} refused`
    : "";
  $("historySummary").classList.toggle("bad-note", failed > 0);
}

/** The ISO timestamp the "when" filter starts from, or "" for any time. */
function historyCutoff() {
  const when = state.historyWhen;
  if (!when || when === "all") return "";
  const now = new Date();
  if (when === "today") {
    const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    return midnight.toISOString();
  }
  const days = Number(when);
  if (!Number.isFinite(days)) return "";
  return new Date(now.getTime() - days * 86400000).toISOString();
}

/* `detailed` turns each entry into a log line an engineer can act on: which
   operation ran, every step it completed, and the error verbatim. The
   dashboard's summary keeps the plain version. */
function drawActivity(container, events, detailed = false) {
  if (!events.length) {
    container.replaceChildren(div("act-empty", "Nothing here yet."));
    return;
  }
  container.replaceChildren(
    ...events.map((event) => (detailed ? logEntry(event) : activityEntry(event)))
  );
}

function activityEntry(event) {
  const item = document.createElement("div");
  const key = eventKey(event);
  const fresh = seenEvents.size > 0 && !seenEvents.has(key);
  seenEvents.add(key);
  item.className = `act ${event.status}${fresh ? " just-in" : ""}`;
  const body = div("act-body", "");
  body.append(div("strong", event.label), div("sub", event.message));
  item.append(div("dot", ""), body, whenCell(event));
  return item;
}

function whenCell(event) {
  const when = div("when", time(event.created_at));
  if (event.mode) {
    const isLive = event.mode === "quickbooks";
    const modeTag = document.createElement("span");
    modeTag.className = `act-mode ${isLive ? "live" : "safe"}`;
    modeTag.textContent = isLive ? "LIVE" : "SAFE TEST";
    modeTag.title = isLive
      ? "Written for real to QuickBooks."
      : "Safe mode was on — nothing was written to QuickBooks.";
    when.prepend(modeTag);
  }
  return when;
}

const OPERATION_LABEL = {
  create_customer: "customer + job",
  update_customer: "update customer",
  create_invoice: "customer + job + invoice",
  premium_invoice: "premium invoice",
  commission_invoice: "commission invoice",
  fees_invoice: "fees invoice",
};

/* Which log entries this page has already shown. Only the genuinely new ones
   animate in - replaying the entrance on all 200 every time the log is redrawn
   would be a flash, not an arrival. */
const seenEvents = new Set();

function eventKey(event) {
  return `${event.created_at}|${event.source_id}|${event.operation}|${event.status}`;
}

function logEntry(event) {
  const failed = event.status === "failed" || event.status === "needs_review";
  const item = document.createElement("details");
  const key = eventKey(event);
  const fresh = seenEvents.size > 0 && !seenEvents.has(key);
  seenEvents.add(key);
  item.className = `log ${event.status}${fresh ? " just-in" : ""}`;
  item.open = event.status === "failed";   // problems are open by default

  const head = document.createElement("summary");
  const chip = tag(STATUS_LABEL[event.status] || event.status, event.status);
  const title = div("log-title", "");
  title.append(div("strong", event.label), div("sub", event.message));

  const meta = div("log-meta", "");
  if (event.operation) {
    meta.append(tag(OPERATION_LABEL[event.operation] || event.operation, ""));
  }
  head.append(chip, title, meta, whenCell(event));
  item.append(head);

  const detail = div("log-detail", "");
  // Where it got to. On a failure this is the last line that worked.
  if ((event.steps || []).length) {
    detail.append(div("log-label", failed ? "Got as far as" : "Steps"));
    const steps = document.createElement("ol");
    steps.className = "log-steps";
    for (const step of event.steps) {
      const li = document.createElement("li");
      li.textContent = step;
      steps.append(li);
    }
    detail.append(steps);
  }
  if (event.error) {
    detail.append(div("log-label", "Error"));
    detail.append(div("log-error mono", event.error));
  }

  const facts = div("log-facts", "");
  for (const [key, value] of [
    ["row", event.row_number || "—"],
    ["policy id", event.source_id],
    ["action", event.operation || "—"],
    ["mode", event.mode === "quickbooks" ? "live" : "safe test"],
    ["at", event.created_at],
  ]) {
    facts.append(div("log-fact", `${key}: ${value}`));
  }
  detail.append(facts);

  const copy = document.createElement("button");
  copy.className = "btn ghost small";
  copy.type = "button";
  copy.textContent = "Copy details";
  copy.addEventListener("click", (clicked) => {
    clicked.stopPropagation();
    const text = [
      `${event.label}`,
      `status: ${event.status}   action: ${event.operation || "—"}   mode: ${event.mode}`,
      `row: ${event.row_number || "—"}   policy id: ${event.source_id}`,
      `at: ${event.created_at}`,
      `message: ${event.message}`,
      ...(event.steps || []).map((step, index) => `  ${index + 1}. ${step}`),
      event.error ? `error: ${event.error}` : "",
    ].filter(Boolean).join("\n");
    navigator.clipboard.writeText(text).then(
      () => toast("Details copied — paste them to whoever is fixing it."),
      () => toast("Could not copy.", true)
    );
  });
  detail.append(copy);

  item.append(detail);
  return item;
}

function renderSelection() {
  const count = state.selected.size;
  const live = Boolean(state.health && state.health.writes_enabled);
  $("selectionNote").textContent = count
    ? `${count} of ${MAX_SELECTION} selected`
    : "";
  $("selectionNote").classList.toggle("bad-note", count >= MAX_SELECTION);

  const send = $("processButton");
  send.disabled = count === 0;
  send.classList.remove("is-busy");
  send.textContent = live ? "Send Customers/Jobs" : "Send Customers/Jobs (safe test)";

  const invoice = $("invoiceButton");
  invoice.disabled = count === 0;
  invoice.classList.remove("is-busy");
  invoice.textContent = live ? "Send Invoice" : "Send Invoice (safe test)";

  const bulk = $("bulkEditButton");
  bulk.disabled = count === 0;
  bulk.textContent = count ? `Edit ${count} selected` : "Edit selected";

  for (const box of document.querySelectorAll("#rows input[type=checkbox]")) {
    const row = box.closest("tr");
    if (row) box.checked = state.selected.has(row.dataset.id);
  }
}

/* ═══ editing many policies at once ═══════════════════════════════════════
   The same correction over and over - a carrier spelt differently, a fee that
   applies to a whole batch - is the one thing the row-by-row editor is bad at.
   A field is only written when its box is ticked, so an empty box can mean
   "clear this" rather than "leave it alone". */

function openBulkEdit() {
  const ids = [...state.selected];
  if (!ids.length) return;
  const records = ids
    .map((id) => state.records.find((entry) => entry.source_id === id))
    .filter(Boolean);
  if (!records.length) return;

  $("bulkSubtitle").textContent =
    `${records.length} polic${records.length === 1 ? "y" : "ies"} selected`;

  const fields = SHEET_COLUMNS.filter((column) => column.key !== "customer_name");
  $("bulkFields").replaceChildren(...fields.map((column) => bulkField(column, records)));
  $("bulkNote").textContent = "Nothing is changed until you press Apply.";
  $("bulkDialog").showModal();
}

/** One row of the bulk editor: tick to change, then the value to write. */
function bulkField(column, records) {
  const row = document.createElement("label");
  row.className = "bulk-field";

  const tick = document.createElement("input");
  tick.type = "checkbox";
  tick.dataset.field = column.key;

  const name = document.createElement("span");
  name.className = "bulk-label";
  name.textContent = column.label;

  const input = document.createElement("input");
  input.type = "text";
  input.disabled = true;
  input.dataset.value = column.key;
  // What the chosen rows hold now, so it is clear what is being replaced.
  const values = new Set(records.map((record) => {
    const raw = rawValue(record, column.key).trim();
    return column.type === "date" ? usDate(raw) : raw;
  }));
  input.placeholder = values.size === 1
    ? `currently ${[...values][0] || "(empty)"}`
    : `${values.size} different values`;
  if (column.type === "date") input.placeholder += "  ·  type as MM/DD/YYYY";

  tick.addEventListener("change", () => {
    input.disabled = !tick.checked;
    row.classList.toggle("is-on", tick.checked);
    if (tick.checked) input.focus();
    renderBulkNote();
  });
  input.addEventListener("input", renderBulkNote);

  row.append(tick, name, input);
  return row;
}

/** What the current ticks will do, said plainly before anything happens. */
function renderBulkNote() {
  const chosen = [...document.querySelectorAll("#bulkFields input[type=checkbox]")]
    .filter((tick) => tick.checked);
  const rows = state.selected.size;
  $("bulkNote").textContent = chosen.length
    ? `${chosen.length} field${chosen.length === 1 ? "" : "s"} on ${rows} polic${rows === 1 ? "y" : "ies"}.`
    : "Tick a field to change it.";
  $("bulkApply").disabled = chosen.length === 0;
}

async function applyBulkEdit() {
  const values = {};
  for (const tick of document.querySelectorAll("#bulkFields input[type=checkbox]")) {
    if (!tick.checked) continue;
    const box = document.querySelector(`#bulkFields input[data-value="${tick.dataset.field}"]`);
    values[tick.dataset.field] = box ? box.value.trim() : "";
  }
  const ids = [...state.selected];
  const names = Object.keys(values);
  if (!names.length || !ids.length) return;

  const blanks = names.filter((field) => !values[field]);
  const go = await ask({
    title: `Change ${names.length} field${names.length === 1 ? "" : "s"} on ${ids.length} policies?`,
    sub: "The corrections are kept locally and survive every refresh of the sheet. "
       + "They do not change the Google Sheet itself.",
    detail: names.map((field) => `${field}  ->  ${values[field] || "(cleared)"}`).join("\n"),
    note: blanks.length
      ? `${blanks.length} field${blanks.length === 1 ? " was" : "s were"} left empty, so `
        + `${blanks.length === 1 ? "it" : "they"} will be cleared on every chosen policy.`
      : "Anything already sent to QuickBooks needs sending again to pick this up.",
    ok: "Apply to all",
    cancel: "Cancel",
    tone: "ask",
  });
  if (!go) return;

  const button = $("bulkApply");
  const label = button.textContent;
  setBusy(button, "Applying…");
  try {
    const payload = await api("/api/edit-many", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids, values }),
    }, SEND_TIMEOUT_MS);

    for (const updated of payload.updated || []) {
      const index = state.records.findIndex((entry) => entry.source_id === updated.source_id);
      if (index >= 0) state.records[index] = { ...state.records[index], ...updated };
    }
    $("bulkDialog").close();
    renderAll();

    const failed = payload.failed || [];
    if (failed.length) {
      await ask({
        title: `${payload.updated.length} changed, ${failed.length} refused`,
        sub: "The rest went through. These could not take one of the values:",
        detail: failed.map((one) => `${one.source_id}: ${one.error}`).join("\n"),
        ok: "Close",
        tone: "danger",
      });
    } else {
      toast(`${payload.updated.length} policies updated.`);
    }
  } catch (error) {
    await ask({
      title: "Nothing was changed",
      detail: error.message,
      ok: "Close",
      tone: "danger",
    });
  } finally {
    button.classList.remove("is-busy");
    button.textContent = label;
    button.disabled = false;
  }
}

/* ═══ choosing the company ════════════════════════════════════════════════
   The companies on this machine are listed so one can be picked here, but
   QuickBooks serves only one at a time: while it is running, the company it
   has open is the only one reachable. The list says which that is, and the
   picker explains the rest rather than failing at the next send. */

/* ═══ changing the sheet link ═════════════════════════════════════════════
   The link is long enough to be mangled in transit, so it is proved against
   the real feed before it replaces the working one.

   It also sits behind a lock. Every policy on the dashboard comes through it,
   so an accidental keystroke in that box would empty the whole thing - the
   lock makes changing it a decision rather than a slip. */

const RELOCK_AFTER_MS = 120000;
let relockTimer = 0;

function setSourceLock(locked) {
  const panel = document.querySelector('[data-lock="source"]');
  panel.classList.toggle("locked", locked);
  for (const control of panel.querySelectorAll(".guarded")) control.disabled = locked;
  panel.querySelector(".lock-text").textContent = locked ? "Unlock to edit" : "Lock";
  panel.querySelector(".lock-btn").title = locked
    ? "The link is protected from accidental changes"
    : "Lock it again";

  clearTimeout(relockTimer);
  if (!locked) {
    // Left unlocked and forgotten is the same as never locked, so it closes
    // itself after a couple of quiet minutes.
    relockTimer = setTimeout(() => {
      setSourceLock(true);
      toast("Sheet link locked again.");
    }, RELOCK_AFTER_MS);
    $("sourceUrl").focus();
  }
}

function toggleSourceLock() {
  const locked = document.querySelector('[data-lock="source"]').classList.contains("locked");
  setSourceLock(!locked);
  if (locked) toast("Unlocked. The link can be changed now.");
}

async function saveDataSource() {
  const button = $("sourceSave");
  const box = $("sourceUrl");
  const url = box.value.trim();
  const current = (state.health && state.health.data_source_url) || "";
  if (!url) {
    toast("Paste the link first.");
    return;
  }
  if (url === current) {
    toast("That is already the link in use.");
    return;
  }

  const go = await ask({
    title: "Read the policies from this link?",
    sub: "The link is tried first. Nothing changes unless it works and has rows "
       + "behind it.",
    detail: url,
    note: "Every policy is re-read from the new sheet. What has already been "
        + "created in QuickBooks is untouched.",
    ok: "Test and save",
    cancel: "Not now",
    tone: "ask",
  });
  if (!go) return;

  const label = button.textContent;
  setBusy(button, "Testing the link…");
  try {
    const payload = await api("/api/source", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    }, RECORDS_TIMEOUT_MS);
    state.health = payload.health || state.health;
    renderHealth();
    renderStatus();
    setSourceLock(true);                 // done with it - protect it again
    toast(`Link saved. ${payload.source.rows} policies loaded.`);
    await refresh();
  } catch (error) {
    await ask({
      title: "That link was not used",
      sub: "The old link is still in place, so nothing was lost.",
      detail: error.message,
      ok: "Close",
      tone: "danger",
    });
  } finally {
    button.classList.remove("is-busy");
    button.textContent = label;
    // Not simply enabled again: a save that succeeded has re-locked the panel.
    button.disabled = document
      .querySelector('[data-lock="source"]').classList.contains("locked");
  }
}

/* Stands for "whatever QuickBooks has open", used when the file's own path
   could not be read - the server treats an empty path as exactly that. */
const OPEN_COMPANY = "__open__";

async function loadCompanies({ announce = false } = {}) {
  const select = $("companyChoice");
  const hint = $("companyHint");
  const button = $("companyRescan");
  const pick = document.querySelector(".company-pick");

  pick.classList.add("scanning");
  select.replaceChildren(new Option("Scanning for company files…", ""));
  if (announce) setBusy(button, "Scanning…");
  // A scan that answers instantly would flash and read as nothing happening,
  // so the animation is given a floor to run out.
  const settled = new Promise((done) => setTimeout(done, announce ? 650 : 250));

  try {
    const [payload] = await Promise.all([api("/api/companies", {}, 20000), settled]);
    state.companies = payload;
  } catch {
    select.replaceChildren(new Option("Could not read the company list", ""));
    return;
  } finally {
    pick.classList.remove("scanning");
    if (announce) {
      button.disabled = false;
      button.classList.remove("is-busy");
      button.textContent = button.dataset.label || "Rescan";
    }
  }

  const {
    companies = [], open_company: open, open_file: openFile, current_file: current,
  } = state.companies;
  // The name QuickBooks shows is the company name, which is set inside the
  // file and often differs from the file's own name. Say so, or the list looks
  // as though it has missed the company that is plainly open.
  const openStem = openFile ? openFile.replace(/^.*[\\/]/, "").replace(/\.qbw$/i, "") : "";
  const aka = openStem && openStem.toLowerCase() !== open.toLowerCase()
    ? ` It is saved as the file ${openStem}.qbw, which is the name shown below.`
    : "";
  select.replaceChildren();
  if (!companies.length) {
    select.append(new Option("No company files found", ""));
  }
  for (const item of companies) {
    const tags = [];
    if (item.open) tags.push("open in QuickBooks");
    if (item.current) tags.push("in use by the app");
    // Two companies can share a file name in different folders, and then the
    // name alone gives no way to tell which is which.
    if (item.ambiguous && item.folder) tags.push(`in ${item.folder}`);
    const label = tags.length
      ? `${item.display_name}  —  ${tags.join(", ")}`
      : item.display_name;
    // No path means QuickBooks has it open but would not say where it lives -
    // it can still be adopted as "whichever company is open right now".
    const option = new Option(label, item.file_path || OPEN_COMPANY);
    option.dataset.name = item.display_name;
    option.selected = item.current;
    select.append(option);
  }
  // With QuickBooks closed, nothing is locked in - but the choice is then a
  // request to start that company, which is worth saying plainly.
  hint.textContent = open
    ? `QuickBooks has ${open} open.${aka} It serves one company at a time, so `
      + `that is the only one the app can be switched to right now. To use a `
      + `different one, open it in QuickBooks first (File, Open Previous Company).`
    : `QuickBooks is not running. Pick any company here and QuickBooks will `
      + `start it — the first time, answer its permission request with `
      + `"Yes, whenever this company file is open".`;
  if (current) select.dataset.current = current;

  // The company the app is aimed at and the one QuickBooks is serving are two
  // separate things, and when they differ nothing can be sent at all. That is
  // worth saying outright rather than leaving it to be read off two tags.
  const mismatch = $("companyMismatch");
  const openHere = companies.find((item) => item.open);
  const wrong = openFile && current && openFile.toLowerCase() !== current.toLowerCase();
  mismatch.hidden = !wrong;
  if (wrong) {
    const aimed = (state.health && state.health.company.display_name) || "another company";
    mismatch.textContent =
      `Nothing can be sent right now. The app is aimed at ${aimed}, but QuickBooks `
      + `has ${open} open — and it only serves the company it has open. Either `
      + `switch the app to ${(openHere && openHere.display_name) || open} below, or `
      + `open ${aimed} in QuickBooks.`;
  }

  select.classList.remove("just-scanned");
  void select.offsetWidth;                       // restart the settle pulse
  select.classList.add("just-scanned");
  if (announce) {
    toast(companies.length === 1
      ? "Found 1 company."
      : `Found ${companies.length} companies.`);
  }
}

async function useCompany() {
  const button = $("useCompany");
  const select = $("companyChoice");
  const chosen = select.value;
  const picked = select.selectedOptions[0];
  const name = (picked && picked.dataset.name)
    || chosen.replace(/^.*[\\/]/, "").replace(/\.qbw$/i, "");
  const current = (state.health && state.health.company) || {};
  if (!chosen) {
    toast("There is no company file selected.");
    return;
  }
  if (chosen !== OPEN_COMPANY
      && current.file_path && chosen.toLowerCase() === current.file_path.toLowerCase()) {
    toast(`Already working in ${name}.`);
    return;
  }

  // Both names, always: the one being left and the one being adopted. Showing
  // only the current file read as though that were the company being switched to.
  const lines = [`Switching to:  ${name}`];
  if (current.display_name) lines.push(`Leaving:  ${current.display_name}`);

  const go = await ask({
    title: `Switch the app to ${name}?`,
    sub: "Every customer, job and invoice this dashboard creates from now on "
       + "goes into that company.",
    detail: lines.join("\n"),
    note: "It keeps its own history file, so nothing from the previous company "
        + "is treated as already done.",
    ok: "Switch",
    cancel: "Not now",
    tone: "ask",
  });
  if (!go) return;

  const label = button.textContent;
  setBusy(button, "Asking QuickBooks…");
  try {
    const payload = await api("/api/company", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file_path: chosen === OPEN_COMPANY ? "" : chosen }),
    }, 90000);
    state.health = payload.health || state.health;
    renderHealth();
    renderStatus();
    await loadCompanies();
    toast(`Now working in ${payload.company.company}.`);
    await refresh();
  } catch (error) {
    await ask({
      title: "Could not switch company",
      sub: "The app is still working in " + (current.display_name || "the old company") + ".",
      detail: error.message,
      ok: "Close",
      tone: "danger",
    });
  } finally {
    button.disabled = false;
    button.classList.remove("is-busy");
    button.textContent = label;
  }
}

/* ═══ the pre-flight check ════════════════════════════════════════════════
   Every account, template and item is matched by exact name, so a rename in
   QuickBooks breaks a whole kind of invoice silently. This is where that
   shows up - once, before a batch - instead of as 200 failed rows. */

async function runPreflight() {
  const button = $("preflightRun");
  const list = $("preflightList");
  button.disabled = true;
  button.textContent = "Checking…";
  showState(list, "loading", "Asking QuickBooks…");
  $("preflightNotes").replaceChildren();
  try {
    renderPreflight(await api("/api/preflight"));
  } catch (error) {
    showState(list, "error", error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Run check";
  }
}

function renderPreflight(result) {
  const list = $("preflightList");
  if (result.error) {
    showState(list, "error", result.error);
    return;
  }
  list.replaceChildren(
    ...result.checks.map((row) => {
      const item = document.createElement("div");
      item.className = `pf ${row.ok ? "ok" : "bad"}`;

      const icon = document.createElement("span");
      icon.className = "pf-mark";
      icon.textContent = row.ok ? "✓" : "✗";

      const text = document.createElement("div");
      const label = document.createElement("strong");
      label.textContent = row.label;
      const value = document.createElement("span");
      value.className = "mono";
      value.textContent = row.value;
      text.append(label, value);

      if (!row.ok) {
        const hint = document.createElement("em");
        hint.textContent = `Not found in QuickBooks → ${row.where}. Every ` +
          `${row.label.split(" ")[0].toLowerCase()} invoice will fail until the ` +
          `names match.`;
        text.append(hint);
      }
      item.append(icon, text);
      return item;
    })
  );

  $("preflightNotes").replaceChildren(
    ...(result.notes || []).flatMap((note) => {
      const dt = document.createElement("dt");
      dt.textContent = note.label;
      const dd = document.createElement("dd");
      dd.textContent = note.value;
      return [dt, dd];
    })
  );

  const total = result.checks.length;
  const bad = result.checks.filter((row) => !row.ok).length;
  $("preflightIntro").textContent = bad
    ? `${bad} of ${total} names do not match QuickBooks. Fix these before sending.`
    : `All ${total} names match QuickBooks. Checked ${time(result.checked_at)}.`;
  $("preflightIntro").classList.toggle("bad-note", bad > 0);
  toast(bad ? `${bad} setting${bad === 1 ? "" : "s"} need attention.` : "Everything matches.", bad > 0);
}

/* ═══ actions ═════════════════════════════════════════════════════════════ */

async function fetchSheet(tab = "", force = false) {
  state.selected.clear();
  $("refreshButton").disabled = true;
  try {
    // Downloading the sheet and rebuilding 2,000+ policies takes well over the
    // ordinary deadline, so this call gets the longer one.
    const payload = await api(
      "/api/fetch",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tab, force }),
      },
      SEND_TIMEOUT_MS
    );
    const summary = payload.import;
    // Nothing is wrong when the sheet loads, so no banner: the skipped rows are
    // just blanks and headers, and how a column was matched is not news. The
    // toast says what loaded; only a real failure below gets the banner.
    hideNote();
    toast(`Loaded ${summary.loaded_rows} policies.`);
    await refresh();
  } catch (error) {
    note(error.message, "error");
    toast(error.message, true);
  } finally {
    $("refreshButton").disabled = false;
  }
}

/* Only the bulk Send button uses this. Updating is a per-row action now, so
   there is no bulk update entry here. */
const OPERATIONS = {
  create_customer: { button: "processButton", verb: "Create", label: "Send Customers/Jobs" },
};

function rowActionButton(text, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "mini-btn";
  button.textContent = text;
  button.addEventListener("click", (event) => {
    event.stopPropagation();   // don't also open the row editor
    onClick(button);
  });
  return button;
}

function setBusy(button, busyLabel) {
  button.disabled = true;
  button.classList.add("is-busy");
  button.dataset.label = button.textContent;
  button.innerHTML = `<span class="spinner-sm"></span>${busyLabel}`;
}

/* QuickBooks Desktop's SDK only tolerates one session at a time - opening
   several at once (e.g. clicking Send on five rows in a row) can leave later
   ones waiting on an earlier one indefinitely, which looked like the button
   was stuck. Every write funnels through this one queue so only one request
   is ever in flight, and everything behind it is honestly labelled "Queued". */
let sendQueue = Promise.resolve();
let queueDepth = 0;   // how many writes are already waiting ahead of a new one

function runQueued(task) {
  queueDepth += 1;
  const run = sendQueue.then(task).finally(() => {
    queueDepth -= 1;
  });
  sendQueue = run.catch(() => {});   // one failure must not jam the rest of the queue
  return run;
}

/** Send or update a single policy's customer, without touching the checkboxes.
    Clicking the row button is itself the confirmation - no popup on top of it. */
async function actOnPolicy(sourceId, operation, button, label = "") {
  const busyLabel = label || (operation === "update_customer" ? "Updating…" : "Sending…");
  const waiting = queueDepth > 0;
  const startLabel = waiting ? "Queued…" : busyLabel;
  if (button) {
    button.disabled = true;
    button.classList.add("is-busy");
    button.dataset.label = button.textContent;
    button.innerHTML = `<span class="spinner-sm"></span>${startLabel}`;
  }
  // The badge on the row, which outlives the button: the table is rebuilt
  // every time some other row finishes, and the button goes with it.
  markRow(sourceId, waiting ? "queued" : "sending", operation, busyLabel);

  await runQueued(async () => {
    markRow(sourceId, "sending", operation, busyLabel);
    if (button) button.innerHTML = `<span class="spinner-sm"></span>${busyLabel}`;
    // One row still takes several seconds against QuickBooks, and its steps are
    // logged as they happen - so the History page fills in here too, not only
    // during a bulk send.
    startHistoryFeed();
    try {
      const payload = await api(
        "/api/process",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ids: [sourceId], operation }),
        },
        SEND_TIMEOUT_MS
      );
      state.records = payload.records || state.records;
      state.activity = payload.activity || state.activity;
      // Cleared only now: the badge stays up from the click right through the
      // queue and the send, and gives way to the row's real status.
      clearRow(sourceId);
      renderAll();   // rebuilds the row, so the button above no longer exists
      const result = (payload.results || [])[0];
      if (result) toast(`${result.label}: ${result.message}`, result.status === "failed");
      // Its new status may well have taken it off the current filter. Let it be
      // seen first.
      holdThenDrop([sourceId]);
    } catch (error) {
      clearRow(sourceId);
      // The row was never rebuilt, so put the button back the way it was.
      if (button) {
        button.disabled = false;
        button.classList.remove("is-busy");
        button.textContent = button.dataset.label || busyLabel;
      }
      renderTable();
      await recoverFromSendFailure(error);
    } finally {
      stopHistoryFeed();
    }
  });
}

/* A send that never answers is not the same as a send that failed: QuickBooks
   may well have done the work. Rather than leave the row showing a guess, go
   and read the real state back. */
async function recoverFromSendFailure(error) {
  if (!(error instanceof TimedOut)) {
    toast(error.message, true);
    return;
  }
  toast("No answer yet — checking what actually happened…", true);
  try {
    await refresh();
  } catch {
    /* refresh reports its own trouble */
  }
}

async function processSelected(operation = "create_customer") {
  const ids = [...state.selected];
  if (!ids.length) return;
  const spec = OPERATIONS[operation];
  const live = Boolean(state.health && state.health.writes_enabled);
  const noun = `${ids.length} polic${ids.length === 1 ? "y" : "ies"}`;
  if (live && !(await quickBooksIsReady())) return;
  if (
    live &&
    !(await ask({
      title: `Send ${noun} to QuickBooks?`,
      sub: "A customer is created for each corporation, with the policy as a job underneath.",
      note: "Anything already in QuickBooks is reused, never duplicated.",
      ok: `${spec.verb} them`,
      cancel: "Not yet",
      tone: "ask",
    }))
  ) {
    return;
  }
  await processBatch(operation, ids, spec.button, { noun, label: spec.label, clear: true });
}

/* ═══ asking the user ═════════════════════════════════════════════════════
   The browser's own alert() and confirm() drop a grey Windows box on top of
   the app and name the server in its title. These ask the same questions in
   the app's own voice.

   Returns true when the person went ahead. Leave `cancel` out for a message
   that only needs acknowledging. */
function ask({ title, sub = "", detail = "", note = "", ok = "OK", cancel = "", tone = "warn" }) {
  const dialog = $("askDialog");
  $("askTitle").textContent = title;
  $("askSub").textContent = sub;
  $("askNote").textContent = note;

  const icon = $("askIcon");
  icon.className = `ask-icon ${tone}`;
  icon.textContent = tone === "danger" ? "!" : tone === "ask" ? "?" : "!";

  const body = $("askBody");
  body.replaceChildren();
  if (detail) body.append(div("ask-detail mono", detail));
  body.hidden = !detail;

  const okButton = $("askOk");
  const cancelButton = $("askCancel");
  okButton.textContent = ok;
  cancelButton.textContent = cancel || "Cancel";
  cancelButton.hidden = !cancel;

  return new Promise((resolve) => {
    const finish = (answer) => {
      okButton.removeEventListener("click", yes);
      cancelButton.removeEventListener("click", no);
      dialog.removeEventListener("close", no);
      if (dialog.open) dialog.close();
      resolve(answer);
    };
    const yes = () => finish(true);
    const no = () => finish(false);
    okButton.addEventListener("click", yes);
    cancelButton.addEventListener("click", no);
    dialog.addEventListener("close", no);
    dialog.showModal();
    (cancel ? cancelButton : okButton).focus();
  });
}

/* QuickBooks crashes outright if its Customer Centre is open while the
   customer list is written to, and a modal dialog freezes every request until
   somebody clicks it. Neither is visible to the SDK, so the app reads the
   window titles instead and stops before doing damage. */
async function quickBooksIsReady() {
  let ui;
  try {
    ui = (await api("/api/health", {}, 10000)).quickbooks_ui || {};
  } catch {
    return true;   // cannot check, so do not stand in the way
  }
  // Nothing to send to. Caught here rather than let the attempt go through:
  // with QuickBooks closed the app used to ask it to start itself, which took
  // a minute and could bring QuickBooks down with an Unrecoverable Error.
  if (ui.checked && !ui.running) {
    await ask({
      title: "QuickBooks is not open",
      sub: "Open QuickBooks Desktop with your company file, then send again.",
      note: "The app works inside the QuickBooks you have open — it cannot "
          + "start it for you.",
      ok: "I'll open it",
      tone: "danger",
    });
    return false;
  }
  if (!ui.checked || ui.safe_to_send) return true;

  if (ui.dialog) {
    await ask({
      title: "QuickBooks is waiting for an answer",
      sub: "Nothing can be sent until this message box is closed.",
      detail: ui.dialog,
      // These boxes often open behind the browser, or on a different monitor
      // from QuickBooks itself, so "look at QuickBooks" is not enough.
      note: "Can't see it? It may be behind this window or on your other screen — " +
            "minimise the browser, or press Alt+Tab and look for that title.",
      ok: "I'll close it",
      tone: "danger",
    });
    return false;
  }
  return ask({
    title: "Close the Customer Centre first",
    sub: "QuickBooks can crash when customers are added while that window is open.",
    detail: ui.customer_centre,
    note: "Leave QuickBooks on the Home screen and check your results afterwards.",
    ok: "Send anyway",
    cancel: "I'll close it",
    tone: "warn",
  });
}

/* While a batch runs the browser is just waiting on one long request, so the
   server publishes what it has finished. Polling that is what lets each row
   show "Sending…", then flip to Created, while the rest sit in the queue. */
let progressTimer = null;
let historyTimer = null;
let lastProgressMark = "";

function watchProgress(button, what) {
  clearInterval(progressTimer);
  progressTimer = setInterval(async () => {
    let progress;
    try {
      progress = await api("/api/progress", {}, 8000);
    } catch {
      return;   // a missed poll changes nothing; the next one catches up
    }
    if (!progress || !progress.active) return;

    // Fold what has finished into the rows on screen.
    for (const done of progress.finished || []) {
      const record = state.records.find((entry) => entry.source_id === done.source_id);
      if (!record) continue;
      const kind = INVOICE_KINDS.find((entry) => entry.operation === done.operation);
      if (kind) record[`${kind.key}_status`] = done.status;
      else record.status = done.status;
    }
    state.sendingOperation = progress.operation || state.sendingOperation;
    state.sending = new Set(progress.current ? [progress.current] : []);
    state.queued = new Set(progress.queue || []);

    // Redrawn only when the batch has actually moved on. The poll runs about
    // once a second, and rebuilding three hundred rows on every tick made the
    // page heavy to use while a batch was running.
    const mark = `${progress.done}|${progress.current}|${(progress.queue || []).length}`;
    if (mark !== lastProgressMark) {
      lastProgressMark = mark;
      renderTable();
      renderSheetCards(visibleRecords());
    }

    if (button) {
      button.innerHTML =
        `<span class="spinner-sm"></span>${progress.done} of ${progress.total} — ${what}`;
    }
  }, 900);

  startHistoryFeed();
}

/* History fills in as the work happens rather than all at once at the end.
   Every row is written to the log the moment it finishes, so there is no
   reason to make someone watching the History page wait for the other 49. */
function startHistoryFeed() {
  clearInterval(historyTimer);
  historyTimer = setInterval(async () => {
    let history;
    try {
      history = await api("/api/activity", {}, 10000);
    } catch {
      return;   // a missed poll changes nothing; the next one catches up
    }
    const entries = history.activity || [];
    // Compared by the newest entry, not by how many there are: the log is
    // served capped at 200, so once it is full the count stops moving and a
    // length check would report "nothing new" for the rest of time.
    if (historyMark(entries) === historyMark(state.activity)) return;
    state.activity = entries;
    renderActivity();
  }, 1500);
}

function stopHistoryFeed() {
  clearInterval(historyTimer);
  historyTimer = null;
}

/** Identifies the newest entry, so a changed log is noticed however long it is. */
function historyMark(entries) {
  const newest = (entries || [])[0];
  if (!newest) return "";
  return `${entries.length}|${newest.created_at}|${newest.source_id}|${newest.status}`;
}

function stopWatchingProgress() {
  clearInterval(progressTimer);
  stopHistoryFeed();
  progressTimer = null;
  lastProgressMark = "";
  state.sending = new Set();
  state.queued = new Set();
  state.sendingOperation = "";
}

/** Send one batch of rows for one operation, with the bulk button as progress. */
async function processBatch(operation, ids, buttonId, { noun, label, clear = false } = {}) {
  if (!ids.length) return;
  const batch = ids.slice(0, MAX_SELECTION);   // the server refuses more anyway
  const button = $(buttonId);
  const what = noun || `${batch.length} polic${batch.length === 1 ? "y" : "ies"}`;
  setBusy(button, queueDepth > 0 ? "Queued…" : `Working on ${what}…`);
  // Badged before joining the queue, not after: a batch waiting behind another
  // send would otherwise show nothing at all until its turn came round.
  state.sendingOperation = operation;
  state.queued = new Set(batch);
  renderTable();

  await runQueued(async () => {
    button.innerHTML = `<span class="spinner-sm"></span>Working on ${what}…`;
    watchProgress(button, what);
    try {
      const payload = await api(
        "/api/process",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ids: batch, operation }),
        },
        SEND_TIMEOUT_MS
      );
      state.records = payload.records || state.records;
      state.activity = payload.activity || state.activity;
      const results = payload.results || [];
      const good = results.filter((result) => DONE.has(result.status)).length;
      const bad = results.length - good;
      // Finished rows stop being selected, so the next batch is a fresh pick.
      if (clear) for (const id of batch) state.selected.delete(id);
      renderAll();
      toast(bad ? `${good} done · ${bad} need attention` : `${what} finished.`, bad > 0);
      if (bad) goto("history");
    } catch (error) {
      await recoverFromSendFailure(error);
    } finally {
      stopWatchingProgress();
      button.textContent = label || button.textContent;
      button.classList.remove("is-busy");
      renderAll();
      renderSelection();
      // Rows the batch has just taken off the current filter stay a moment
      // longer, so the result of the work can be read before they go.
      holdThenDrop(batch);
    }
  });
}

/* ═══ helpers ═════════════════════════════════════════════════════════════ */

function td(text = "") {
  const cell = document.createElement("td");
  if (text) cell.textContent = text;
  return cell;
}

function div(className, text) {
  const element = document.createElement("div");
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
}

function tag(text, kind) {
  const element = document.createElement("span");
  element.className = kind ? `tag ${kind}` : "tag";
  element.textContent = text;
  return element;
}

function showState(container, kind, message = "") {
  if (!kind) {
    container.replaceChildren();
    return;
  }
  const parts = [];
  if (kind === "loading") parts.push(div("spinner", ""));
  parts.push(div(kind === "error" ? "empty-title" : "muted", message));
  if (kind === "error") {
    parts.push(div("muted", "QuickBooks must be running with the company file open."));
  }
  container.replaceChildren(...parts);
}

function money(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value ?? "—");
  return number.toLocaleString(undefined, { style: "currency", currency: "USD" });
}

function time(iso) {
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime())
    ? iso
    : parsed.toLocaleString([], { dateStyle: "short", timeStyle: "short" });
}

function capitalise(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : "";
}

function setLink(ok, message) {
  const line = $("linkStatus");
  line.classList.toggle("ok", ok);
  line.classList.toggle("bad", !ok);
  $("linkText").textContent = message;
}

/** The banner is for failures only. tone: "warn" or "error". */
function note(message, tone = "error") {
  const element = $("importNote");
  element.hidden = false;
  element.textContent = message;
  element.classList.toggle("warn", tone === "warn");
  element.classList.toggle("error", tone === "error");
}

function hideNote() {
  $("importNote").hidden = true;
}

let toastTimer = null;
function toast(message, isError = false) {
  const element = $("toast");
  element.textContent = message;
  element.classList.toggle("error", isError);
  element.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("show"), 4200);
}

/* ═══ wiring ══════════════════════════════════════════════════════════════ */

function wire() {
  applyTheme(readTheme());
  $("themeToggle").addEventListener("change", (event) => {
    applyTheme(event.target.checked ? "dark" : "light", { animate: true });
  });

  applySidebar(sidebarCollapsed());
  $("sidebarToggle").addEventListener("click", () => {
    applySidebar(document.documentElement.dataset.sidebar !== "collapsed");
  });

  for (const item of document.querySelectorAll(".nav-item")) {
    item.addEventListener("click", () => goto(item.dataset.view));
  }
  for (const button of document.querySelectorAll("[data-goto]")) {
    button.addEventListener("click", () => goto(button.dataset.goto));
  }

  // "Refresh" re-downloads the sheet rather than just redrawing what we already have.
  $("refreshButton").addEventListener("click", () => fetchSheet("", true));

  // Clicking anywhere on a policy opens it, not just the pencil. One delegated
  // listener covers every row without wiring thousands of handlers.
  $("rows").addEventListener("click", (event) => {
    if (event.target.closest("input, button, a, label")) return;
    // Don't hijack a click that was really the end of a text selection.
    if (String(window.getSelection())) return;
    const row = event.target.closest("tr");
    if (row && row.dataset.id) openEditor(row.dataset.id);
  });

  const dialog = $("editDialog");
  $("editClose").addEventListener("click", closeEditor);
  $("editCancel").addEventListener("click", closeEditor);
  $("editSave").addEventListener("click", () => {
    const record = state.records.find((entry) => entry.source_id === state.editing);
    if (record) saveRow(record);
  });
  dialog.addEventListener("close", () => {
    state.editing = null;
  });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) closeEditor();   // click the backdrop to dismiss
  });

  const invoiceDialog = $("invoiceDialog");
  $("invoiceClose").addEventListener("click", closeInvoicePicker);
  $("invoiceCancel").addEventListener("click", closeInvoicePicker);
  $("invoiceCreate").addEventListener("click", createInvoice);
  invoiceDialog.addEventListener("close", () => {
    state.invoicing = null;
  });
  invoiceDialog.addEventListener("click", (event) => {
    if (event.target === invoiceDialog) closeInvoicePicker();
  });


  const reFilter = () => {
    renderTable();
    renderSelection();
  };
  $("sourceFilter").addEventListener("change", (event) => {
    state.source = event.target.value;
    reFilter();
  });
  $("dateFilter").addEventListener("change", (event) => {
    state.dateRange = event.target.value;
    $("customRange").hidden = state.dateRange !== "custom";
    reFilter();
  });
  setupDateField("dateFrom", (iso) => {
    state.dateFrom = iso;
    reFilter();
  });
  setupDateField("dateTo", (iso) => {
    state.dateTo = iso;
    reFilter();
  });
  $("statusRefresh").addEventListener("click", refresh);
  $("preflightRun").addEventListener("click", runPreflight);
  $("useCompany").addEventListener("click", useCompany);
  $("companyRescan").addEventListener("click", () => loadCompanies({ announce: true }));
  $("sourceSave").addEventListener("click", saveDataSource);
  $("bulkEditButton").addEventListener("click", openBulkEdit);
  $("bulkApply").addEventListener("click", applyBulkEdit);
  $("bulkCancel").addEventListener("click", () => $("bulkDialog").close());
  $("bulkClose").addEventListener("click", () => $("bulkDialog").close());
  document.querySelector('[data-lock-for="source"]').addEventListener("click", toggleSourceLock);
  setSourceLock(true);                   // always starts protected
  $("processButton").addEventListener("click", () => processSelected("create_customer"));
  $("invoiceButton").addEventListener("click", () => openInvoicePicker([...state.selected]));

  // Debounced: filtering 2,500+ policies and rebuilding the table on every
  // keystroke makes both the typing and the row animation stutter.
  let searchTimer = null;
  $("searchInput").addEventListener("input", (event) => {
    state.search = event.target.value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      renderTable();
      renderSelection();
    }, 140);
  });
  $("statusFilter").addEventListener("change", (event) => {
    state.status = event.target.value;
    renderTable();
    renderSelection();
  });
  $("invoiceFilter").addEventListener("change", (event) => {
    state.invoiceStatus = event.target.value;
    renderTable();
    renderSelection();
  });
  $("invoiceKindFilter").addEventListener("change", (event) => {
    state.invoiceKind = event.target.value;
    renderTable();
    renderSelection();
  });
  $("historyFilter").addEventListener("change", (event) => {
    state.historyFilter = event.target.value;
    renderActivity();
  });
  $("historyAction").addEventListener("change", (event) => {
    state.historyAction = event.target.value;
    renderActivity();
  });
  $("historyWhen").addEventListener("change", (event) => {
    state.historyWhen = event.target.value;
    renderActivity();
  });
  let historyTimer = null;
  $("historySearch").addEventListener("input", (event) => {
    state.historySearch = event.target.value;
    clearTimeout(historyTimer);
    historyTimer = setTimeout(renderActivity, 140);
  });
  $("historyClear").addEventListener("click", () => {
    state.historyFilter = "all";
    state.historyAction = "all";
    state.historyWhen = "all";
    state.historySearch = "";
    $("historyFilter").value = "all";
    $("historyAction").value = "all";
    $("historyWhen").value = "all";
    $("historySearch").value = "";
    renderActivity();
  });

  wireHorizontalKeys();
}

/* The policies table is far wider than the screen, so the arrow keys scroll it
   sideways - without needing to click into it first. */
const SCROLL_STEP = 160;   // roughly one column per press

function wireHorizontalKeys() {
  const KEYS = {
    ArrowRight: (el) => ({ left: SCROLL_STEP }),
    ArrowLeft: () => ({ left: -SCROLL_STEP }),
    Home: () => ({ left: -1e7 }),
    End: () => ({ left: 1e7 }),
  };

  document.addEventListener("keydown", (event) => {
    const step = KEYS[event.key];
    if (!step || event.ctrlKey || event.altKey || event.metaKey) return;
    if (state.view !== "sheet") return;                 // only on the policies table
    if ($("editDialog").open || $("invoiceDialog").open || $("askDialog").open) return;

    // Never steal keys from something the user is typing or choosing in.
    const active = document.activeElement;
    if (active && active.closest("input, textarea, select, [contenteditable]")) return;

    const scroller = $("sheetScroll");
    if (scroller.scrollWidth <= scroller.clientWidth) return;   // nothing to scroll

    event.preventDefault();
    scroller.scrollBy({
      ...step(scroller),
      // Smooth for a single press; instant while a key is held, so repeats
      // don't queue up behind each other and lag.
      behavior: event.repeat ? "auto" : "smooth",
    });
  });
}

wire();
goto("dashboard");
refresh();
startWatching();
