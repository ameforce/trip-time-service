"use strict";

/* ── State ────────────────────────────────────────── */
let _map = null;
let _markers = [];
let _endpointMarkers = { origin: null, dest: null };
let _routeGroup = null;
let _config = {
  naver_map_client_id: null,
  timezone: "Asia/Seoul",
  provider: "unknown",
  version: "v0.0.0.0",
};
let _currentMode = "arrival"; // "arrival" | "departure"

// 자동완성/최근/즐겨찾기에서 확정된 stable selection payload 저장
let _selectedOrigin = null;   // {lat, lon, display_name, address, coords_ready, selection_kind, canonical_query}
let _selectedDest = null;     // {lat, lon, display_name, address, coords_ready, selection_kind, canonical_query}
const _privateRouteEndpoints = { origin: false, dest: false };
let _mapCenteredOnPrivateRoute = false;
const DEFAULT_MAP_CENTER = [37.5665, 126.978];
const DEFAULT_MAP_ZOOM = 12;

// 자동완성 상태
let _acTimerOrigin = null;
let _acTimerDest = null;
let _acActiveIdx = { origin: -1, dest: -1 };
const AUTOCOMPLETE_DEBOUNCE_MS = 120;
const AUTOCOMPLETE_FETCH_TIMEOUT_MS = 45000;
const AUTOCOMPLETE_CACHE_TTL_MS = 5 * 60 * 1000;
const AUTOCOMPLETE_CACHE_MAX_KEYS = 160;
const AUTOCOMPLETE_MIN_QUERY_LENGTH = 2;
const COORDS_UNRESOLVED_ROUTE_MESSAGE =
  "좌표를 확인할 수 없어 경로를 조회할 수 없습니다. 다른 후보를 선택해 주세요.";
let _autocompleteWarmupQueued = false;
let _autocompleteCacheMap = {};
let _searchInProgress = false;
let _searchController = null;
let _routeInputRevision = 0;

// geocode 요청 URL → 좌표. 검색어와 지도 중심을 함께 사용한다.
const _geocodeCache = new Map();

// 후보 상세 tooltip portal 상태
let _candidateTooltipPortal = null;
let _candidateTooltipActiveBadge = null;
let _candidateTooltipHideTimer = null;
let _candidateTooltipGlobalBound = false;
let _analysisTooltipGlobalBound = false;
const CANDIDATE_TOOLTIP_HIDE_DELAY_MS = 180;

/* ── DOM refs ─────────────────────────────────────── */
const $origin = document.getElementById("origin");
const $destination = document.getElementById("destination");
const $swapBtn = document.getElementById("swap-btn");
const $tabArrival = document.getElementById("tab-arrival");
const $tabDeparture = document.getElementById("tab-departure");
const $datetimeLabel = document.getElementById("datetime-label");
const $datetimeInput = document.getElementById("datetime-input");
const $datetimeInputWrap = document.querySelector(".datetime-input-wrap");
const $datetimeInputTooltip = document.getElementById("datetime-input-tooltip");
const $searchBtn = document.getElementById("search-btn");
const $results = document.getElementById("results");
const $errorBox = document.getElementById("error-box");
const $errorMsg = document.getElementById("error-msg");
const $retrySearch = document.getElementById("retry-search");
const $searchStatus = document.getElementById("search-status");
const $loading = document.getElementById("loading");
const $mapEl = document.getElementById("map");
const $mapPlaceholder = document.getElementById("map-placeholder");
const $mapStatus = document.getElementById("map-status");
const $mapRouteNote = document.getElementById("map-route-note");
const $mobileToggle = document.getElementById("mobile-toggle");
const $sidebar = document.getElementById("sidebar");
const $originAC = document.getElementById("origin-ac");
const $destAC = document.getElementById("dest-ac");
const $datetimePicker = document.getElementById("datetime-picker");
const $datetimeToggle = document.getElementById("datetime-toggle");
const $datetimePanel = document.getElementById("datetime-panel");
const $datetimeYear = document.getElementById("datetime-year");
const $datetimeMonth = document.getElementById("datetime-month");
const $datetimeDay = document.getElementById("datetime-day");
const $datetimeCalendarTitle = document.getElementById("datetime-calendar-title");
const $datetimeCalendarDays = document.getElementById("datetime-calendar-days");
const $datetimePrevMonth = document.getElementById("datetime-prev-month");
const $datetimeNextMonth = document.getElementById("datetime-next-month");
const $datetimePeriod = document.getElementById("datetime-period");
const $datetimeHour = document.getElementById("datetime-hour");
const $datetimeMinute = document.getElementById("datetime-minute");
const $providerBadge = document.getElementById("provider-badge");
const $providerWarning = document.getElementById("provider-warning");
const $versionBadge = document.getElementById("version-badge");
const $sidebarBackdrop = document.getElementById("sidebar-backdrop");
const $sheetRestore = document.getElementById("sheet-restore");

if ($datetimePanel && $datetimePanel.parentElement !== document.body) {
  document.body.appendChild($datetimePanel);
}

let DATETIME_MINUTE_STEP = 10;
const MAX_AUTOCOMPLETE_WARMUP_QUERIES = 8;
let _datetimePickerState = {
  selected: null,
  viewYear: 0,
  viewMonth: 0,
  isOpen: false,
};
let _datetimeRefocusFromValidation = false;

/* ── Helpers ──────────────────────────────────────── */

function formatDuration(seconds) {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "-";
  var wholeSeconds = Math.round(seconds);
  var hours = Math.floor(wholeSeconds / 3600);
  var minutes = Math.floor((wholeSeconds % 3600) / 60);
  var remaining = wholeSeconds % 60;
  var parts = [];
  if (hours) parts.push(hours + "시간");
  if (minutes) parts.push(minutes + "분");
  if (remaining) parts.push(remaining + "초");
  return parts.join(" ") || "0분";
}

function formatDatetime(isoStr) {
  var date = parseIsoDatetime(isoStr);
  if (!date) return "-";
  var wall = toServiceWallDate(date).toISOString();
  return wall.slice(0, date.getUTCSeconds() ? 19 : 16).replace("T", " ");
}

function formatScorePercent(score) {
  if (typeof score !== "number" || !isFinite(score)) {
    return "-";
  }
  var normalized = Math.min(1, Math.max(0, score));
  return (normalized * 100).toFixed(1);
}

function formatArrivalDelta(recommendedArrivalIso, baselineArrivalIso) {
  var recommendedArrival = parseIsoDatetime(recommendedArrivalIso);
  var baselineArrival = parseIsoDatetime(baselineArrivalIso);
  if (!recommendedArrival || !baselineArrival) {
    return "-";
  }

  var diffSeconds = Math.round(
    (recommendedArrival.getTime() - baselineArrival.getTime()) / 1000
  );
  if (diffSeconds === 0) {
    return "동일";
  }
  var diffText = formatDuration(Math.abs(diffSeconds));
  return diffSeconds < 0 ? diffText + " 빠름" : diffText + " 늦음";
}

function escapeHtml(value) {
  var source = value == null ? "" : String(value);
  return source
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// The picker uses UTC fields to hold civil calendar values without applying
// the device zone. Only this boundary converts between civil values and instants.
let _serviceTimeFormatter;

function toServiceWallDate(instant) {
  if (!_serviceTimeFormatter) {
    _serviceTimeFormatter = new Intl.DateTimeFormat("en-GB", {
      timeZone: _config.timezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    });
  }
  var parts = {};
  _serviceTimeFormatter.formatToParts(instant).forEach(function (part) {
    parts[part.type] = Number(part.value);
  });
  return new Date(Date.UTC(
    parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, parts.second
  ));
}

function resolveServiceInstant(wallDate, now) {
  var wallTime = wallDate.getTime();
  // Either side of a DST transition can supply the applicable offset.
  // Skip DST gaps and choose the earliest upcoming occurrence of a repeated time.
  var instants = [-24, 24].map(function (hours) {
    var sample = new Date(wallTime + hours * 3600000);
    var offset = toServiceWallDate(sample).getTime() - sample.getTime();
    return wallTime - offset;
  }).filter(function (instant) {
    return toServiceWallDate(new Date(instant)).getTime() === wallTime;
  });
  instants.sort(function (a, b) { return a - b; });
  if (!instants.length) return null;
  return new Date(instants.find(function (instant) { return instant > now; }) ?? instants[0]);
}

function parseIsoDatetime(isoStr) {
  if (!isoStr) {
    return null;
  }
  var parsed = new Date(isoStr);
  if (isNaN(parsed.getTime())) {
    return null;
  }
  return parsed;
}

function toGoogleCalendarUtcToken(isoStr) {
  var dateObj = parseIsoDatetime(isoStr);
  if (!dateObj) {
    return null;
  }
  var year = String(dateObj.getUTCFullYear()).padStart(4, "0");
  var month = String(dateObj.getUTCMonth() + 1).padStart(2, "0");
  var day = String(dateObj.getUTCDate()).padStart(2, "0");
  var hours = String(dateObj.getUTCHours()).padStart(2, "0");
  var minutes = String(dateObj.getUTCMinutes()).padStart(2, "0");
  var seconds = String(dateObj.getUTCSeconds()).padStart(2, "0");
  return year + month + day + "T" + hours + minutes + seconds + "Z";
}

function resolveCalendarEventRange(startIso, endIso, durationSeconds) {
  var startDate = parseIsoDatetime(startIso);
  if (!startDate) {
    return null;
  }

  var endDate = parseIsoDatetime(endIso);
  var normalizedDuration = Number(durationSeconds);
  if (
    !endDate &&
    isFinite(normalizedDuration) &&
    normalizedDuration > 0
  ) {
    endDate = new Date(startDate.getTime() + normalizedDuration * 1000);
  }

  if (!endDate || endDate.getTime() <= startDate.getTime()) {
    if (isFinite(normalizedDuration) && normalizedDuration > 0) {
      endDate = new Date(startDate.getTime() + normalizedDuration * 1000);
    } else {
      endDate = new Date(startDate.getTime() + 30 * 60 * 1000);
    }
  }

  return {
    startIso: startDate.toISOString(),
    endIso: endDate.toISOString(),
  };
}

function buildGoogleCalendarTemplateUrl(eventPayload) {
  if (!eventPayload) {
    return null;
  }
  var range = resolveCalendarEventRange(
    eventPayload.startIso,
    eventPayload.endIso,
    eventPayload.durationSeconds
  );
  if (!range) {
    return null;
  }

  var startToken = toGoogleCalendarUtcToken(range.startIso);
  var endToken = toGoogleCalendarUtcToken(range.endIso);
  if (!startToken || !endToken) {
    return null;
  }

  var queryParts = [
    "action=TEMPLATE",
    "text=" + encodeURIComponent(eventPayload.title || "Trip Time 일정"),
    "dates=" + encodeURIComponent(startToken + "/" + endToken),
    "ctz=" + encodeURIComponent(_config.timezone),
  ];
  if (eventPayload.details) {
    queryParts.push("details=" + encodeURIComponent(eventPayload.details));
  }
  if (eventPayload.location) {
    queryParts.push("location=" + encodeURIComponent(eventPayload.location));
  }
  return "https://calendar.google.com/calendar/render?" + queryParts.join("&");
}

function buildCalendarEventPayload(options) {
  if (!options) {
    return null;
  }
  var route = options.route || {};
  var origin = route.origin || "-";
  var destination = route.destination || "-";
  var label = options.label || "이동 일정";
  var detailLines = [
    "Trip Time " + label,
    "출발: " + formatDatetime(options.departureIso),
    "도착: " + formatDatetime(options.arrivalIso),
    "출발지: " + origin,
    "도착지: " + destination,
    "희망 도착: " + formatDatetime(options.desiredArrivalIso),
    "예상 소요시간: " + formatDuration(options.durationSeconds),
  ];
  return {
    title: "[Trip Time] " + label + " - " + origin + " -> " + destination,
    startIso: options.departureIso,
    endIso: options.arrivalIso,
    durationSeconds: options.durationSeconds,
    details: detailLines.join("\n"),
    location: destination,
  };
}

function buildCalendarActionLink(label, href, variantClass) {
  var className = "calendar-action-btn " + (variantClass || "");
  if (!href) {
    return (
      '<span class="' +
      className +
      ' is-disabled" aria-disabled="true">' +
      escapeHtml(label) +
      "</span>"
    );
  }
  return (
    '<a class="' +
    className +
    '" href="' +
    escapeHtml(href) +
    '" target="_blank" rel="noopener noreferrer">' +
    escapeHtml(label) +
    "</a>"
  );
}

function resolveProviderName(provider) {
  return (provider || _config.provider || "unknown").toLowerCase();
}

function isMockProvider(provider) {
  return resolveProviderName(provider) === "mock";
}

function buildProviderNoticeCard(provider) {
  return "";
}

function setProviderName(provider) {
  return;
}

function setVersionBadge(versionText) {
  if (!$versionBadge) {
    return;
  }
  var normalized = "v0.0.0.0";
  if (typeof versionText === "string" && versionText.trim()) {
    normalized = versionText.trim();
  }
  $versionBadge.textContent = normalized;
  $versionBadge.setAttribute("title", "배포 버전: " + normalized);
}

/**
 * 현재 시각을 설정된 분 단위로 올림(ceil).
 * 예: 10분 단위에서는 18:31 → 18:40, 18:40 → 18:40, 18:41 → 18:50
 * 네이버 지도는 과거 시간 조회 불가이므로 항상 미래 시간을 반환.
 */
function defaultDatetimeValue() {
  var now = Date.now();
  var nowWall = toServiceWallDate(new Date(now));
  var step = DATETIME_MINUTE_STEP;
  var minute = nowWall.getUTCMinutes();
  nowWall.setUTCMinutes(Math.floor(minute / step) * step + step, 0, 0);
  var instant = resolveServiceInstant(nowWall, now);
  if (!instant || instant.getTime() <= now) {
    return toWallIsoMinutes(toServiceWallDate(new Date(now + step * 60000)));
  }
  return toWallIsoMinutes(nowWall);
}

function toWallIsoMinutes(wallDate) {
  return wallDate.toISOString().slice(0, 16);
}

function parseWallIsoMinutes(value) {
  if (!value) {
    return null;
  }
  var normalized = value.trim().replace(" ", "T");
  var match = normalized.match(
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/
  );
  if (!match) {
    return null;
  }
  var year = parseInt(match[1], 10);
  var month = parseInt(match[2], 10);
  var day = parseInt(match[3], 10);
  var hour = parseInt(match[4], 10);
  var minute = parseInt(match[5], 10);
  return buildValidatedDate(year, month, day, hour, minute);
}

function buildValidatedDate(year, month, day, hour, minute) {
  var parsed = new Date(Date.UTC(year, month - 1, day, hour, minute));

  if (
    parsed.getUTCFullYear() !== year ||
    parsed.getUTCMonth() !== month - 1 ||
    parsed.getUTCDate() !== day ||
    parsed.getUTCHours() !== hour ||
    parsed.getUTCMinutes() !== minute
  ) {
    return null;
  }
  return parsed;
}

function parseFlexibleDatetimeInput(value, fallbackDate) {
  if (!value) {
    return null;
  }

  var strict = parseWallIsoMinutes(value);
  if (strict) {
    return strict;
  }

  var trimmed = value.trim();
  if (!trimmed) {
    return null;
  }

  var base =
    fallbackDate && !isNaN(fallbackDate.getTime())
      ? fallbackDate
      : toServiceWallDate(new Date());
  var year;
  var month;
  var day;
  var hour = base.getUTCHours();
  var minute = base.getUTCMinutes();

  var digitsOnly = trimmed.replace(/\D/g, "");
  if (digitsOnly.length === 12 || digitsOnly.length === 8) {
    year = parseInt(digitsOnly.slice(0, 4), 10);
    month = parseInt(digitsOnly.slice(4, 6), 10);
    day = parseInt(digitsOnly.slice(6, 8), 10);
    if (digitsOnly.length === 12) {
      hour = parseInt(digitsOnly.slice(8, 10), 10);
      minute = parseInt(digitsOnly.slice(10, 12), 10);
    }
    return buildValidatedDate(year, month, day, hour, minute);
  }

  var normalized = trimmed.replace("T", " ");
  var match = normalized.match(
    /^(\d{4})[.\-/]?(\d{1,2})[.\-/]?(\d{1,2})(?:\s+(\d{1,2})(?::?(\d{1,2}))?)?$/
  );
  if (!match) {
    return null;
  }
  year = parseInt(match[1], 10);
  month = parseInt(match[2], 10);
  day = parseInt(match[3], 10);
  if (match[4]) {
    hour = parseInt(match[4], 10);
  }
  if (match[5]) {
    minute = parseInt(match[5], 10);
  }
  return buildValidatedDate(year, month, day, hour, minute);
}

function clampNumber(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function getSelectedDatetimeOrNow() {
  if (_datetimePickerState.selected) {
    return new Date(_datetimePickerState.selected.getTime());
  }
  var parsed = parseFlexibleDatetimeInput($datetimeInput.value, toServiceWallDate(new Date()));
  if (parsed) {
    return parsed;
  }
  return toServiceWallDate(new Date());
}

function syncDatetimeDateFields() {
  if (!$datetimeYear || !$datetimeMonth || !$datetimeDay) {
    return;
  }
  var selected = getSelectedDatetimeOrNow();
  $datetimeYear.value = String(selected.getUTCFullYear());
  $datetimeMonth.value = String(selected.getUTCMonth() + 1).padStart(2, "0");
  $datetimeDay.value = String(selected.getUTCDate()).padStart(2, "0");
}

function syncDatetimeTimeFields() {
  if (!$datetimePeriod || !$datetimeHour || !$datetimeMinute) {
    return;
  }
  var selected = getSelectedDatetimeOrNow();
  var hours24 = selected.getUTCHours();
  var period = hours24 >= 12 ? "pm" : "am";
  var hours12 = hours24 % 12;
  if (hours12 === 0) {
    hours12 = 12;
  }
  $datetimePeriod.value = period;
  $datetimeHour.value = String(hours12).padStart(2, "0");
  $datetimeMinute.value = String(selected.getUTCMinutes()).padStart(2, "0");
}

function renderDatetimeCalendar() {
  if (!$datetimeCalendarTitle || !$datetimeCalendarDays) {
    return;
  }

  var viewYear = _datetimePickerState.viewYear;
  var viewMonth = _datetimePickerState.viewMonth;
  var title = viewYear + "년 " + String(viewMonth + 1).padStart(2, "0") + "월";
  // Keep the day buttons mounted when focus leaves a date editor mid-click.
  if ($datetimeCalendarTitle.textContent !== title) {
    $datetimeCalendarTitle.textContent = title;
    var firstWeekday = new Date(Date.UTC(viewYear, viewMonth, 1)).getUTCDay();
    var daysInMonth = new Date(Date.UTC(viewYear, viewMonth + 1, 0)).getUTCDate();
    var html = '<span class="datetime-day-empty"></span>'.repeat(firstWeekday);
    for (var day = 1; day <= daysInMonth; day++) {
      html += '<button class="datetime-day-btn" type="button" data-day="' + day +
        '">' + day + "</button>";
    }
    $datetimeCalendarDays.innerHTML = html;
  }
  var selectedDay = toWallIsoMinutes(getSelectedDatetimeOrNow()).slice(0, 10);
  var today = toWallIsoMinutes(toServiceWallDate(new Date())).slice(0, 10);
  $datetimeCalendarDays.querySelectorAll(".datetime-day-btn").forEach(function (button) {
    var date = new Date(Date.UTC(viewYear, viewMonth, Number(button.dataset.day)));
    var day = toWallIsoMinutes(date).slice(0, 10);
    button.classList.toggle("is-selected", day === selectedDay);
    button.classList.toggle("is-today", day === today);
    button.setAttribute("aria-pressed", String(day === selectedDay));
    button.disabled = day < today;
  });
}

function renderDatetimePicker() {
  syncDatetimeDateFields();
  syncDatetimeTimeFields();
  renderDatetimeCalendar();
  if (_datetimePickerState.isOpen) {
    window.requestAnimationFrame(positionDatetimePanel);
  }
}

function setDatetimeFromDate(dateObj, options) {
  if (!dateObj || isNaN(dateObj.getTime())) {
    return;
  }
  var opts = options || {};
  _datetimePickerState.selected = dateObj;
  if (!opts.keepView) {
    _datetimePickerState.viewYear = dateObj.getUTCFullYear();
    _datetimePickerState.viewMonth = dateObj.getUTCMonth();
  }
  var value = toWallIsoMinutes(dateObj);
  if ($datetimeInput.value !== value) {
    $datetimeInput.value = value;
    invalidateSearchOnlyState();
  }
  renderDatetimePicker();
}

function syncDatetimePickerFromInput(options) {
  var fallbackDate =
    _datetimePickerState.selected ||
    parseWallIsoMinutes(defaultDatetimeValue()) ||
    new Date();
  var parsed = parseFlexibleDatetimeInput($datetimeInput.value, fallbackDate);
  if (!parsed) {
    parsed = parseWallIsoMinutes(defaultDatetimeValue());
  }
  if (!parsed) {
    return false;
  }
  setDatetimeFromDate(parsed, options || {});
  return true;
}

function syncDatetimeStateFromInputValue() {
  var fallbackDate =
    _datetimePickerState.selected ||
    parseWallIsoMinutes(defaultDatetimeValue()) ||
    new Date();
  var parsed = parseFlexibleDatetimeInput($datetimeInput.value, fallbackDate);
  if (!parsed) {
    return false;
  }

  _datetimePickerState.selected = new Date(parsed.getTime());
  _datetimePickerState.viewYear = parsed.getUTCFullYear();
  _datetimePickerState.viewMonth = parsed.getUTCMonth();
  if (_datetimePickerState.isOpen) {
    renderDatetimePicker();
  }
  return true;
}

function hideDatetimeInputTooltip() {
  if ($datetimeInputWrap) {
    $datetimeInputWrap.classList.remove("has-error");
    $datetimeInputWrap.classList.remove("tooltip-above");
  }
  if ($datetimeInputTooltip) {
    $datetimeInputTooltip.textContent = "";
    $datetimeInputTooltip.classList.add("hidden");
  }
}

function positionDatetimeInputTooltip() {
  if (
    !$datetimeInputWrap ||
    !$datetimeInputTooltip ||
    $datetimeInputTooltip.classList.contains("hidden")
  ) {
    return;
  }

  $datetimeInputWrap.classList.remove("tooltip-above");
  var margin = 8;
  var wrapRect = $datetimeInputWrap.getBoundingClientRect();
  var tooltipRect = $datetimeInputTooltip.getBoundingClientRect();
  var hasAboveSpace = wrapRect.top - margin >= tooltipRect.height + 6;
  if (_datetimePickerState.isOpen && hasAboveSpace) {
    $datetimeInputWrap.classList.add("tooltip-above");
  }
}

function focusDatetimeProblemSegment(segment) {
  $datetimeInput.focus();
  var start = segment === "time" ? 11 : 0;
  var end = segment === "time" ? 16 : 10;
  try {
    $datetimeInput.setSelectionRange(start, end);
    return;
  } catch (_) {
    // datetime-local은 selection range를 지원하지 않을 수 있다.
  }

  openDatetimePanel();
  if (segment === "date" && $datetimeDay) {
    $datetimeDay.focus();
    if (typeof $datetimeDay.select === "function") {
      $datetimeDay.select();
    }
    return;
  }
  if ($datetimeMinute) {
    $datetimeMinute.focus();
  }
}

function showDatetimeInputTooltip(message, segment) {
  if ($datetimeInputWrap) {
    $datetimeInputWrap.classList.add("has-error");
  }
  if ($datetimeInputTooltip) {
    $datetimeInputTooltip.textContent = message;
    $datetimeInputTooltip.classList.remove("hidden");
  }
  positionDatetimeInputTooltip();
  _datetimeRefocusFromValidation = true;
  setTimeout(function () {
    focusDatetimeProblemSegment(segment);
  }, 0);
}

function validateDatetimeAgainstNowOnBlur() {
  var fallbackDate =
    _datetimePickerState.selected ||
    parseWallIsoMinutes(defaultDatetimeValue()) ||
    new Date();
  var selected = parseFlexibleDatetimeInput($datetimeInput.value, fallbackDate);
  if (!selected) {
    hideDatetimeInputTooltip();
    return true;
  }

  var instant = resolveServiceInstant(selected, Date.now());
  if (!instant) {
    showDatetimeInputTooltip("서비스 시간대에 존재하지 않는 시각입니다.", "time");
    return false;
  }
  if (instant.getTime() > Date.now()) {
    hideDatetimeInputTooltip();
    return true;
  }

  var nowWall = toServiceWallDate(new Date());
  var segment = toWallIsoMinutes(selected).slice(0, 10) < toWallIsoMinutes(nowWall).slice(0, 10)
    ? "date" : "time";
  showDatetimeInputTooltip(
    "현재 시점 이전으로는 조회를 할 수 없습니다.",
    segment
  );
  return false;
}

function moveDatetimeMonth(delta) {
  var base = new Date(Date.UTC(
    _datetimePickerState.viewYear,
    _datetimePickerState.viewMonth + delta,
    1
  ));
  _datetimePickerState.viewYear = base.getUTCFullYear();
  _datetimePickerState.viewMonth = base.getUTCMonth();
  renderDatetimeCalendar();
}

function applyDatetimeDateFields() {
  var year = parseInt($datetimeYear.value, 10);
  var month = parseInt($datetimeMonth.value, 10);
  var day = parseInt($datetimeDay.value, 10);
  if (
    !Number.isFinite(year) ||
    !Number.isFinite(month) ||
    !Number.isFinite(day)
  ) {
    syncDatetimeDateFields();
    return;
  }
  year = clampNumber(year, 2000, 2099);
  month = clampNumber(month, 1, 12);
  var maxDay = new Date(Date.UTC(year, month, 0)).getUTCDate();
  day = clampNumber(day, 1, maxDay);

  var selected = getSelectedDatetimeOrNow();
  var nextDate = new Date(Date.UTC(
    year,
    month - 1,
    day,
    selected.getUTCHours(),
    selected.getUTCMinutes(),
    0,
    0
  ));
  setDatetimeFromDate(nextDate);
}

function applyDatetimeTimeFields() {
  var period = $datetimePeriod.value;
  var hour12 = parseInt($datetimeHour.value, 10);
  var minute = parseInt($datetimeMinute.value, 10);
  if (
    !Number.isFinite(hour12) ||
    !Number.isFinite(minute) ||
    (period !== "am" && period !== "pm")
  ) {
    syncDatetimeTimeFields();
    return;
  }

  var hour24 = hour12 % 12;
  if (period === "pm") {
    hour24 += 12;
  }

  var selected = getSelectedDatetimeOrNow();
  var nextDate = new Date(Date.UTC(
    selected.getUTCFullYear(),
    selected.getUTCMonth(),
    selected.getUTCDate(),
    hour24,
    minute,
    0,
    0
  ));
  setDatetimeFromDate(nextDate, { keepView: true });
}

function positionDatetimePanel() {
  if (!$datetimePanel || !$datetimePicker || !_datetimePickerState.isOpen) {
    return;
  }
  positionDatetimeInputTooltip();
  var viewport = $sidebar.classList.contains("kb-tracking") ? window.visualViewport : null;
  var margin = 8;
  var minTop = (viewport ? viewport.offsetTop : 0) + margin;
  $datetimePanel.style.maxHeight = viewport ? Math.max(0, viewport.height - 2 * margin) + "px" : "";
  var anchorRect = $datetimePicker.getBoundingClientRect();
  var panelRect = $datetimePanel.getBoundingClientRect();
  var gap = 8;
  var viewportWidth = window.innerWidth;
  var viewportBottom = viewport ? viewport.offsetTop + viewport.height : window.innerHeight;
  var tooltipExtraGap = 0;
  if (
    $datetimeInputTooltip &&
    !$datetimeInputTooltip.classList.contains("hidden") &&
    $datetimeInputWrap &&
    !$datetimeInputWrap.classList.contains("tooltip-above")
  ) {
    tooltipExtraGap = $datetimeInputTooltip.getBoundingClientRect().height + 8;
  }

  var left = anchorRect.left;
  var maxLeft = viewportWidth - margin - panelRect.width;
  if (left > maxLeft) {
    left = maxLeft;
  }
  if (left < margin) {
    left = margin;
  }

  var top = anchorRect.bottom + gap + tooltipExtraGap;
  var maxTop = viewportBottom - margin - panelRect.height;
  if (top > maxTop) top = anchorRect.top - panelRect.height - gap;
  top = Math.max(minTop, Math.min(top, maxTop));

  $datetimePanel.style.left = Math.round(left) + "px";
  $datetimePanel.style.top = Math.round(top) + "px";
}

function openDatetimePanel() {
  if (!$datetimePanel) {
    return;
  }
  renderDatetimePicker();
  _datetimePickerState.isOpen = true;
  $datetimePanel.classList.remove("hidden");
  $datetimeInput.setAttribute("aria-expanded", "true");
  positionDatetimeInputTooltip();
  positionDatetimePanel();
  window.requestAnimationFrame(positionDatetimePanel);
}

function closeDatetimePanel() {
  if (!$datetimePanel) {
    return;
  }
  _datetimePickerState.isOpen = false;
  $datetimePanel.classList.add("hidden");
  $datetimeInput.setAttribute("aria-expanded", "false");
  positionDatetimeInputTooltip();
}

function toggleDatetimePanel() {
  if (_datetimePickerState.isOpen) {
    closeDatetimePanel();
  } else {
    renderDatetimePicker();
    openDatetimePanel();
  }
}

function sanitizeDateFieldInput(event) {
  var input = event.target;
  input.value = input.value.replace(/\D/g, "");
}

function bindDatetimeDateField($input) {
  $input.addEventListener("input", function (event) {
    sanitizeDateFieldInput(event);
    invalidateSearchOnlyState();
  });
  $input.addEventListener("blur", applyDatetimeDateFields);
  $input.addEventListener("change", applyDatetimeDateFields);
  $input.addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
      event.preventDefault();
      applyDatetimeDateFields();
    }
  });
}

function populateDatetimeTimeOptions() {
  if (!$datetimeHour || !$datetimeMinute) {
    return;
  }
  var hourOptions = "";
  for (var hour = 1; hour <= 12; hour++) {
    var hourText = String(hour).padStart(2, "0");
    hourOptions +=
      '<option value="' + hourText + '">' + hourText + "</option>";
  }
  $datetimeHour.innerHTML = hourOptions;

  var minuteOptions = "";
  for (var minute = 0; minute < 60; minute++) {
    var minuteText = String(minute).padStart(2, "0");
    minuteOptions +=
      '<option value="' + minuteText + '">' + minuteText + "</option>";
  }
  $datetimeMinute.innerHTML = minuteOptions;
}

function initDatetimePicker() {
  if (
    !$datetimePicker ||
    !$datetimePanel ||
    !$datetimeToggle ||
    !$datetimePrevMonth ||
    !$datetimeNextMonth ||
    !$datetimeCalendarDays ||
    !$datetimePeriod ||
    !$datetimeHour ||
    !$datetimeMinute
  ) {
    return;
  }

  populateDatetimeTimeOptions();
  syncDatetimePickerFromInput();

  $datetimeToggle.addEventListener("click", function (event) {
    event.preventDefault();
    event.stopPropagation();
    toggleDatetimePanel();
  });

  $datetimeInput.addEventListener("keydown", function (event) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      openDatetimePanel();
    } else if (event.key === "Escape") {
      closeDatetimePanel();
    }
  });
  $datetimeInput.addEventListener("focus", function () {
    if (_datetimeRefocusFromValidation) {
      _datetimeRefocusFromValidation = false;
      return;
    }
    hideDatetimeInputTooltip();
  });
  $datetimeInput.addEventListener("blur", function () {
    setTimeout(function () {
      if ($datetimePicker.contains(document.activeElement) ||
          $datetimePanel.contains(document.activeElement)) {
        return;
      }
      validateDatetimeAgainstNowOnBlur();
    }, 0);
  });

  $datetimePrevMonth.addEventListener("click", function () {
    moveDatetimeMonth(-1);
  });
  $datetimeNextMonth.addEventListener("click", function () {
    moveDatetimeMonth(1);
  });

  $datetimeCalendarDays.addEventListener("pointerdown", function (event) {
    var field = document.activeElement;
    if (event.target.closest(".datetime-day-btn:not(:disabled)") &&
        field.matches("input") && ($datetimePicker.contains(field) || $datetimePanel.contains(field))) {
      // Select the tapped date before blur can commit an editor's old value or move the panel.
      event.preventDefault();
    }
  });

  $datetimeCalendarDays.addEventListener("click", function (event) {
    var $btn = event.target.closest(".datetime-day-btn");
    if (!$btn || $btn.disabled) {
      return;
    }
    var day = parseInt($btn.getAttribute("data-day"), 10);
    if (!Number.isFinite(day)) {
      return;
    }
    var selected = getSelectedDatetimeOrNow();
    var nextDate = new Date(Date.UTC(
      _datetimePickerState.viewYear,
      _datetimePickerState.viewMonth,
      day,
      selected.getUTCHours(),
      selected.getUTCMinutes()
    ));
    setDatetimeFromDate(nextDate);
    $btn.focus({ preventScroll: true });
  });

  bindDatetimeDateField($datetimeYear);
  bindDatetimeDateField($datetimeMonth);
  bindDatetimeDateField($datetimeDay);

  $datetimePeriod.addEventListener("change", applyDatetimeTimeFields);
  $datetimeHour.addEventListener("change", applyDatetimeTimeFields);
  $datetimeMinute.addEventListener("change", applyDatetimeTimeFields);

  document.addEventListener("pointerdown", function (event) {
    if (!_datetimePickerState.isOpen) {
      return;
    }
    if ($datetimePicker.contains(event.target)) {
      return;
    }
    if ($datetimePanel && $datetimePanel.contains(event.target)) {
      return;
    }
    closeDatetimePanel();
  });

  document.addEventListener("keydown", function (event) {
    if (!_datetimePickerState.isOpen) {
      return;
    }
    if (event.key === "Escape") {
      closeDatetimePanel();
    }
  });

  window.addEventListener("resize", function () {
    if (_datetimePickerState.isOpen) {
      positionDatetimePanel();
    }
  });
  $sidebar.addEventListener(
    "scroll",
    function () {
      if (_datetimePickerState.isOpen) {
        positionDatetimePanel();
      }
    },
    { passive: true }
  );
}

function showError(msg, options) {
  var opts = options || {};
  $errorBox.classList.remove("hidden");
  $errorMsg.textContent = msg;
  $searchStatus.textContent = msg;
  $retrySearch.classList.toggle("hidden", !opts.retry);
  if (!opts.keepResults) $results.classList.add("hidden");
}

function hideError() {
  $errorBox.classList.add("hidden");
  $errorMsg.textContent = "";
  $searchStatus.textContent = "";
  $retrySearch.classList.add("hidden");
}

function showLoading() {
  $loading.classList.remove("hidden");
  $searchStatus.textContent = "출발지와 도착지의 교통 정보를 조회하고 있습니다.";
  $searchBtn.disabled = true;
}

function hideLoading() {
  $loading.classList.add("hidden");
  $searchBtn.disabled = false;
}

function timeAgo(ts) {
  var diff = Date.now() - ts;
  var min = Math.floor(diff / 60000);
  if (min < 1) return "방금";
  if (min < 60) return min + "분 전";
  var hr = Math.floor(min / 60);
  if (hr < 24) return hr + "시간 전";
  var d = Math.floor(hr / 24);
  return d + "일 전";
}

/* ── localStorage Helpers ─────────────────────────── */
var RECENT_KEY = "tts_recent_searches";
var FAV_KEY = "tts_favorites";
var RECENT_MAX = 10;
var FAV_MAX = 20;

function loadJSON(key, fallback) {
  try {
    var v = localStorage.getItem(key);
    return v ? JSON.parse(v) : fallback;
  } catch (e) {
    return fallback;
  }
}

function saveJSON(key, val) {
  try {
    localStorage.setItem(key, JSON.stringify(val));
  } catch (e) { /* quota exceeded */ }
}

function addRecentSearch(origin, dest, originCoords, destCoords) {
  if (hasPrivateRouteInput()) return;
  var list = loadJSON(RECENT_KEY, []);
  // 중복 제거
  list = list.filter(function (r) {
    return !(r.origin === origin && r.destination === dest);
  });
  list.unshift({
    origin: origin,
    destination: dest,
    origin_coords: originCoords,
    destination_coords: destCoords,
    ts: Date.now(),
  });
  if (list.length > RECENT_MAX) list = list.slice(0, RECENT_MAX);
  saveJSON(RECENT_KEY, list);
  renderRecentSearches();
}

function getRecentSearches() {
  return loadJSON(RECENT_KEY, []);
}

function clearRecentSearches() {
  saveJSON(RECENT_KEY, []);
  renderRecentSearches();
}

function getFavorites() {
  var items = loadJSON(FAV_KEY, []);
  return Array.isArray(items) ? items : [];
}

/* ── Map (Leaflet + OpenStreetMap) ────────────────── */

function initMap() {
  function showFallback() {
    $mapStatus.textContent = "지도를 불러올 수 없습니다. 이동 시간 검색은 계속 사용할 수 있습니다.";
  }

  function mountMap() {
    try {
      _map = window.L.map($mapEl, { zoomControl: true }).setView(DEFAULT_MAP_CENTER, DEFAULT_MAP_ZOOM);
      window.L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
        maxZoom: 19,
      }).addTo(_map);
      $mapPlaceholder.classList.add("hidden");
    } catch (_) {
      _map = null;
      showFallback();
    }
  }

  if (window.L) {
    mountMap();
    return;
  }
  var timeout = window.setTimeout(showFallback, 5000);
  var script = document.createElement("script");
  script.src = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js";
  script.integrity = "sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=";
  script.crossOrigin = "anonymous";
  script.addEventListener("load", function () {
    window.clearTimeout(timeout);
    mountMap();
  });
  script.addEventListener("error", function () {
    window.clearTimeout(timeout);
    showFallback();
  });
  document.head.appendChild(script);
}

function clearMarkers() {
  _markers.forEach(function (m) { _map.removeLayer(m); });
  _markers = [];
  _endpointMarkers = { origin: null, dest: null };
  clearRouteGroup();
}

function syncMarkerList() {
  _markers = [];
  if (_endpointMarkers.origin) _markers.push(_endpointMarkers.origin);
  if (_endpointMarkers.dest) _markers.push(_endpointMarkers.dest);
}

function clearRouteGroup() {
  if (_routeGroup && _map) {
    _map.removeLayer(_routeGroup);
  }
  _routeGroup = null;
  $mapRouteNote.classList.add("hidden");
}

function clearEndpointMarker(endpoint) {
  if (!_map || !_endpointMarkers[endpoint]) return;
  _map.removeLayer(_endpointMarkers[endpoint]);
  _endpointMarkers[endpoint] = null;
  syncMarkerList();
}

function setEndpointMarker(endpoint, latlng, opts) {
  if (!_map) return;
  clearEndpointMarker(endpoint);
  _endpointMarkers[endpoint] = addMarker(latlng, opts);
  syncMarkerList();
}

function invalidateSearchState(options) {
  var clearMapMarkers = !options || options.clearMarkers !== false;
  _routeInputRevision += 1;
  if (_searchController) _searchController.abort();
  _searchController = null;
  _searchInProgress = false;
  hideCandidateTooltip(true);
  hideError();
  if (clearMapMarkers && _map) {
    clearMarkers();
  }
  $results.classList.add("hidden");
  $results.replaceChildren();
  hideLoading();
}

function invalidateRouteInputState() {
  var endpoint = arguments.length > 0 ? arguments[0] : null;
  if (endpoint === "origin" || endpoint === "dest") {
    invalidateSearchState({ clearMarkers: false });
    clearRouteGroup();
    clearEndpointMarker(endpoint);
    return;
  }
  invalidateSearchState({ clearMarkers: true });
}

function invalidateSearchOnlyState() {
  invalidateSearchState({ clearMarkers: false });
}

function addMarker(latlng, opts) {
  var label = opts.label || "";
  var icon = L.divIcon({
    className: "custom-marker",
    html:
      '<div class="marker-pin" style="' +
      "background:" + (opts.color || "#03C75A") + ";" +
      '">' +
      '<span class="marker-label">' + label + '</span>' +
      '</div>',
    iconSize: [36, 44],
    iconAnchor: [18, 44],
    popupAnchor: [0, -44],
  });
  var marker = L.marker(latlng, { icon: icon }).addTo(_map);
  _markers.push(marker);
  return marker;
}

/**
 * 주소 → 좌표 변환 (캐시 우선).
 * 자동완성 선택 좌표가 있으면 즉시 반환, 캐시 히트면 네트워크 생략.
 */
function geocodeQueryToCoords(query, signal) {
  var geocodeQuery = String(query || "").trim();
  if (!geocodeQuery) return Promise.resolve(null);
  var privateRoute = hasPrivateRouteInput();
  var url = "/api/geocode?q=" + encodeURIComponent(geocodeQuery) + buildMapCenterQueryString();
  var cached = privateRoute ? null : _geocodeCache.get(url);
  if (cached) return Promise.resolve(cached);
  return fetch(url, { signal: signal, cache: privateRoute ? "no-store" : "default" })
    .then(function (res) { return res.ok ? res.json() : null; })
    .then(function (data) {
      var coords = normalizeCoords(Array.isArray(data) ? data[0] : null);
      if (!coords) return null;
      if (!privateRoute) _geocodeCache.set(url, coords);
      if (!privateRoute && data[0].source) {
        console.info(
          "[geocode]",
          geocodeQuery,
          "source=" + data[0].source,
          "confidence=" + (data[0].confidence || "n/a")
        );
      }
      return coords;
    })
    .catch(function () { return null; });
}

function resolveCoords(address, selectedCoords, signal) {
  var normalizedSelectedCoords = normalizeCoords(selectedCoords);
  if (normalizedSelectedCoords) {
    return Promise.resolve(normalizedSelectedCoords);
  }
  if (hasStableSelection(selectedCoords) && selectedCoords.coords_ready === false) {
    return Promise.resolve(null);
  }
  return geocodeQueryToCoords(address, signal);
}

function resolveRouteCriticalCoords(address, selectedCoords, signal) {
  var normalizedSelectedCoords = normalizeCoords(selectedCoords);
  if (normalizedSelectedCoords) {
    return Promise.resolve(normalizedSelectedCoords);
  }
  if (hasStableSelection(selectedCoords)) {
    return geocodeQueryToCoords(getStableSearchQuery(address, selectedCoords), signal);
  }
  return resolveCoords(address, selectedCoords, signal);
}

function markSelectionCoordsResolved(selection, coords) {
  if (!hasStableSelection(selection) || !hasValidCoords(coords)) return selection;
  return Object.assign({}, selection, {
    lat: Number(coords.lat),
    lon: Number(coords.lon),
    coords_ready: true,
    source: selection.source || "geocode",
  });
}

function fitBounds() {
  var layers = _markers.slice();
  if (_routeGroup) {
    _routeGroup.eachLayer(function (l) { layers.push(l); });
  }
  if (layers.length === 0) return;
  if (hasPrivateRouteInput()) _mapCenteredOnPrivateRoute = true;
  if (layers.length === 1 && _markers.length === 1) {
    _map.setView(_markers[0].getLatLng(), 16, { animate: true });
    return;
  }
  var group = L.featureGroup(layers);
  _map.fitBounds(group.getBounds(), {
    padding: [80, 80],
    maxZoom: 17,
    animate: true,
  });
}

function fetchAndDrawRoute(lat1, lon1, lat2, lon2, isCurrentRoute, signal) {
  if (!_map) return Promise.resolve();
  if (typeof isCurrentRoute === "function" && !isCurrentRoute()) {
    return Promise.resolve();
  }
  var url = "/api/route?olat=" + lat1 + "&olon=" + lon1 +
            "&dlat=" + lat2 + "&dlon=" + lon2;
  return fetch(url, { signal: signal, cache: hasPrivateRouteInput() ? "no-store" : "default" })
    .then(function (res) { return res.json(); })
    .then(function (data) {
      if (typeof isCurrentRoute === "function" && !isCurrentRoute()) {
        return;
      }
      if (!data.routes || !data.routes.length) return;
      var route = data.routes[0];
      var geom = route.geometry;
      var border = L.geoJSON(geom, {
        style: { color: "#0D47A1", weight: 9, opacity: 0.45, lineCap: "round", lineJoin: "round" },
      });
      var fill = L.geoJSON(geom, {
        style: { color: "#2979FF", weight: 5, opacity: 0.9, lineCap: "round", lineJoin: "round" },
      });
      clearRouteGroup();
      _routeGroup = L.layerGroup([border, fill]).addTo(_map);
      var etaSource = /^naver(?:_|$)/.test(resolveProviderName(_config.provider)) ? "예상 시간은 네이버 지도 기준" :
        isMockProvider(_config.provider) ? "예상 시간은 시험 데이터 기준" : "";
      var pathSource = route.source === "e2e_fixture" ? "시험 데이터 참고 경로" : "OSRM 참고 경로";
      $mapRouteNote.textContent = etaSource ? pathSource + " / " + etaSource : pathSource;
      $mapRouteNote.classList.remove("hidden");
      fitBounds();
    })
    .catch(function (err) {
      if (err.name !== "AbortError") console.error("route failed:", err);
    });
}

/**
 * 지도에 마커 + 경로를 그리고 Promise 반환.
 * 자동완성에서 선택된 좌표가 있으면 재지오코딩 없이 직접 사용.
 */
function updateMapMarkersAsync(originText, destText) {
  clearMarkers();
  if (!_map) return Promise.resolve();

  var originSelection = _selectedOrigin;
  var destSelection = _selectedDest;
  var normalizedOriginText = String(originText || "").trim();
  var normalizedDestText = String(destText || "").trim();
  var isMarkerRefreshCurrent = function () {
    return (
      $origin.value.trim() === normalizedOriginText &&
      $destination.value.trim() === normalizedDestText &&
      _selectedOrigin === originSelection &&
      _selectedDest === destSelection
    );
  };
  var pOrigin = normalizedOriginText
    ? resolveRouteCriticalCoords(normalizedOriginText, originSelection)
    : Promise.resolve(null);
  var pDest = normalizedDestText
    ? resolveRouteCriticalCoords(normalizedDestText, destSelection)
    : Promise.resolve(null);

  return Promise.all([pOrigin, pDest]).then(function (results) {
    if (!isMarkerRefreshCurrent()) return;
    var originCoords = results[0];
    var destCoords = results[1];
    if (originCoords && hasStableSelection(originSelection) && !hasValidCoords(originSelection)) {
      originSelection = markSelectionCoordsResolved(originSelection, originCoords);
      _selectedOrigin = originSelection;
    }
    if (destCoords && hasStableSelection(destSelection) && !hasValidCoords(destSelection)) {
      destSelection = markSelectionCoordsResolved(destSelection, destCoords);
      _selectedDest = destSelection;
    }
    clearMarkers();
    if (originCoords) {
      setEndpointMarker("origin", [originCoords.lat, originCoords.lon], { color: "#03C75A", label: "출발" });
    }
    if (destCoords) {
      setEndpointMarker("dest", [destCoords.lat, destCoords.lon], { color: "#E53935", label: "도착" });
    }
    fitBounds();
    if (originCoords && destCoords) {
      return fetchAndDrawRoute(
        originCoords.lat, originCoords.lon,
        destCoords.lat, destCoords.lon,
        isMarkerRefreshCurrent
      );
    }
  });
}

/* ── Autocomplete ────────────────────────────────── */

function getAutocompleteTimer(timerKey) {
  if (timerKey === "origin") return _acTimerOrigin;
  if (timerKey === "dest") return _acTimerDest;
  return null;
}

function setAutocompleteTimer(timerKey, timerId) {
  if (timerKey === "origin") _acTimerOrigin = timerId;
  if (timerKey === "dest") _acTimerDest = timerId;
}

function clearAutocompleteTimer(timerKey) {
  var timerId = getAutocompleteTimer(timerKey);
  if (timerId) {
    clearTimeout(timerId);
    setAutocompleteTimer(timerKey, null);
  }
}

function buildMapCenterQueryString() {
  if (_mapCenteredOnPrivateRoute || !_map || typeof _map.getCenter !== "function") return "";
  try {
    var center = _map.getCenter();
    if (!center) return "";
    var centerLat = Number(center.lat);
    var centerLon = Number(center.lng);
    if (!isFinite(centerLat) || !isFinite(centerLon)) return "";
    return (
      "&center_lat=" +
      encodeURIComponent(centerLat.toFixed(6)) +
      "&center_lon=" +
      encodeURIComponent(centerLon.toFixed(6))
    );
  } catch (_) {
    return "";
  }
}

function hasValidCoords(coords) {
  if (!coords) return false;
  if (coords.lat === null || coords.lon === null) return false;
  if (coords.lat === undefined || coords.lon === undefined) return false;
  if (String(coords.lat).trim() === "" || String(coords.lon).trim() === "") return false;
  var lat = Number(coords.lat);
  var lon = Number(coords.lon);
  return isFinite(lat) && isFinite(lon);
}

function normalizeCoords(coords) {
  if (!hasValidCoords(coords)) return null;
  return {
    lat: Number(coords.lat),
    lon: Number(coords.lon),
  };
}

var ROAD_ADDRESS_CORE_RE = /^(.*?(?:번길|대로|로|길)\s*\d+(?:-\d+)?)(?:\s+.*)?$/;
var ROAD_ADDRESS_SEGMENT_RE = /([^\s,()]+(?:번길|대로|로|길))\s*(\d+(?:-\d+)?)/;

function trimToCoreRoadAddress(value) {
  var normalized = String(value || "").replace(/\s+/g, " ").trim();
  if (!normalized) return "";
  var segmentMatch = normalized.match(ROAD_ADDRESS_SEGMENT_RE);
  if (segmentMatch) {
    return (segmentMatch[1].trim() + " " + segmentMatch[2].trim()).trim();
  }
  var coreMatch = normalized.match(ROAD_ADDRESS_CORE_RE);
  if (!coreMatch) return "";
  return coreMatch[1].replace(/\s+/g, " ").trim();
}

function buildStableSelection(selection, options) {
  options = options || {};
  if (!selection && !options) return null;

  var displayName = String(
    (selection && selection.display_name) ||
      options.display_name ||
      (selection && selection.address) ||
      options.address ||
      ""
  ).trim();
  var address = String(
    (selection && selection.address) ||
      options.address ||
      displayName
  ).trim();
  var selectionKind = String(
    (selection && selection.selection_kind) ||
      options.selection_kind ||
      ""
  ).trim();
  if (!selectionKind) {
    selectionKind = address && address === displayName ? "address" : "poi";
  }
  var canonicalQuery = String(
    (selection && selection.canonical_query) ||
      options.canonical_query ||
      (selectionKind === "address" ? address : displayName || address)
  ).trim();
  var lat = selection && selection.lat !== undefined && selection.lat !== null && String(selection.lat).trim() !== ""
    ? Number(selection.lat)
    : null;
    var lon = selection && selection.lon !== undefined && selection.lon !== null && String(selection.lon).trim() !== ""
      ? Number(selection.lon)
      : null;
    var coordsReady = lat !== null && lon !== null && isFinite(lat) && isFinite(lon);
    if (!displayName && !address && !canonicalQuery) return null;

  return {
    lat: coordsReady ? lat : null,
    lon: coordsReady ? lon : null,
    display_name: displayName,
    address: address,
    coords_ready: coordsReady,
    selection_kind: selectionKind,
    canonical_query: canonicalQuery || address || displayName,
    source: (selection && selection.source) || options.source || null,
    confidence:
      selection && selection.confidence !== undefined
        ? selection.confidence
        : options.confidence !== undefined
          ? options.confidence
          : null,
  };
}

function hasStableSelection(selection) {
  if (!selection) return false;
  return !!String(
    selection.canonical_query ||
      selection.address ||
      selection.display_name ||
      ""
  ).trim();
}

function getStableSearchQuery(text, selection) {
  var fallback = String(text || "").trim();
  if (!hasStableSelection(selection) || hasValidCoords(selection)) {
    return fallback;
  }
  var canonical = String(selection.canonical_query || "").trim();
  return canonical || fallback;
}

function buildNamedPoiDisplayLabel(selection) {
  if (!hasStableSelection(selection)) return "";
  if (selection.coords_ready !== false) return "";
  if (String(selection.selection_kind || "").trim() !== "poi") return "";
  var displayName = String(selection.display_name || "").trim();
  var address = String(selection.address || "").trim();
  if (!displayName || !address || displayName === address) return "";
  if (trimToCoreRoadAddress(displayName)) return "";
  return displayName + " (" + address + ")";
}

function getSelectionDisplayText(text, selection) {
  var fallback = String(text || "").trim();
  var namedPoiLabel = buildNamedPoiDisplayLabel(selection);
  return namedPoiLabel || fallback;
}

function getSelectedState(timerKey) {
  if (timerKey === "origin") return _selectedOrigin;
  if (timerKey === "dest") return _selectedDest;
  return null;
}

function selectionRetainTexts(selection) {
  if (!hasStableSelection(selection)) return [];
  var candidates = [
    buildNamedPoiDisplayLabel(selection),
    String(selection.canonical_query || "").trim(),
    String(selection.display_name || "").trim(),
    String(selection.address || "").trim(),
  ];
  var seen = {};
  var retained = [];
  for (var i = 0; i < candidates.length; i++) {
    var text = candidates[i];
    if (!text || seen[text]) continue;
    seen[text] = true;
    retained.push(text);
  }
  return retained;
}

function shouldRetainSelectionOnInput(timerKey, text) {
  var selection = getSelectedState(timerKey);
  if (!hasStableSelection(selection)) return false;
  var normalized = String(text || "").trim();
  if (!normalized) return false;
  var retained = selectionRetainTexts(selection);
  for (var i = 0; i < retained.length; i++) {
    if (retained[i] === normalized) {
      return true;
    }
  }
  return false;
}

function getWarmupQuery(text, selection) {
  var rawText = String(text || "").trim();
  var canonical = hasStableSelection(selection)
    ? String(selection.canonical_query || "").trim()
    : "";
  var preferred = canonical || rawText;
  if (!preferred) return "";
  return trimToCoreRoadAddress(preferred) || preferred;
}

function hasTrailingHangulJamo(value) {
  return /[ㄱ-ㅎㅏ-ㅣ]$/.test(String(value || "").trim());
}

function buildAutocompleteCacheKey(query) {
  var normalized = (query || "").trim();
  try {
    normalized = normalized.normalize("NFC");
  } catch (_) {
    // Keep the raw trimmed value when the browser lacks normalize().
  }
  normalized = normalized.toLowerCase();
  if (_mapCenteredOnPrivateRoute || !_map || typeof _map.getCenter !== "function") {
    return normalized + "|nomap";
  }
  try {
    var center = _map.getCenter();
    if (!center) return normalized + "|nomap";
    var lat = Number(center.lat);
    var lon = Number(center.lng);
    if (!isFinite(lat) || !isFinite(lon)) return normalized + "|nomap";
    return normalized + "|" + lat.toFixed(6) + "|" + lon.toFixed(6);
  } catch (_) {
    return normalized + "|nomap";
  }
}

function getCachedAutocompleteItems(cacheKey) {
  var cached = _autocompleteCacheMap[cacheKey];
  if (!cached) return null;
  if (Date.now() - cached.ts > AUTOCOMPLETE_CACHE_TTL_MS) {
    delete _autocompleteCacheMap[cacheKey];
    return null;
  }
  if (!Array.isArray(cached.items) || cached.items.length === 0) {
    delete _autocompleteCacheMap[cacheKey];
    return null;
  }
  return cached.items;
}

function setCachedAutocompleteItems(cacheKey, items) {
  if (!Array.isArray(items) || items.length === 0) {
    delete _autocompleteCacheMap[cacheKey];
    return;
  }
  _autocompleteCacheMap[cacheKey] = {
    ts: Date.now(),
    items: items,
  };
  var keys = Object.keys(_autocompleteCacheMap);
  if (keys.length <= AUTOCOMPLETE_CACHE_MAX_KEYS) return;
  keys
    .sort(function (a, b) {
      return (_autocompleteCacheMap[a].ts || 0) - (_autocompleteCacheMap[b].ts || 0);
    })
    .slice(0, Math.max(0, keys.length - AUTOCOMPLETE_CACHE_MAX_KEYS))
    .forEach(function (key) {
      delete _autocompleteCacheMap[key];
    });
}

function collectWarmupQueries() {
  var candidates = [];
  var recents = getRecentSearches();
  var favorites = getFavorites();

  recents.forEach(function (entry) {
    var originSelection = buildStableSelection(entry && entry.origin_coords, {
      display_name: entry && entry.origin ? String(entry.origin) : "",
      address: entry && entry.origin ? String(entry.origin) : "",
      canonical_query: entry && entry.origin ? String(entry.origin) : "",
    });
    var destinationSelection = buildStableSelection(entry && entry.destination_coords, {
      display_name: entry && entry.destination ? String(entry.destination) : "",
      address: entry && entry.destination ? String(entry.destination) : "",
      canonical_query: entry && entry.destination ? String(entry.destination) : "",
    });
    var originQuery = getWarmupQuery(entry && entry.origin, originSelection);
    var destinationQuery = getWarmupQuery(entry && entry.destination, destinationSelection);
    if (originQuery) candidates.push(originQuery);
    if (destinationQuery) candidates.push(destinationQuery);
  });
  favorites.forEach(function (entry) {
    if (entry && entry.name) candidates.push(String(entry.name));
    var favoriteSelection = buildStableSelection(
      entry && entry.lat !== undefined && entry.lon !== undefined
        ? {
            lat: entry.lat,
            lon: entry.lon,
            display_name: entry.name || entry.address || "",
            address: entry.address || entry.name || "",
            canonical_query:
              trimToCoreRoadAddress(entry.address || "") ||
              entry.address ||
              entry.name ||
              "",
            selection_kind: "poi",
            source: "favorite",
          }
        : null,
      {
        display_name: entry && entry.name ? String(entry.name) : "",
        address: entry && entry.address ? String(entry.address) : "",
        canonical_query:
          trimToCoreRoadAddress(entry && entry.address ? String(entry.address) : "") ||
          (entry && entry.address ? String(entry.address) : "") ||
          (entry && entry.name ? String(entry.name) : ""),
        selection_kind: "poi",
        source: "favorite",
      }
    );
    var favoriteQuery = getWarmupQuery(entry && entry.address, favoriteSelection);
    if (favoriteQuery) candidates.push(favoriteQuery);
  });

  var deduped = [];
  var seen = {};
  for (var i = 0; i < candidates.length; i++) {
    var query = (candidates[i] || "").trim();
    if (!query) continue;
    var compact = query.replace(/\s+/g, "").toLowerCase();
    if (compact.length < 2) continue;
    if (seen[compact]) continue;
    seen[compact] = true;
    deduped.push(query);
    if (deduped.length >= MAX_AUTOCOMPLETE_WARMUP_QUERIES) break;
  }
  return deduped;
}

function queueAutocompleteWarmup(isStillGuest) {
  if (_autocompleteWarmupQueued) return;
  var warmupQueries = collectWarmupQueries();
  _autocompleteWarmupQueued = true;

  var requestBody = {
    queries: warmupQueries,
  };
  if (!_mapCenteredOnPrivateRoute && _map && typeof _map.getCenter === "function") {
    try {
      var center = _map.getCenter();
      if (center) {
        requestBody.center_lat = Number(center.lat);
        requestBody.center_lon = Number(center.lng);
      }
    } catch (_) {
      // center unavailable; warmup without center bias
    }
  }

  var runWarmup = function () {
    if (!isStillGuest()) {
      _autocompleteWarmupQueued = false;
      return;
    }
    fetch("/api/autocomplete/warmup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestBody),
    }).catch(function () {
      // warmup 실패는 사용자 흐름에 영향 주지 않음
    });
  };

  if (typeof window.requestIdleCallback === "function") {
    window.requestIdleCallback(runWarmup, { timeout: 2500 });
  } else {
    setTimeout(runWarmup, 1200);
  }
}

function fetchAutocomplete(query, abortState) {
  var cacheKey = buildAutocompleteCacheKey(query);
  var cachedItems = getCachedAutocompleteItems(cacheKey);
  if (Array.isArray(cachedItems) && cachedItems.length > 0) {
    return Promise.resolve(cachedItems);
  }

  if (abortState.ctrl) abortState.ctrl.abort();
  abortState.ctrl = new AbortController();
  var timeoutId = setTimeout(function () {
    if (abortState.ctrl) abortState.ctrl.abort();
  }, AUTOCOMPLETE_FETCH_TIMEOUT_MS);
  return fetch(
    "/api/autocomplete?q=" +
      encodeURIComponent(query) +
      buildMapCenterQueryString(),
    {
      signal: abortState.ctrl.signal,
    }
  )
    .then(function (res) {
      if (!res.ok) {
        throw new Error("autocomplete request failed");
      }
      return res.json();
    })
    .then(function (items) {
      if (!Array.isArray(items)) {
        return [];
      }
      setCachedAutocompleteItems(cacheKey, items);
      return items;
    })
    .catch(function () { return []; })
    .finally(function () { clearTimeout(timeoutId); });
}

function renderACDropdown($dropdown, items, onSelect) {
  if (!items || items.length === 0) {
    closeACDropdown($dropdown);
    return;
  }
  $dropdown.setAttribute("role", "listbox");
  var html = "";
  for (var i = 0; i < items.length; i++) {
    var it = items[i];
    var safeType = escapeHtml(it.type || "");
    var safeDisplayName = escapeHtml(it.display_name || it.address || "");
    var safeAddress = escapeHtml(it.address || "");
    var typeBadge = safeType ? '<span class="ac-item-type">' + safeType + "</span>" : "";
    html +=
      '<div class="ac-item" role="option" id="' + $dropdown.id + '-option-' + i +
      '" aria-selected="false" tabindex="-1" data-idx="' + i + '">' +
      '  <div class="ac-item-name">' + safeDisplayName + typeBadge + "</div>" +
      '  <div class="ac-item-addr">' + safeAddress + "</div>" +
      "</div>";
  }
  $dropdown.innerHTML = html;
  $dropdown.classList.remove("hidden");
  var ownerId = $dropdown.getAttribute("id");
  var $owner = ownerId
    ? document.querySelector('[aria-controls="' + ownerId + '"]')
    : null;
  if ($owner) $owner.setAttribute("aria-expanded", "true");

  var acItems = $dropdown.querySelectorAll(".ac-item");
  acItems.forEach(function (el) {
    el.addEventListener("mousedown", function (e) {
      e.preventDefault();
      var idx = parseInt(el.getAttribute("data-idx"), 10);
      onSelect(items[idx]);
    });
  });
}

function closeACDropdown($dropdown) {
  $dropdown.classList.add("hidden");
  var ownerId = $dropdown.getAttribute("id");
  var $owner = ownerId
    ? document.querySelector('[aria-controls="' + ownerId + '"]')
    : null;
  if ($owner) {
    $owner.setAttribute("aria-expanded", "false");
    $owner.removeAttribute("aria-activedescendant");
  }
}

function getEndpointForInput($input) {
  if ($input === $origin) return "origin";
  if ($input === $destination) return "dest";
  return null;
}

function applyAutocompleteSelection($input, $dropdown, setSelected, item, endpoint) {
  var selection = buildStableSelection(item, {
    display_name: item.display_name || item.address || "",
    address: item.address || item.display_name || "",
    selection_kind: item.selection_kind || "",
    canonical_query: item.canonical_query || "",
    source: item.source || null,
    confidence: item.confidence,
  });
  var namedPoiLabel = buildNamedPoiDisplayLabel(selection);
  $input.value =
    selection && selection.coords_ready === false
      ? namedPoiLabel || selection.canonical_query || item.canonical_query || item.address || item.display_name || ""
      : item.display_name || item.address || "";
  setSelected(selection);
  closeACDropdown($dropdown);
  var routeEndpoint = endpoint || getEndpointForInput($input);
  invalidateRouteInputState(routeEndpoint);
  refreshMapMarkersLive(routeEndpoint);
}

function withDisplayRoute(payload, origin, destination) {
  if (!payload || !payload.route) return payload;
  var nextOrigin = String(origin || "").trim();
  var nextDestination = String(destination || "").trim();
  return Object.assign({}, payload, {
    route: Object.assign({}, payload.route, {
      origin: nextOrigin || payload.route.origin,
      destination: nextDestination || payload.route.destination,
    }),
  });
}

function setupAutocomplete($input, $dropdown, setSelected, timerKey) {
  var currentItems = [];
  var activeIdx = -1;
  var abortState = { ctrl: null };
  var requestSeq = 0;
  var isComposing = false;

  $input.addEventListener("input", function () {
    clearAutocompleteTimer(timerKey);
    var q = $input.value.trim();
    if (shouldRetainSelectionOnInput(timerKey, q)) {
      closeACDropdown($dropdown);
      return;
    }
    setSelected(null);
    invalidateRouteInputState(timerKey);
    // Keep suppressing noisy trailing-jamo states, but do not block
    // already-stable syllables while a Korean IME composition is active.
    if (hasTrailingHangulJamo(q)) return;
    if (abortState.ctrl) {
      abortState.ctrl.abort();
      abortState.ctrl = null;
    }
    if (q.length < AUTOCOMPLETE_MIN_QUERY_LENGTH) {
      closeACDropdown($dropdown);
      currentItems = [];
      activeIdx = -1;
      _acActiveIdx[timerKey] = -1;
      return;
    }

    var timerId = setTimeout(function () {
      requestSeq += 1;
      var seq = requestSeq;
      var queryAtRequest = q;
      fetchAutocomplete(queryAtRequest, abortState).then(function (items) {
        if (seq !== requestSeq) return;
        if ($input.value.trim() !== queryAtRequest) return;
        if (!items || !items.length) {
          currentItems = [];
          activeIdx = -1;
          _acActiveIdx[timerKey] = -1;
          closeACDropdown($dropdown);
          return;
        }
        currentItems = items;
        activeIdx = -1;
        _acActiveIdx[timerKey] = -1;
        renderACDropdown($dropdown, items, function (item) {
          applyAutocompleteSelection($input, $dropdown, setSelected, item, timerKey);
        });
      });
    }, AUTOCOMPLETE_DEBOUNCE_MS);
    setAutocompleteTimer(timerKey, timerId);
  });

  $input.addEventListener("compositionstart", function () {
    isComposing = true;
  });

  $input.addEventListener("compositionend", function () {
    isComposing = false;
    // Some IMEs commit the finalized value after compositionend returns.
    setTimeout(function () {
      $input.dispatchEvent(new Event("input", { bubbles: true }));
    }, 0);
  });

  $input.addEventListener("keydown", function (e) {
    var items = $dropdown.querySelectorAll(".ac-item");
    if (!items.length) return;

    if (e.key === "ArrowDown") {
      e.preventDefault();
      activeIdx = Math.min(activeIdx + 1, items.length - 1);
      _acActiveIdx[timerKey] = activeIdx;
      items.forEach(function (el, i) {
        el.classList.toggle("ac-active", i === activeIdx);
      });
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      activeIdx = Math.max(activeIdx - 1, 0);
      _acActiveIdx[timerKey] = activeIdx;
      items.forEach(function (el, i) {
        el.classList.toggle("ac-active", i === activeIdx);
      });
    } else if (e.key === "Enter" && activeIdx >= 0 && currentItems[activeIdx]) {
      e.preventDefault();
      e.stopImmediatePropagation(); // 일반 Enter 핸들러의 handleSearch() 중복 호출 방지
      applyAutocompleteSelection($input, $dropdown, setSelected, currentItems[activeIdx], timerKey);
    } else if (e.key === "Escape") {
      closeACDropdown($dropdown);
      activeIdx = -1;
      _acActiveIdx[timerKey] = -1;
    }
  });

  $input.addEventListener("blur", function () {
    clearAutocompleteTimer(timerKey);
    setTimeout(function () { closeACDropdown($dropdown); }, 150);
  });

  document.addEventListener("pointerdown", function (event) {
    if ($dropdown.classList.contains("hidden")) return;
    if ($input.contains(event.target) || $dropdown.contains(event.target)) {
      return;
    }
    clearAutocompleteTimer(timerKey);
    currentItems = [];
    activeIdx = -1;
    _acActiveIdx[timerKey] = -1;
    closeACDropdown($dropdown);
  });
}

/* ── API ──────────────────────────────────────────── */

async function fetchConfig() {
  var res = await fetch("/api/config");
  if (!res.ok) throw new Error("Config fetch failed");
  return res.json();
}

function buildRoutePlaceMetadata(text, selection, coords) {
  var stable = hasStableSelection(selection) ? selection : null;
  var normalizedCoords = normalizeCoords(coords) || normalizeCoords(selection);
  var label = String(
    (stable && (stable.display_name || stable.address || stable.canonical_query)) ||
      text ||
      ""
  ).trim();
  var canonicalQuery = String(
    (stable && stable.canonical_query) ||
      (stable && stable.address) ||
      label
  ).trim();
  if (!stable && !normalizedCoords && !canonicalQuery) {
    return null;
  }
  var coordsReady = hasValidCoords(normalizedCoords);
  return {
    query: String(text || "").trim(),
    display_name: label || canonicalQuery,
    canonical_query: canonicalQuery || label,
    selection_kind: String(
      (stable && stable.selection_kind) || (stable ? "poi" : "address")
    ).trim(),
    coords_ready: coordsReady,
    lat: coordsReady ? Number(normalizedCoords.lat) : null,
    lon: coordsReady ? Number(normalizedCoords.lon) : null,
    degraded_reason: coordsReady
      ? null
      : String((stable && stable.degraded_reason) || "coords_unresolved"),
  };
}

function attachRouteContractFields(
  body,
  origin,
  destination,
  oCoords,
  dCoords,
  originSelection,
  destSelection
) {
  if (oCoords) body.origin_coords = { lat: oCoords.lat, lon: oCoords.lon };
  if (dCoords) body.dest_coords = { lat: dCoords.lat, lon: dCoords.lon };
  var originPlace = buildRoutePlaceMetadata(origin, originSelection, oCoords);
  var destPlace = buildRoutePlaceMetadata(destination, destSelection, dCoords);
  if (originPlace) body.origin_place = originPlace;
  if (destPlace) body.dest_place = destPlace;
}

function strictRouteContractHeaders() {
  return {
    "Content-Type": "application/json",
    "X-TTS-Route-Input-Contract": "strict",
  };
}

function tripRequestError(payload, status, fallback) {
  var data = payload || {};
  var reason = data.reason;
  var providerBucket = data.provider_bucket;
  var messages = {
    capacity_exhausted: "검색 요청이 많습니다. 잠시 후 다시 시도해 주세요.",
    stream_stall_timeout: "검색 응답이 지연되었습니다. 잠시 후 다시 시도해 주세요.",
    request_cancelled: "검색이 중단되었습니다.",
    panel_parse_timeout: "네이버 교통 정보를 제시간에 읽지 못했습니다. 잠시 후 다시 시도해 주세요.",
    arrival_deadline_in_past: "현재 시점 이전으로는 조회를 할 수 없습니다.",
    provider_retry_exhausted: "네이버 교통 정보 응답이 지연되었습니다. 잠시 후 다시 시도해 주세요.",
    ncaptcha_backoff: "네이버 교통 정보 조회가 잠시 제한되었습니다. 잠시 후 다시 시도해 주세요.",
    coords_unresolved: COORDS_UNRESOLVED_ROUTE_MESSAGE,
  };
  var detail = data.detail;
  if (Array.isArray(detail)) {
    detail = detail.map(function (item) { return item && item.msg; }).filter(Boolean).join(" / ");
  }
  var message = (Object.hasOwn(messages, providerBucket) && messages[providerBucket]) ||
    (Object.hasOwn(messages, reason) && messages[reason]) ||
    (typeof detail === "string" && detail) || fallback;
  var error = new Error(message);
  error.reason = reason;
  error.providerBucket = providerBucket;
  error.retryable = data.retryable !== false &&
    reason !== "coords_unresolved" && providerBucket !== "coords_unresolved" &&
    (status >= 500 || reason === "capacity_exhausted" ||
      reason === "stream_stall_timeout" || reason === "provider_degraded" ||
      reason === "worker_failed" || (status === 0 && !reason));
  return error;
}

function canSkipDepartureBaseline(error) {
  var sharedFailures = ["capacity_exhausted", "ncaptcha_backoff", "request_cancelled", "coords_unresolved"];
  return error.name !== "AbortError" &&
    !sharedFailures.includes(error.reason) && !sharedFailures.includes(error.providerBucket) &&
    (error.reason === "provider_degraded" ||
      ["panel_parse_timeout", "provider_retry_exhausted"].includes(error.providerBucket));
}

function hasUsableDepartureBaseline(data) {
  var offsetSuffix = /(?:Z|[+-]\d{2}:\d{2})$/;
  if (!data || typeof data.departure_time !== "string" || typeof data.arrival_time !== "string" ||
      !offsetSuffix.test(data.departure_time) || !offsetSuffix.test(data.arrival_time)) {
    return false;
  }
  var departure = parseIsoDatetime(data.departure_time);
  var arrival = parseIsoDatetime(data.arrival_time);
  return departure && arrival && departure <= arrival &&
    Number.isFinite(data.duration_seconds) && data.duration_seconds >= 0 &&
    typeof data.provider === "string";
}

async function fetchTrip(path, body, signal) {
  var response = await fetch("/v1/trip/" + path, {
    method: "POST",
    headers: strictRouteContractHeaders(),
    body: JSON.stringify(body),
    signal: signal,
  });
  if (!response.ok) {
    var payload = await response.json().catch(function () { return {}; });
    throw tripRequestError(payload, response.status,
      "요청 처리 중 오류가 발생했습니다 (" + response.status + ")");
  }
  return response;
}

async function readTripStream(path, body, signal, handlers) {
  var response = await fetchTrip(path, body, signal);
  if (!response.body) throw new Error("브라우저가 스트리밍 응답을 지원하지 않습니다.");

  signal.throwIfAborted();
  var reader = response.body.getReader();
  function cancelReader() { void reader.cancel().catch(function () {}); }
  signal.addEventListener("abort", cancelReader, { once: true });
  var decoder = new TextDecoder("utf-8");
  var buffer = "";
  var recommendation = null;
  var completed = false;
  var candidates = [];
  var progress = null;

  function dispatch(event, data) {
    if (event === "error" || event === "busy" || (event === "end" && data.ok !== true)) {
      if (event === "busy" && !data.reason) data.reason = "capacity_exhausted";
      throw tripRequestError(data, 0, "추천 계산 중 오류가 발생했습니다.");
    }
    if (event === "end") {
      completed = true;
      return;
    }
    if (event === "arrival" && handlers.onArrival) {
      progress = data.progress || progress;
      handlers.onArrival(data.arrival, data.immediate_safe_departure, progress);
    } else if (event === "plan" || event === "candidate") {
      if (event === "candidate") candidates.push(data.candidate || data);
      progress = event === "plan" ? data : data.progress || progress;
      handlers.onProgress(progress);
    } else if (event === "recommendation") {
      recommendation = data;
    }
  }

  function consumeBuffer() {
    var events = buffer.split(/\r?\n\r?\n/);
    buffer = events.pop();
    for (var block of events) {
      var event = "message";
      var data = [];
      block.split(/\r?\n/).forEach(function (line) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
      });
      if (data.length) dispatch(event, JSON.parse(data.join("\n")) || {});
      if (completed) break;
    }
  }

  try {
    while (!completed) {
      var chunk = await reader.read();
      signal.throwIfAborted();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      consumeBuffer();
    }
  } finally {
    // Stop the response when an SSE error arrives before its closing frame.
    signal.removeEventListener("abort", cancelReader);
    cancelReader();
    reader.releaseLock();
  }

  if (!completed) throw new Error("검색 응답이 끝나기 전에 연결이 끊겼습니다. 다시 시도해 주세요.");
  if (!recommendation) throw new Error("추천 결과를 수신하지 못했습니다.");
  if (candidates.length && !recommendation.candidate_evaluations?.length) {
    recommendation.candidate_evaluations = candidates;
  }
  return recommendation;
}

/* ── Rendering ────────────────────────────────────── */

var _RENDER_CONTEXT_BY_MODE = {
  arrival: {
    mode: "arrival",
    baselineTitle: "입력 출발 기준 단일 조회",
    baselineDurationLabel: "입력 출발 기준 소요시간",
    pendingTitle: "추천 출발 시각 계산 중",
    pendingCopy: "입력 출발 시각 기준 결과를 먼저 보여주고, 추천 결과가 준비되면 자동으로 갱신합니다.",
    baselineHint: "입력한 출발 시각 결과를 먼저 표시",
    desiredLabel: "지정 출발시 예상 도착 시간",
    analysisTitle: "출발 시각 분석",
    analysisDirectionLabel: "전방향 탐색",
    tightTag: "타이트",
    tightLabel: "기준 출발",
    safeLabel: "여유 도착 (출발 고정 &times;1.25)",
    calcBaseDurationLabel: "기준 소요시간",
    calcFormulaText: "기준출발 + 보정소요 = 여유 도착시각",
    baselineDepartureLabel: "지정 출발 시간",
    arrivalDeltaLabel: "지정 출발 대비 도착 시간 차이",
    candidatePassText: "지정 출발보다 빠름",
    candidateFailText: "지정 출발보다 느림",
    statusPass: '<span class="result-badge badge-success">&#10003; 지정 출발 소요 시간보다 빠름</span>',
    statusFail: '<span class="result-badge badge-danger">&#9888; 지정 출발 소요 시간보다 느림</span>',
  },
  departure: {
    mode: "departure",
    baselineTitle: "참고 단일 조회(현재 시각 출발 기준)",
    baselineDurationLabel: "현재 출발 기준 소요시간",
    pendingTitle: "추천 출발 시각 계산 중",
    pendingCopy: "참고용 단일 조회(현재 시각 출발 기준)를 먼저 보여주고, 추천 결과가 준비되면 자동으로 갱신합니다.",
    baselineHint: "참고용: 현재 시각 출발 기준 단일 조회",
    desiredLabel: "희망 도착",
    analysisTitle: "마지노선 분석",
    analysisDirectionLabel: "역방향 탐색",
    tightTag: "타이트",
    tightLabel: "마지노선 출발",
    safeLabel: "여유 출발 (&times;1.25)",
    calcBaseDurationLabel: "마지노선 기준 소요시간",
    calcFormulaText: "희망도착 - 보정소요 = 안정적 출발시각",
    baselineDepartureLabel: "타이트 출발 시간",
    arrivalDeltaLabel: "희망 도착 대비 도착 시간 차이",
    candidatePassText: "정시 도착 가능",
    candidateFailText: "정시 도착 불가",
    statusPass: '<span class="result-badge badge-success">&#10003; 정시 도착 가능</span>',
    statusFail: '<span class="result-badge badge-danger">&#9888; 정시 도착 불가</span>',
  },
};

function _renderContext(mode) {
  return _RENDER_CONTEXT_BY_MODE[mode] || _RENDER_CONTEXT_BY_MODE.departure;
}

function candidatePhaseLabel(phase) {
  if (phase === "coarse") return "거친 탐색";
  if (phase === "refine") return "정밀 탐색";
  if (phase === "full") return "전수";
  return "전수";
}

function candidatePhaseClass(phase) {
  if (phase === "coarse") return "phase-coarse";
  if (phase === "refine") return "phase-refine";
  return "phase-full";
}

function buildCandidateScoreRow(label, score) {
  return (
    '<div class="candidate-tooltip-score-row">' +
    '  <span class="candidate-tooltip-score-label">' + label + "</span>" +
    '  <span class="candidate-tooltip-score-value">' +
    formatScorePercent(score) +
    "</span>" +
    "</div>"
  );
}

function buildCandidateTooltip(candidates, context) {
  var ctx = context || _renderContext("departure");
  var items = candidates || [];
  var sorted = items.slice().sort(function (a, b) {
    return new Date(a.departure_time).getTime() - new Date(b.departure_time).getTime();
  });
  var html =
    '<div class="candidate-tooltip-header candidate-tooltip-row">' +
    "조회 후보 상세 (" + sorted.length + "건)" +
    "</div>";
  if (!sorted.length) {
    html += '<div class="candidate-tooltip-empty">후보 상세 데이터가 없습니다.</div>';
    return html;
  }
  for (var i = 0; i < sorted.length; i++) {
    var item = sorted[i];
    var resultText = item.meets_deadline
      ? ctx.candidatePassText
      : ctx.candidateFailText;
    var statusClass = item.meets_deadline
      ? "status-pass"
      : "status-fail";
    var scoreHtml = "";
    if (typeof item.score_total === "number") {
      scoreHtml =
        '<div class="candidate-tooltip-score">' +
        buildCandidateScoreRow("총점", item.score_total) +
        buildCandidateScoreRow("시간 효율", item.score_duration) +
        buildCandidateScoreRow("출발 기준 근접도", item.score_time_proximity) +
        buildCandidateScoreRow("야간 안전", item.score_night_drive) +
        buildCandidateScoreRow("구간 안정성", item.score_stability) +
        buildCandidateScoreRow("대기 대비 효율", item.score_improvement_efficiency) +
        "</div>";
    }
    html += (
      '<div class="candidate-tooltip-item">' +
      '  <div class="candidate-tooltip-item-top">' +
      '    <span class="candidate-phase-badge ' +
            candidatePhaseClass(item.phase) +
            '">' +
            candidatePhaseLabel(item.phase) +
      "</span>" +
      '    <span class="candidate-tooltip-item-range candidate-tooltip-row">' +
            formatDatetime(item.departure_time) +
            " -> " +
            formatDatetime(item.arrival_time) +
      "</span>" +
      "  </div>" +
      '  <div class="candidate-tooltip-item-meta">' +
      '    <span class="candidate-tooltip-duration">' +
            formatDuration(item.duration_seconds) +
      "</span>" +
      '    <span class="candidate-tooltip-status ' +
            statusClass +
            '">' +
            resultText +
      "</span>" +
      "  </div>" +
      scoreHtml +
      "</div>"
    );
  }
  return html;
}

function clearCandidateTooltipHideTimer() {
  if (_candidateTooltipHideTimer) {
    clearTimeout(_candidateTooltipHideTimer);
    _candidateTooltipHideTimer = null;
  }
}

function hideCandidateTooltip(immediate) {
  clearCandidateTooltipHideTimer();
  if (!immediate) {
    _candidateTooltipHideTimer = setTimeout(function () {
      hideCandidateTooltip(true);
    }, CANDIDATE_TOOLTIP_HIDE_DELAY_MS);
    return;
  }
  if (_candidateTooltipPortal) {
    _candidateTooltipPortal.classList.remove("is-visible");
    _candidateTooltipPortal.setAttribute("aria-hidden", "true");
    _candidateTooltipPortal.innerHTML = "";
  }
  _candidateTooltipActiveBadge = null;
}

function ensureCandidateTooltipPortal() {
  if (_candidateTooltipPortal) {
    return _candidateTooltipPortal;
  }
  var $portal = document.createElement("div");
  $portal.className = "candidate-tooltip-panel candidate-tooltip-portal";
  $portal.setAttribute("aria-hidden", "true");
  document.body.appendChild($portal);

  $portal.addEventListener("mouseenter", function () {
    clearCandidateTooltipHideTimer();
  });
  $portal.addEventListener("mouseleave", function () {
    hideCandidateTooltip(false);
  });

  if (!_candidateTooltipGlobalBound) {
    document.addEventListener("pointerdown", function (event) {
      if (!_candidateTooltipPortal || !_candidateTooltipPortal.classList.contains("is-visible")) {
        return;
      }
      var target = event.target;
      var withinPortal = _candidateTooltipPortal.contains(target);
      var withinBadge = target.closest && target.closest(".candidate-badge");
      if (!withinPortal && !withinBadge) {
        hideCandidateTooltip(true);
      }
    });
    $sidebar.addEventListener("scroll", function () {
      if (_candidateTooltipActiveBadge) {
        positionCandidateTooltipPortal(_candidateTooltipActiveBadge);
      }
    }, { passive: true });
    window.addEventListener("resize", function () {
      if (_candidateTooltipActiveBadge) {
        positionCandidateTooltipPortal(_candidateTooltipActiveBadge);
      }
    });
    _candidateTooltipGlobalBound = true;
  }

  _candidateTooltipPortal = $portal;
  return $portal;
}

function positionCandidateTooltipPortal($badge) {
  if (!_candidateTooltipPortal || !$badge || !$sidebar) {
    return;
  }
  var portalRect = _candidateTooltipPortal.getBoundingClientRect();
  var badgeRect = $badge.getBoundingClientRect();
  var sidebarRect = $sidebar.getBoundingClientRect();
  if (!portalRect.width || !portalRect.height || !sidebarRect.width) {
    return;
  }

  var margin = 8;
  var gap = 6;
  var left = badgeRect.left;
  var minLeft = sidebarRect.left + margin;
  var maxLeft = sidebarRect.right - margin - portalRect.width;
  if (left < minLeft) {
    left = minLeft;
  }
  if (left > maxLeft) {
    left = maxLeft;
  }

  var top = badgeRect.top - portalRect.height - gap;
  if (top < sidebarRect.top + margin) {
    top = badgeRect.bottom + gap;
  }
  var maxTop = sidebarRect.bottom - margin - portalRect.height;
  if (top > maxTop) {
    top = maxTop;
  }
  if (top < sidebarRect.top + margin) {
    top = sidebarRect.top + margin;
  }

  _candidateTooltipPortal.style.left = Math.round(left) + "px";
  _candidateTooltipPortal.style.top = Math.round(top) + "px";
}

function showCandidateTooltip($badge) {
  if (!$badge) {
    return;
  }
  var $template = $badge.querySelector(".candidate-tooltip-panel");
  if (!$template) {
    return;
  }
  var $portal = ensureCandidateTooltipPortal();
  clearCandidateTooltipHideTimer();
  _candidateTooltipActiveBadge = $badge;

  $portal.innerHTML = $template.innerHTML;
  $portal.classList.add("is-visible");
  $portal.setAttribute("aria-hidden", "false");
  positionCandidateTooltipPortal($badge);
  window.requestAnimationFrame(function () {
    if (_candidateTooltipActiveBadge === $badge) {
      positionCandidateTooltipPortal($badge);
    }
  });
}

function setupCandidateTooltipPanels() {
  var badges = $results.querySelectorAll(".candidate-badge");
  for (var i = 0; i < badges.length; i++) {
    (function ($badge) {
      var $template = $badge.querySelector(".candidate-tooltip-panel");
      if ($template) {
        $template.classList.add("candidate-tooltip-template");
        $template.setAttribute("aria-hidden", "true");
      }
      $badge.addEventListener("mouseenter", function () {
        showCandidateTooltip($badge);
      });
      $badge.addEventListener("mouseleave", function () {
        hideCandidateTooltip(false);
      });
      $badge.addEventListener("focusin", function () {
        showCandidateTooltip($badge);
      });
      $badge.addEventListener("focusout", function () {
        hideCandidateTooltip(false);
      });
      $badge.addEventListener("touchstart", function (event) {
        event.stopPropagation();
        if (_candidateTooltipActiveBadge === $badge && _candidateTooltipPortal) {
          hideCandidateTooltip(true);
          return;
        }
        showCandidateTooltip($badge);
      });
    })(badges[i]);
  }
}

function hideAnalysisTooltips(excludeCard) {
  var cards = $results.querySelectorAll(".deadline-card.analysis-tooltip-open");
  for (var i = 0; i < cards.length; i++) {
    var $card = cards[i];
    if (excludeCard && $card === excludeCard) {
      continue;
    }
    $card.classList.remove("analysis-tooltip-open");
    var $trigger = $card.querySelector(".analysis-tooltip-trigger");
    if ($trigger) {
      $trigger.setAttribute("aria-expanded", "false");
    }
  }
}

function setupAnalysisTooltipPanels() {
  var cards = $results.querySelectorAll(".deadline-card");
  for (var i = 0; i < cards.length; i++) {
    (function ($card) {
      var $trigger = $card.querySelector(".analysis-tooltip-trigger");
      var $panel = $card.querySelector(".analysis-tooltip-panel");
      if (!$trigger || !$panel) {
        return;
      }
      $trigger.addEventListener("click", function (event) {
        event.stopPropagation();
        var isOpen = $card.classList.contains("analysis-tooltip-open");
        if (isOpen) {
          hideAnalysisTooltips(null);
          return;
        }
        hideAnalysisTooltips($card);
        $card.classList.add("analysis-tooltip-open");
        $trigger.setAttribute("aria-expanded", "true");
      });
    })(cards[i]);
  }

  if (_analysisTooltipGlobalBound) {
    return;
  }
  document.addEventListener("pointerdown", function (event) {
    var target = event.target;
    if (!target || !target.closest) {
      hideAnalysisTooltips(null);
      return;
    }
    var withinTrigger = target.closest(".analysis-tooltip-trigger");
    var withinPanel = target.closest(".analysis-tooltip-panel");
    if (!withinTrigger && !withinPanel) {
      hideAnalysisTooltips(null);
    }
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      hideAnalysisTooltips(null);
    }
  });
  _analysisTooltipGlobalBound = true;
}

function buildImmediateSafeCard(immediateSafe) {
  if (!immediateSafe || !immediateSafe.safe_departure_time) {
    return "";
  }
  var safeDur = formatDuration(immediateSafe.safe_duration_seconds || 0);
  var clampMsg = immediateSafe.clamped_to_now
    ? '<span class="result-badge badge-warn">&#9888; 현재 시각 하한 적용</span>'
    : '<span class="result-badge badge-success">&#10003; 즉시 산출</span>';
  return (
    '<div class="result-card quick-safe-card">' +
    '  <div class="result-title">&#127793; 즉시 여유 출발 제안</div>' +
    '  <div class="result-rows">' +
    '    <div class="result-row">' +
    '      <span class="label">여유 출발</span>' +
    '      <span class="value">' + formatDatetime(immediateSafe.safe_departure_time) + "</span>" +
    "    </div>" +
    '    <div class="result-row">' +
    '      <span class="label">적용 소요시간</span>' +
    '      <span class="value">' + safeDur + "</span>" +
    "    </div>" +
    "  </div>" +
    '  <div class="result-meta">' + clampMsg + "</div>" +
    "</div>"
  );
}

function computeArrivalFromDeparture(departureTime, durationSeconds) {
  if (!departureTime || durationSeconds == null) {
    return null;
  }
  var departure = new Date(departureTime);
  if (isNaN(departure.getTime())) {
    return null;
  }
  return new Date(departure.getTime() + durationSeconds * 1000).toISOString();
}

function buildSampledDepartureNote(data) {
  if (!data) return "";
  var requested = parseIsoDatetime(data.departure_time);
  var sampled = parseIsoDatetime(data.sampled_departure_time);
  if (!requested || !sampled || requested.getTime() === sampled.getTime()) return "";
  return '<p class="progress-copy sample-time-note">교통 정보 조회 기준 시각 ' +
    formatDatetime(data.sampled_departure_time) + "</p>";
}

function buildAnalysisCard(data, context, options) {
  var ctx = context || _renderContext("departure");
  var opts = options || {};
  var latestDepartureTime = data.latest_departure_time || null;
  var latestArrivalTime = data.latest_departure_arrival_time || null;
  var latestDurationSeconds = data.latest_departure_duration_seconds || null;
  var safeDepartureTime = data.safe_departure_time || null;
  var safeDurationSeconds = data.safe_departure_duration_seconds || null;
  var safeArrivalTime = computeArrivalFromDeparture(
    safeDepartureTime,
    safeDurationSeconds
  );
  var tightDur = formatDuration(latestDurationSeconds);
  var safeDur = formatDuration(safeDurationSeconds);
  var bufferText = latestDurationSeconds != null && safeDurationSeconds != null
    ? "+" + formatDuration(Math.max(0, safeDurationSeconds - latestDurationSeconds))
    : "-";
  var candidateCount = data.candidates_checked || 0;
  var plannedCount = data.planned_queries || 0;
  var totalCount = data.total_candidates || 0;
  var candidateSummary = candidateCount + "개";
  if (plannedCount > 0) {
    candidateSummary += " / 계획 " + plannedCount + "개";
  }
  if (totalCount > 0) {
    candidateSummary += " / 전체 " + totalCount + "개";
  }
  var candidateRow = opts.hideCandidateSummary
    ? ""
    : (
      '<div class="calc-explain-row"><span class="label">분석 후보 수</span><span class="val">' +
      candidateSummary +
      " (" +
      ctx.analysisDirectionLabel +
      ")</span></div>"
    );
  return (
    '<div class="result-card deadline-card">' +
    '  <div class="result-title">&#9200; ' + ctx.analysisTitle +
    '    <button type="button" class="analysis-tooltip-trigger" aria-expanded="false">&#9432; 보정 근거</button>' +
    "</div>" +
    '  <div class="deadline-grid">' +
    '    <div class="deadline-block tight-block">' +
    '      <div class="deadline-block-header">' +
    '        <span class="deadline-tag tag-tight">' + ctx.tightTag + "</span>" +
    '        <span class="deadline-block-label">' + ctx.tightLabel + "</span>" +
    "      </div>" +
    '      <div class="deadline-time">' +
            formatDatetime(latestDepartureTime) +
    "</div>" +
    '      <div class="deadline-sub">' +
    '        <span>도착 ' +
            formatDatetime(latestArrivalTime) +
    "</span>" +
    '        <span class="deadline-dur">' + tightDur + "</span>" +
    "      </div>" +
    "    </div>" +
    '    <div class="deadline-block safe-block">' +
    '      <div class="deadline-block-header">' +
    '        <span class="deadline-tag tag-safe">안정적</span>' +
    '        <span class="deadline-block-label">' + ctx.safeLabel + "</span>" +
    "      </div>" +
    '      <div class="deadline-time">' +
            formatDatetime(safeDepartureTime) +
    "</div>" +
    '      <div class="deadline-sub">' +
    '        <span>도착 ' +
            formatDatetime(safeArrivalTime) +
    "</span>" +
    '        <span class="deadline-dur">' + safeDur + "</span>" +
    "      </div>" +
    "    </div>" +
    "  </div>" +
    buildSampledDepartureNote(opts.baseline) +
    '  <div class="calc-explain analysis-tooltip-panel">' +
    '    <div class="calc-explain-title">&#128209; 보정 산출 근거</div>' +
         candidateRow +
    '    <div class="calc-explain-row"><span class="label">' +
          ctx.calcBaseDurationLabel +
          '</span><span class="val">' + tightDur + "</span></div>" +
    '    <div class="calc-explain-row"><span class="label">안정적 보정 계수</span><span class="val">&times;1.25 (25% 여유)</span></div>' +
    '    <div class="calc-explain-row"><span class="label">보정 적용 소요시간</span><span class="val">' + safeDur + "</span></div>" +
    '    <div class="calc-explain-row"><span class="label">추가된 여유 시간</span><span class="val">' +
          bufferText +
          '</span></div>' +
    '    <div class="calc-explain-row"><span class="label">산출 방식</span><span class="val">' +
          ctx.calcFormulaText +
          "</span></div>" +
    "  </div>" +
    "</div>"
  );
}

function buildLiveProgressCard(progress) {
  var checked = progress && progress.checked ? progress.checked : 0;
  var planned = progress && progress.planned ? progress.planned : 0;
  var remaining = progress && typeof progress.remaining === "number"
    ? progress.remaining
    : Math.max(0, planned - checked);
  var plannedText = planned > 0 ? String(planned) : "-";
  return (
    '<div id="live-progress-card" class="result-card progress-card">' +
    '  <div class="result-title">&#9201; 추천 출발 시각 계산 중</div>' +
    '  <p class="progress-copy">후보군 분석을 진행 중입니다.</p>' +
    '  <div class="result-meta progress-meta">' +
    '    <span class="result-badge badge-warn candidate-badge">' +
    "&#128202; 분석 <span id=\"live-candidate-count\">" + checked + "</span>개 / " +
    "<span id=\"live-candidate-total\">" + plannedText + "</span>개" +
    "</span>" +
    '    <span class="result-badge badge-success">' +
    "&#9203; 남은 후보 <span id=\"live-candidate-remaining\">" + remaining + "</span>개" +
    "</span>" +
    "  </div>" +
    "</div>"
  );
}

function updateLiveProgress(progress) {
  var checkedEl = document.getElementById("live-candidate-count");
  var plannedEl = document.getElementById("live-candidate-total");
  var remainingEl = document.getElementById("live-candidate-remaining");
  if (!checkedEl || !plannedEl || !remainingEl) {
    return false;
  }
  var checked = progress && progress.checked ? progress.checked : 0;
  var planned = progress && progress.planned ? progress.planned : 0;
  var remaining = progress && typeof progress.remaining === "number"
    ? progress.remaining
    : Math.max(0, planned - checked);
  checkedEl.textContent = String(checked);
  plannedEl.textContent = planned > 0 ? String(planned) : "-";
  remainingEl.textContent = String(remaining);
  return true;
}

function buildBaselineArrivalCard(data, context) {
  var ctx = context || _renderContext("departure");
  var durationText = formatDuration(data.duration_seconds);
  var cacheBadge = data.cache_hit
    ? "&#9889; 캐시 히트"
    : "&#128268; 신규 조회";

  return (
    '<div class="result-card baseline-card">' +
    '  <div class="result-title">&#128345; ' + ctx.baselineTitle + "</div>" +
    '  <div class="result-rows">' +
    '    <div class="result-row">' +
    '      <span class="label">출발</span>' +
    '      <span class="value">' + formatDatetime(data.departure_time) + "</span>" +
    "    </div>" +
    '    <div class="result-row">' +
    '      <span class="label">도착</span>' +
    '      <span class="value">' + formatDatetime(data.arrival_time) + "</span>" +
    "    </div>" +
    "  </div>" +
    buildSampledDepartureNote(data) +
    '  <div class="result-duration">' +
    '    <div class="duration-big">' + durationText + "</div>" +
    '    <div class="duration-label">' + ctx.baselineDurationLabel + "</div>" +
    "  </div>" +
    '  <div class="result-meta">' +
    '    <span class="result-badge badge-warn">&#9203; ' + ctx.baselineHint + "</span>" +
    '    <span class="result-badge badge-success">' + cacheBadge + "</span>" +
    "  </div>" +
    "</div>"
  );
}

function renderDeparturePending(arrivalData, context, immediateSafe, progress) {
  hideCandidateTooltip(true);
  var ctx = context || _renderContext("departure");
  var prog = progress || {
    checked: 0,
    planned: 0,
    remaining: 0,
    total_candidates: 0,
  };
  if (arrivalData && arrivalData.provider) {
    setProviderName(arrivalData.provider);
  }
  var providerNoticeHtml = buildProviderNoticeCard(
    arrivalData && arrivalData.provider ? arrivalData.provider : _config.provider
  );

  if (ctx.mode === "arrival" && arrivalData) {
    if (updateLiveProgress(prog)) {
      return;
    }
    var pendingAnalysis = {
      latest_departure_time: arrivalData.departure_time,
      latest_departure_arrival_time: arrivalData.arrival_time,
      latest_departure_duration_seconds: arrivalData.duration_seconds,
      safe_departure_time: immediateSafe ? immediateSafe.safe_departure_time : null,
      safe_departure_duration_seconds: immediateSafe ? immediateSafe.safe_duration_seconds : null,
      candidates_checked: prog.checked || 0,
    };
    var analysisHtml = (
      providerNoticeHtml +
      buildAnalysisCard(
        pendingAnalysis,
        ctx,
        { hideCandidateSummary: true, baseline: arrivalData }
      ) +
      buildLiveProgressCard(prog)
    );
    $results.innerHTML = analysisHtml;
    $results.classList.remove("hidden");
    setupAnalysisTooltipPanels();
    return;
  }

  var html = providerNoticeHtml;
  if (arrivalData) {
    html += buildBaselineArrivalCard(arrivalData, ctx);
  }
  if (immediateSafe) {
    html += buildImmediateSafeCard(immediateSafe);
  }
  var checked = prog && prog.checked ? prog.checked : 0;
  var planned = prog && prog.planned ? prog.planned : 0;
  var remaining = prog && typeof prog.remaining === "number"
    ? prog.remaining
    : Math.max(0, planned - checked);
  var plannedText = planned > 0 ? String(planned) : "-";
  var progressText = planned > 0
    ? "후보 " + checked + "개 분석 / 계획 " + planned + "개"
    : (
      checked > 0
        ? "후보 " + checked + "개 분석 중입니다."
        : "후보를 수집하는 중입니다."
    );
  html +=
    '<div class="result-card progress-card">' +
    '  <div class="result-title">&#9201; ' + ctx.pendingTitle + "</div>" +
    '  <p class="progress-copy">' + ctx.pendingCopy + "</p>" +
    '  <p class="progress-copy">' + progressText + "</p>" +
    '  <div class="result-meta progress-meta">' +
    '    <span class="result-badge badge-warn">' +
    "&#128202; 분석 " + checked + "개 / " + plannedText + "개" +
    "</span>" +
    '    <span class="result-badge badge-success">' +
    "&#9203; 남은 후보 " + remaining + "개" +
    "</span>" +
    "  </div>" +
    "</div>";

  $results.innerHTML = html;
  $results.classList.remove("hidden");
  setupAnalysisTooltipPanels();
}

const DEPARTURE_BASELINE_UNAVAILABLE = "지금 출발할 때의 예상 시간을 불러오지 못했습니다.";

function renderDepartureResult(data, baselineArrivalData, context, immediateSafe) {
  hideCandidateTooltip(true);
  var ctx = context || _renderContext("departure");
  setProviderName(data.provider);
  var detailsHtml = buildProviderNoticeCard(data.provider);
  if (ctx.mode === "departure") {
    detailsHtml += baselineArrivalData
      ? buildBaselineArrivalCard(baselineArrivalData, ctx)
      : '<p class="result-card progress-copy baseline-unavailable">' + DEPARTURE_BASELINE_UNAVAILABLE + "</p>";
  }
  if (immediateSafe && ctx.mode !== "arrival") {
    detailsHtml += buildImmediateSafeCard(immediateSafe);
  }

  // Forward analysis can use a sampled slot for its baseline candidate.
  // Compare the recommendation to the actual trip returned in the arrival frame.
  var requestedBaseline = ctx.mode === "arrival" ? baselineArrivalData : null;
  var latestDepartureTime = requestedBaseline?.departure_time || data.latest_departure_time || null;
  var latestArrivalTime = requestedBaseline?.arrival_time || data.latest_departure_arrival_time || null;
  var latestDurationSeconds = requestedBaseline?.duration_seconds ?? data.latest_departure_duration_seconds ?? null;
  var comparisonArrivalTime = requestedBaseline?.arrival_time || data.desired_arrival_time;
  var safeDepartureTime = data.safe_departure_time || immediateSafe?.safe_departure_time || null;
  var safeDurationSeconds = data.safe_departure_duration_seconds ?? immediateSafe?.safe_duration_seconds ?? null;
  if (requestedBaseline && immediateSafe?.safe_departure_time && immediateSafe.safe_duration_seconds != null) {
    safeDepartureTime = immediateSafe.safe_departure_time;
    safeDurationSeconds = immediateSafe.safe_duration_seconds;
  }

  var recommendedDepartureDisplayTime = data.recommended_departure_time || null;
  var recommendedDurationDisplaySeconds = data.duration_seconds != null
    ? data.duration_seconds
    : null;
  var expectedArrivalDisplayTime = data.expected_arrival_time || null;
  if (ctx.mode === "departure" && safeDepartureTime) {
    recommendedDepartureDisplayTime = safeDepartureTime;
    if (safeDurationSeconds != null) {
      recommendedDurationDisplaySeconds = safeDurationSeconds;
      var safeExpectedArrival = computeArrivalFromDeparture(
        safeDepartureTime,
        safeDurationSeconds
      );
      if (safeExpectedArrival) {
        expectedArrivalDisplayTime = safeExpectedArrival;
      }
    }
  }

  var durationText = formatDuration(recommendedDurationDisplaySeconds);
  var tightDurationText = formatDuration(latestDurationSeconds);
  var safeDurationText = formatDuration(safeDurationSeconds);
  var displayMeetsDeadline = !!data.meets_deadline;
  if (ctx.mode === "departure") {
    var displayArrival = parseIsoDatetime(expectedArrivalDisplayTime);
    var desiredArrival = parseIsoDatetime(comparisonArrivalTime);
    if (displayArrival && desiredArrival) {
      displayMeetsDeadline = displayArrival.getTime() <= desiredArrival.getTime();
    }
  } else if (requestedBaseline && Number.isFinite(recommendedDurationDisplaySeconds) &&
      Number.isFinite(latestDurationSeconds)) {
    displayMeetsDeadline = recommendedDurationDisplaySeconds <= latestDurationSeconds;
  }
  var deadlineBadge = displayMeetsDeadline
    ? ctx.statusPass
    : ctx.statusFail;
  var arrivalDeltaText = formatArrivalDelta(
    expectedArrivalDisplayTime,
    comparisonArrivalTime
  );

  var analysisData = {
    latest_departure_time: latestDepartureTime,
    latest_departure_arrival_time: latestArrivalTime,
    latest_departure_duration_seconds: latestDurationSeconds,
    safe_departure_time: safeDepartureTime,
    safe_departure_duration_seconds: safeDurationSeconds,
    candidates_checked: data.candidates_checked,
    planned_queries: data.planned_queries,
    total_candidates: data.total_candidates,
  };
  if (analysisData.latest_departure_time || analysisData.safe_departure_time) {
    detailsHtml += buildAnalysisCard(analysisData, ctx, {
      baseline: requestedBaseline,
    });
  }

  var candidateTooltipHtml = buildCandidateTooltip(data.candidate_evaluations || [], ctx);
  var checkedCount = data.candidates_checked || 0;
  var plannedCount = data.planned_queries || 0;
  var totalCount = data.total_candidates || 0;
  var candidateBadgeText = "&#128202; 분석 " + checkedCount + "개";
  if (plannedCount > 0) {
    candidateBadgeText += " / 계획 " + plannedCount + "개";
  }
  if (totalCount > 0) {
    candidateBadgeText += " / 전체 " + totalCount + "개";
  }
  var candidateBadge =
    '<span class="result-badge badge-warn candidate-badge">' +
    candidateBadgeText +
    '<span class="candidate-tooltip-panel candidate-tooltip-template" aria-hidden="true">' +
      candidateTooltipHtml +
    "</span>" +
    "</span>";
  var recommendedScoreText = formatScorePercent(data.recommended_score_total);
  var baselineScoreText = formatScorePercent(data.baseline_score_total);
  var baselineDepartureText = formatDatetime(latestDepartureTime);
  var baselineDepartureLabel = ctx.baselineDepartureLabel || "지정 출발 시간";
  var arrivalDeltaLabel = ctx.arrivalDeltaLabel || "지정 출발 대비 도착 시간 차이";
  var baselineScoreLabel = ctx.mode === "arrival"
    ? "지정 출발 점수 "
    : "타이트 출발 점수 ";
  var recommendedScoreBadge =
    '<span class="result-badge badge-success">' +
    "추천 출발 점수 " +
    recommendedScoreText +
    "</span>";
  var baselineScoreBadge = latestDepartureTime
    ? (
      '<span class="result-badge badge-warn">' +
      baselineScoreLabel +
      baselineScoreText +
      "</span>"
    )
    : "";
  var recommendedArrivalForCalendar = expectedArrivalDisplayTime;
  if (!recommendedArrivalForCalendar) {
    recommendedArrivalForCalendar = computeArrivalFromDeparture(
      recommendedDepartureDisplayTime,
      recommendedDurationDisplaySeconds
    );
  }
  var tightArrivalForCalendar = latestArrivalTime;
  if (!tightArrivalForCalendar) {
    tightArrivalForCalendar = computeArrivalFromDeparture(
      latestDepartureTime,
      latestDurationSeconds
    );
  }
  var recommendedCalendarEvent = buildCalendarEventPayload({
    label: "추천 출발",
    route: data.route,
    departureIso: recommendedDepartureDisplayTime,
    arrivalIso: recommendedArrivalForCalendar,
    desiredArrivalIso: comparisonArrivalTime,
    durationSeconds: recommendedDurationDisplaySeconds,
  });
  var tightCalendarEvent = buildCalendarEventPayload({
    label: "타이트 출발",
    route: data.route,
    departureIso: latestDepartureTime,
    arrivalIso: tightArrivalForCalendar,
    desiredArrivalIso: comparisonArrivalTime,
    durationSeconds: latestDurationSeconds,
  });
  var recommendedCalendarUrl = buildGoogleCalendarTemplateUrl(
    recommendedCalendarEvent
  );
  var tightCalendarUrl = buildGoogleCalendarTemplateUrl(tightCalendarEvent);
  var calendarActionsHtml =
    '<div class="calendar-actions">' +
    '  <div class="calendar-actions-title">&#128197; 구글 캘린더</div>' +
    '  <div class="calendar-actions-buttons">' +
         buildCalendarActionLink(
           "추천 일정 추가",
           recommendedCalendarUrl,
           "is-recommended"
         ) +
         buildCalendarActionLink(
           "타이트 일정 추가",
           tightCalendarUrl,
           "is-tight"
         ) +
    "  </div>" +
    "</div>";

  var html =
    '<div class="result-card recommendation-card">' +
    '  <div class="result-title">&#128663; 추천 출발 시각</div>' +
    '  <div class="result-rows">' +
    '    <div class="result-row row-recommended-departure">' +
    '      <span class="label">추천 출발 시간</span>' +
    '      <span class="value">' + formatDatetime(recommendedDepartureDisplayTime) + "</span>" +
    "    </div>" +
    '    <div class="result-row row-baseline-departure">' +
    '      <span class="label">' + baselineDepartureLabel + "</span>" +
    '      <span class="value">' + baselineDepartureText + "</span>" +
    "    </div>" +
    '    <div class="result-row">' +
    '      <span class="label">추천 출발시 예상 도착 시간</span>' +
    '      <span class="value">' + formatDatetime(expectedArrivalDisplayTime) + "</span>" +
    "    </div>" +
    '    <div class="result-row">' +
    '      <span class="label">' + ctx.desiredLabel + "</span>" +
    '      <span class="value">' + formatDatetime(comparisonArrivalTime) + "</span>" +
    "    </div>" +
    '    <div class="result-row">' +
    '      <span class="label">' + arrivalDeltaLabel + "</span>" +
    '      <span class="value">' + arrivalDeltaText + "</span>" +
    "    </div>" +
    '    <div class="result-row">' +
    '      <span class="label">타이트 소요시간</span>' +
    '      <span class="value">' + tightDurationText + "</span>" +
    "    </div>" +
    '    <div class="result-row">' +
    '      <span class="label">안정적 소요시간 (&times;1.25)</span>' +
    '      <span class="value">' + safeDurationText + "</span>" +
    "    </div>" +
    "  </div>" +
    '  <div class="result-duration">' +
    '    <div class="duration-big">' + durationText + "</div>" +
    '    <div class="duration-label">예상 소요시간</div>' +
    "  </div>" +
    '  <div class="result-meta">' +
    "    " + deadlineBadge +
    "    " + recommendedScoreBadge +
    "    " + baselineScoreBadge +
    "    " + candidateBadge +
    "  </div>" +
         calendarActionsHtml +
    "</div>";

  $results.innerHTML = html + detailsHtml;
  $results.classList.remove("hidden");
  setupAnalysisTooltipPanels();
  setupCandidateTooltipPanels();
}

/* ── Recent Searches UI ──────────────────────────── */

function renderRecentSearches() {
  var $section = document.getElementById("recent-section");
  var $list = document.getElementById("recent-list");
  var items = getRecentSearches();

  if (items.length === 0) {
    $section.classList.add("hidden");
    return;
  }
  $section.classList.remove("hidden");

  var html = "";
  for (var i = 0; i < items.length; i++) {
    var r = items[i];
    var safeOrigin = escapeHtml(r.origin || "");
    var safeDestination = escapeHtml(r.destination || "");
    html +=
      '<div class="recent-item" data-idx="' + i + '">' +
      '  <span class="recent-item-route">' +
           safeOrigin +
           ' <span class="recent-item-arrow">&rarr;</span> ' +
           safeDestination +
      '  </span>' +
      '  <span class="recent-item-time">' + timeAgo(r.ts) + '</span>' +
      '</div>';
  }
  $list.innerHTML = html;

  $list.querySelectorAll(".recent-item").forEach(function (el) {
    el.addEventListener("click", function () {
      var idx = parseInt(el.getAttribute("data-idx"), 10);
      var r = items[idx];
      if (hasPrivateRouteInput()) {
        setPrivateRouteEndpoint("origin", false);
        setPrivateRouteEndpoint("dest", false);
        resetRouteAutocomplete($origin);
        resetRouteAutocomplete($destination);
      }
      $origin.value = r.origin;
      $destination.value = r.destination;
      _selectedOrigin = buildStableSelection(r.origin_coords, {
        display_name: r.origin,
        address: r.origin,
        canonical_query: r.origin,
      });
      _selectedDest = buildStableSelection(r.destination_coords, {
        display_name: r.destination,
        address: r.destination,
        canonical_query: r.destination,
      });
      invalidateRouteInputState();
      refreshMapMarkersLive();
    });
  });
}

/* ── Favorites route selection ───────────────────── */

function hasPrivateRouteInput() {
  return _privateRouteEndpoints.origin || _privateRouteEndpoints.dest;
}

function setPrivateRouteEndpoint(endpoint, isPrivate) {
  _privateRouteEndpoints[endpoint] = isPrivate;
}

function resetRouteAutocomplete(input) {
  input.dispatchEvent(new Event("tts:reset-autocomplete"));
}

function clearPrivateRouteState() {
  if (!hasPrivateRouteInput() && !_mapCenteredOnPrivateRoute) return;

  if (_privateRouteEndpoints.origin) {
    $origin.value = "";
    _selectedOrigin = null;
    resetRouteAutocomplete($origin);
  }
  if (_privateRouteEndpoints.dest) {
    $destination.value = "";
    _selectedDest = null;
    resetRouteAutocomplete($destination);
  }
  _privateRouteEndpoints.origin = false;
  _privateRouteEndpoints.dest = false;
  invalidateRouteInputState();
  if (_mapCenteredOnPrivateRoute && _map) {
    _map.stop();
    _map.setView(DEFAULT_MAP_CENTER, DEFAULT_MAP_ZOOM, { animate: false });
  }
  _mapCenteredOnPrivateRoute = false;
}

function useFavoriteForRoute(favorite, endpoint, isPrivate) {
  var selection = buildStableSelection({
    lat: favorite.lat,
    lon: favorite.lon,
    display_name: favorite.name,
    address: favorite.address,
    selection_kind: "poi",
    canonical_query: favorite.address,
    source: "favorite",
  });
  var input = endpoint === "origin" ? $origin : $destination;
  resetRouteAutocomplete(input);
  setPrivateRouteEndpoint(endpoint, isPrivate);
  input.value = favorite.address;
  if (endpoint === "origin") _selectedOrigin = selection;
  else _selectedDest = selection;
  invalidateRouteInputState(endpoint);
  refreshMapMarkersLive(endpoint);
}

/* ── Events ───────────────────────────────────────── */

function switchMode(mode) {
  if (_currentMode !== mode) {
    invalidateSearchOnlyState();
  }
  _currentMode = mode;
  if (mode === "arrival") {
    $tabArrival.classList.add("active");
    $tabArrival.setAttribute("aria-selected", "true");
    $tabDeparture.classList.remove("active");
    $tabDeparture.setAttribute("aria-selected", "false");
    $datetimeLabel.textContent = "출발 시각";
  } else {
    $tabDeparture.classList.add("active");
    $tabDeparture.setAttribute("aria-selected", "true");
    $tabArrival.classList.remove("active");
    $tabArrival.setAttribute("aria-selected", "false");
    $datetimeLabel.textContent = "도착 희망 시각";
  }
  hideCandidateTooltip(true);
  $results.classList.add("hidden");
  hideError();
}

$tabArrival.addEventListener("click", function () { switchMode("arrival"); });
$tabDeparture.addEventListener("click", function () { switchMode("departure"); });

$swapBtn.addEventListener("click", function () {
  var tmp = $origin.value;
  $origin.value = $destination.value;
  $destination.value = tmp;
  var tmpCoords = _selectedOrigin;
  _selectedOrigin = _selectedDest;
  _selectedDest = tmpCoords;
  if (hasPrivateRouteInput()) {
    var privateOrigin = _privateRouteEndpoints.origin;
    setPrivateRouteEndpoint("origin", _privateRouteEndpoints.dest);
    setPrivateRouteEndpoint("dest", privateOrigin);
    resetRouteAutocomplete($origin);
    resetRouteAutocomplete($destination);
  }
  invalidateRouteInputState();
  refreshMapMarkersLive();
});

/* Live marker update on input change */
function refreshMapMarkersLive(endpoint) {
  if (!_map) return;
  var orig = $origin.value.trim();
  var dest = $destination.value.trim();
  var originSelection = _selectedOrigin;
  var destSelection = _selectedDest;
  var refreshOrigin = endpoint !== "dest";
  var refreshDest = endpoint !== "origin";
  var isLiveMarkerRefreshCurrent = function () {
    return (
      !_searchInProgress &&
      $origin.value.trim() === orig &&
      $destination.value.trim() === dest &&
      _selectedOrigin === originSelection &&
      _selectedDest === destSelection
    );
  };

  var pO = orig && refreshOrigin
    ? resolveRouteCriticalCoords(orig, originSelection)
    : Promise.resolve(orig ? normalizeCoords(originSelection) : null);
  var pD = dest && refreshDest
    ? resolveRouteCriticalCoords(dest, destSelection)
    : Promise.resolve(dest ? normalizeCoords(destSelection) : null);

  Promise.all([pO, pD]).then(function (results) {
    if (!isLiveMarkerRefreshCurrent()) return;
    var originCoords = results[0];
    var destCoords = results[1];
    if (originCoords && hasStableSelection(originSelection) && !hasValidCoords(originSelection)) {
      originSelection = markSelectionCoordsResolved(originSelection, originCoords);
      _selectedOrigin = originSelection;
    }
    if (destCoords && hasStableSelection(destSelection) && !hasValidCoords(destSelection)) {
      destSelection = markSelectionCoordsResolved(destSelection, destCoords);
      _selectedDest = destSelection;
    }
    if (refreshOrigin) {
      if (originCoords) {
        setEndpointMarker("origin", [originCoords.lat, originCoords.lon], { color: "#03C75A", label: "출발" });
      } else {
        clearEndpointMarker("origin");
      }
    }
    if (refreshDest) {
      if (destCoords) {
        setEndpointMarker("dest", [destCoords.lat, destCoords.lon], { color: "#E53935", label: "도착" });
      } else {
        clearEndpointMarker("dest");
      }
    }
    fitBounds();
    if (originCoords && destCoords) {
      clearRouteGroup();
      fetchAndDrawRoute(
        originCoords.lat, originCoords.lon,
        destCoords.lat, destCoords.lon,
        isLiveMarkerRefreshCurrent
      );
    }
  });
}

$origin.addEventListener("change", function () {
  var orig = $origin.value.trim();
  var dest = $destination.value.trim();
  if (!hasValidCoords(_selectedOrigin) && orig.length >= 2 && dest.length >= 2) {
    refreshMapMarkersLive();
  }
});
$destination.addEventListener("change", function () {
  var orig = $origin.value.trim();
  var dest = $destination.value.trim();
  if (!hasValidCoords(_selectedDest) && orig.length >= 2 && dest.length >= 2) {
    refreshMapMarkersLive();
  }
});

function setSidebarOpen(open) {
  $sidebar.classList.toggle("open", open);
  if (open) {
    $sidebar.classList.remove("sheet-hidden");
  }
  var hidden = $sidebar.classList.contains("sheet-hidden");
  $sheetRestore.hidden = !hidden;
  $sidebar.inert = hidden && isMobileViewport();
  if ($mobileToggle) {
    $mobileToggle.setAttribute("aria-expanded", open ? "true" : "false");
  }
  if ($sidebarBackdrop) {
    $sidebarBackdrop.classList.toggle(
      "is-visible",
      open && !$sidebar.classList.contains("sheet-hidden")
    );
  }
  if (!open && _datetimePickerState.isOpen) {
    closeDatetimePanel();
  }
}

function setSheetHidden(hidden) {
  $sidebar.classList.toggle("sheet-hidden", hidden);
  if (hidden && $sidebar.contains(document.activeElement)) document.activeElement.blur();
  setSidebarOpen(false);
}

function isMobileViewport() {
  return window.matchMedia && window.matchMedia("(max-width: 768px)").matches;
}

(function () {
  var $sheetHeader = document.querySelector(".sidebar-header");
  if (!$sheetHeader) return;

  var drag = null;

  function pointerY(e) {
    return e.touches && e.touches.length ? e.touches[0].clientY : e.clientY;
  }

  $sheetHeader.addEventListener("pointerdown", function (e) {
    if (!isMobileViewport() || $sidebar.classList.contains("sheet-hidden")) {
      return;
    }
    drag = {
      id: e.pointerId,
      startY: pointerY(e),
      dy: 0,
      moved: false,
      wasOpen: $sidebar.classList.contains("open"),
    };
    try {
      $sheetHeader.setPointerCapture(e.pointerId);
    } catch (err) {}
  });

  window.addEventListener(
    "pointermove",
    function (e) {
      if (!drag || e.pointerId !== drag.id) return;
      var dy = Math.max(0, pointerY(e) - drag.startY);
      drag.dy = dy;
      if (!drag.moved && dy > 4) {
        drag.moved = true;
        $sidebar.classList.add("dragging");
      }
      if (drag.moved) {
        $sidebar.style.transform = "translateY(" + Math.round(dy) + "px)";
        if (e.cancelable) {
          e.preventDefault();
        }
      }
    },
    { passive: false }
  );

  function endDrag(e) {
    if (!drag || e.pointerId !== drag.id) return;
    var state = drag;
    drag = null;
    try {
      $sheetHeader.releasePointerCapture(e.pointerId);
    } catch (err) {}
    $sidebar.classList.remove("dragging");
    $sidebar.style.transform = "";
    if (!state.moved) {
      setSidebarOpen(!state.wasOpen);
      return;
    }
    var threshold = Math.max(80, window.innerHeight * 0.12);
    if (state.dy > threshold || state.dy > window.innerHeight * 0.25) {
      if (state.wasOpen) {
        if (state.dy > window.innerHeight * 0.35) {
          setSheetHidden(true);
        } else {
          setSidebarOpen(false);
        }
      } else {
        setSheetHidden(true);
      }
    }
  }

  $sheetHeader.addEventListener("pointerup", endDrag);
  $sheetHeader.addEventListener("pointercancel", endDrag);

  $mobileToggle.addEventListener("click", function () {
    if ($sidebar.classList.contains("sheet-hidden")) {
      setSheetHidden(false);
      return;
    }
    setSidebarOpen(!$sidebar.classList.contains("open"));
  });
})();

window.addEventListener("resize", function () {
  $sidebar.inert = isMobileViewport() && $sidebar.classList.contains("sheet-hidden");
});
function restoreSheet() {
  setSheetHidden(false);
  $mobileToggle.focus({ preventScroll: true });
}
$sheetRestore.addEventListener("click", restoreSheet);
var restorePointer = null;
$sheetRestore.addEventListener("pointerdown", function (event) {
  restorePointer = { id: event.pointerId, startY: event.clientY };
  $sheetRestore.setPointerCapture(event.pointerId);
});
$sheetRestore.addEventListener("pointerup", function (event) {
  if (restorePointer && restorePointer.id === event.pointerId && restorePointer.startY - event.clientY > 36) {
    restoreSheet();
  }
  restorePointer = null;
});
$sheetRestore.addEventListener("pointercancel", function () { restorePointer = null; });

(function () {
  var viewport = window.visualViewport;
  if (!viewport) return;

  function updateKeyboardOffset() {
    var field = document.activeElement;
    var container = $sidebar.contains(field) ? $sidebar :
      _datetimePickerState.isOpen && $datetimePanel.contains(field) ? $datetimePanel : null;
    var textInput = field && container && !field.readOnly &&
      field.matches('input:not([type]), input[type="text"], input[type="search"], input[type="tel"], input[type="email"], input[type="url"], input[type="number"], textarea');
    var overlap = Math.round(window.innerHeight - viewport.height - viewport.offsetTop);
    var showOffset = isMobileViewport() && textInput && document.hasFocus() && !document.hidden &&
      Math.abs(viewport.scale - 1) < 0.02 && overlap >= 100;

    $sidebar.classList.toggle("kb-tracking", showOffset);
    $sidebar.style.bottom = showOffset ? overlap + "px" : "";
    $sidebar.style.maxHeight = showOffset
      ? Math.min($sidebar.scrollHeight, Math.max(0, Math.round(viewport.height) - 24)) + "px"
      : "";

    if (_datetimePickerState.isOpen) positionDatetimePanel();
    if (showOffset) {
      var bounds = container.getBoundingClientRect();
      var fieldRect = field.getBoundingClientRect();
      if (fieldRect.bottom + 12 > bounds.bottom) {
        container.scrollTop += fieldRect.bottom + 12 - bounds.bottom;
      } else if (fieldRect.top - 12 < bounds.top) {
        container.scrollTop -= bounds.top - fieldRect.top + 12;
      }
    }
  }
  viewport.addEventListener("resize", updateKeyboardOffset);
  viewport.addEventListener("scroll", updateKeyboardOffset);
  window.addEventListener("resize", updateKeyboardOffset);
  window.addEventListener("blur", updateKeyboardOffset);
  window.addEventListener("focus", updateKeyboardOffset);
  document.addEventListener("visibilitychange", updateKeyboardOffset);
  document.addEventListener("focusin", updateKeyboardOffset);
  document.addEventListener("focusout", function () {
    window.requestAnimationFrame(updateKeyboardOffset);
  });
})();

if ($sidebarBackdrop) {
  $sidebarBackdrop.addEventListener("click", function () {
    setSidebarOpen(false);
  });
}

function isCurrentSearchSnapshot(
  origin,
  destination,
  datetimeVal,
  originSelection,
  destSelection,
  mode
) {
  return (
    $origin.value.trim() === origin &&
    $destination.value.trim() === destination &&
    $datetimeInput.value === datetimeVal &&
    _selectedOrigin === originSelection &&
    _selectedDest === destSelection &&
    _currentMode === mode
  );
}

/**
 * 검색 실행 (최적화 흐름):
 *   1) 출발/도착 좌표를 병렬 resolve (autocomplete → 캐시 → API geocode)
 *   2) 단일 조회 결과를 먼저 렌더링
 *   3) 추천 출발 계산 완료 시 추천/여유 결과까지 갱신
 */
async function handleSearch() {
  if (_searchInProgress) return; // 중복 검색 방지
  closeACDropdown($originAC);
  closeACDropdown($destAC);

  if (window.matchMedia && window.matchMedia("(max-width: 768px)").matches) {
    setSidebarOpen(true);
  }

  var origin = $origin.value.trim();
  var destination = $destination.value.trim();
  var datetimeVal = $datetimeInput.value;

  if (!origin) { showError("출발지를 입력해 주세요."); $origin.focus(); return; }
  if (!destination) { showError("도착지를 입력해 주세요."); $destination.focus(); return; }
  if (!datetimeVal) { showError("시각을 선택해 주세요."); $datetimeInput.focus(); return; }

  var now = Date.now();
  var wallDate = parseWallIsoMinutes(datetimeVal);
  var instant = wallDate && resolveServiceInstant(wallDate, now);
  if (!instant || instant.getTime() <= now) {
    showError(!wallDate ? "시각 형식이 올바르지 않습니다." :
      !instant ? "서비스 시간대에 존재하지 않는 시각입니다." :
      "현재 시점 이전으로는 조회를 할 수 없습니다.");
    $datetimeInput.focus();
    return;
  }
  var isoTime = instant.toISOString();

  var searchOriginSelection = _selectedOrigin;
  var searchDestSelection = _selectedDest;
  var searchMode = _currentMode;
  var searchRouteInputRevision = _routeInputRevision;
  if (_searchController) _searchController.abort();
  var controller = new AbortController();
  _searchController = controller;
  var signal = controller.signal;
  var isSearchStillCurrent = function () {
    return (
      _searchController === controller &&
      !signal.aborted &&
      _routeInputRevision === searchRouteInputRevision &&
      isCurrentSearchSnapshot(
        origin,
        destination,
        datetimeVal,
        searchOriginSelection,
        searchDestSelection,
        searchMode
      )
    );
  };

  _searchInProgress = true;
  hideCandidateTooltip(true);
  hideError();
  hideDatetimeInputTooltip();
  showLoading();
  $results.classList.add("hidden");
  $results.replaceChildren();

  try {
    // ── Step 1: 출발/도착 좌표를 병렬 resolve ──
    var originCoordsPromise = resolveRouteCriticalCoords(origin, searchOriginSelection, signal).then(function (coords) {
      if (!isSearchStillCurrent()) return coords;
      if (coords && hasStableSelection(searchOriginSelection) && !hasValidCoords(searchOriginSelection)) {
        searchOriginSelection = markSelectionCoordsResolved(searchOriginSelection, coords);
        _selectedOrigin = searchOriginSelection;
      }
      if (_map && coords) {
        setEndpointMarker("origin", [coords.lat, coords.lon], { color: "#03C75A", label: "출발" });
        fitBounds();
      }
      return coords;
    });
    var destCoordsPromise = resolveRouteCriticalCoords(destination, searchDestSelection, signal).then(function (coords) {
      if (!isSearchStillCurrent()) return coords;
      if (coords && hasStableSelection(searchDestSelection) && !hasValidCoords(searchDestSelection)) {
        searchDestSelection = markSelectionCoordsResolved(searchDestSelection, coords);
        _selectedDest = searchDestSelection;
      }
      if (_map && coords) {
        setEndpointMarker("dest", [coords.lat, coords.lon], { color: "#E53935", label: "도착" });
        fitBounds();
      }
      return coords;
    });
    var coordResults = await Promise.all([
      originCoordsPromise,
      destCoordsPromise,
    ]);
    var oCoords = coordResults[0];
    var dCoords = coordResults[1];
    if (!isSearchStillCurrent()) {
      console.info("search_stale: ignored changed inputs during coordinate resolution");
      return;
    }
    if (oCoords && hasStableSelection(searchOriginSelection) && !hasValidCoords(searchOriginSelection)) {
      searchOriginSelection = markSelectionCoordsResolved(searchOriginSelection, oCoords);
      _selectedOrigin = searchOriginSelection;
    }
    if (dCoords && hasStableSelection(searchDestSelection) && !hasValidCoords(searchDestSelection)) {
      searchDestSelection = markSelectionCoordsResolved(searchDestSelection, dCoords);
      _selectedDest = searchDestSelection;
    }
    var originSelectedWithoutCoords =
      hasStableSelection(searchOriginSelection) && !hasValidCoords(searchOriginSelection);
    var destSelectedWithoutCoords =
      hasStableSelection(searchDestSelection) && !hasValidCoords(searchDestSelection);
    var requestOrigin = getStableSearchQuery(origin, searchOriginSelection);
    var requestDestination = getStableSearchQuery(destination, searchDestSelection);
    var displayOrigin = getSelectionDisplayText(origin, searchOriginSelection);
    var displayDestination = getSelectionDisplayText(destination, searchDestSelection);

    if (!oCoords && !originSelectedWithoutCoords) {
      showError('"' + origin + '" 위치를 찾을 수 없습니다. 도로명주소 또는 역/지하철명으로 입력해 주세요.');
      return;
    }
    if (!dCoords && !destSelectedWithoutCoords) {
      showError('"' + destination + '" 위치를 찾을 수 없습니다. 도로명주소 또는 역/지하철명으로 입력해 주세요.');
      return;
    }
    if (!oCoords || !dCoords) {
      if (originSelectedWithoutCoords || destSelectedWithoutCoords) {
        console.warn("coords_unresolved: route-critical autocomplete selection blocked");
        showError(COORDS_UNRESOLVED_ROUTE_MESSAGE);
      } else {
        showError("출발지 또는 도착지 좌표를 확인할 수 없습니다. 다시 입력해 주세요.");
      }
      return;
    }

    // ── Step 2: 좌표 확보됨 → 지도 그리기 시작 ──
    clearMarkers();
    if (_map) {
      if (oCoords) {
        setEndpointMarker("origin", [oCoords.lat, oCoords.lon], { color: "#03C75A", label: "출발" });
      }
      if (dCoords) {
        setEndpointMarker("dest", [dCoords.lat, dCoords.lon], { color: "#E53935", label: "도착" });
      }
      fitBounds();
    }
    void fetchAndDrawRoute(
      oCoords.lat, oCoords.lon, dCoords.lat, dCoords.lon, isSearchStillCurrent, signal
    );

    var routeBody = { origin: requestOrigin, destination: requestDestination };
    attachRouteContractFields(
      routeBody, requestOrigin, requestDestination, oCoords, dCoords,
      searchOriginSelection, searchDestSelection
    );
    var context = _renderContext(searchMode);
    var baselineData = null;
    var immediateSafe = null;
    var progress = null;

    function renderPending() {
      if (!isSearchStillCurrent()) return;
      $loading.classList.add("hidden");
      renderDeparturePending(baselineData, context, immediateSafe, progress);
    }

    function receiveBaseline(data, safe, currentProgress) {
      if (!isSearchStillCurrent()) return;
      baselineData = withDisplayRoute(data, displayOrigin, displayDestination);
      if (baselineData) $searchStatus.textContent = "기준 조회 결과가 나왔습니다. 추천 출발 시각을 계산하고 있습니다.";
      immediateSafe = safe || null;
      progress = currentProgress || progress;
      renderPending();
    }

    var handlers = {
      onArrival: receiveBaseline,
      onProgress: function (currentProgress) {
        if (!isSearchStillCurrent()) return;
        progress = currentProgress || progress;
        if (baselineData) renderPending();
      },
    };

    var streamPath = "arrival-time-with-recommendation/stream";
    var streamBody = { ...routeBody, departure_time: isoTime };
    if (searchMode === "departure") {
      // The baseline and recommendation share server capacity.
      var baselineDeparture = new Date(Math.ceil(Date.now() / 60000) * 60000);
      var baseline = null;
      try {
        var baselineResponse = await fetchTrip("arrival-time", {
          ...routeBody, departure_time: baselineDeparture.toISOString(),
        }, signal);
        baseline = await baselineResponse.json().catch(function (error) {
          signal.throwIfAborted();
          if (error.name === "AbortError") throw error;
          return null;
        });
      } catch (baselineError) {
        if (!isSearchStillCurrent()) return;
        if (!canSkipDepartureBaseline(baselineError)) throw baselineError;
      }
      if (!isSearchStillCurrent()) return;
      if (hasUsableDepartureBaseline(baseline)) {
        receiveBaseline(baseline);
      } else {
        $searchStatus.textContent = DEPARTURE_BASELINE_UNAVAILABLE + " 추천 출발 시각을 계산하고 있습니다.";
      }
      streamPath = "recommended-departure-time/stream";
      streamBody = { ...routeBody, desired_arrival_time: isoTime };
    }

    try {
      var recommendation = await readTripStream(streamPath, streamBody, signal, handlers);
      if (!isSearchStillCurrent()) return;
      renderDepartureResult(
        withDisplayRoute(recommendation, displayOrigin, displayDestination),
        baselineData, context, immediateSafe
      );
      $searchStatus.textContent = "추천 출발 시각을 확인할 수 있습니다." +
        (searchMode === "departure" && !baselineData ? " " + DEPARTURE_BASELINE_UNAVAILABLE : "");
    } catch (streamError) {
      if (!isSearchStillCurrent()) return;
      if (!baselineData) throw streamError;
      $results.innerHTML = buildBaselineArrivalCard(baselineData, context) +
        (immediateSafe ? buildImmediateSafeCard(immediateSafe) : "");
      $results.classList.remove("hidden");
      showError("기준 조회 결과를 표시합니다. 추천 출발 시각을 계산하지 못했습니다. " + streamError.message, {
        keepResults: true,
        retry: streamError.retryable !== false,
      });
    }

    // ── Step 3: 최근 검색 저장 ──
    if (!isSearchStillCurrent()) return;
    addRecentSearch(
      displayOrigin,
      displayDestination,
      hasStableSelection(searchOriginSelection)
        ? buildStableSelection(searchOriginSelection, {
            display_name: requestOrigin,
            address: requestOrigin,
            canonical_query: requestOrigin,
          })
        : oCoords ? buildStableSelection(
            {
              lat: oCoords.lat,
              lon: oCoords.lon,
              display_name: requestOrigin,
              address: requestOrigin,
              canonical_query: requestOrigin,
              selection_kind: "poi",
              source: "geocode",
            },
            {
              display_name: requestOrigin,
              address: requestOrigin,
              canonical_query: requestOrigin,
              selection_kind: "poi",
              source: "geocode",
            }
          ) : null,
      hasStableSelection(searchDestSelection)
        ? buildStableSelection(searchDestSelection, {
            display_name: requestDestination,
            address: requestDestination,
            canonical_query: requestDestination,
          })
        : dCoords ? buildStableSelection(
            {
              lat: dCoords.lat,
              lon: dCoords.lon,
              display_name: requestDestination,
              address: requestDestination,
              canonical_query: requestDestination,
              selection_kind: "poi",
              source: "geocode",
            },
            {
              display_name: requestDestination,
              address: requestDestination,
              canonical_query: requestDestination,
              selection_kind: "poi",
              source: "geocode",
            }
          ) : null
    );

    // Bottom sheet stays expanded so results remain visible.
  } catch (err) {
    if (!isSearchStillCurrent()) return;
    showError(err.message || "알 수 없는 오류가 발생했습니다.", {
      retry: err.retryable !== false,
    });
  } finally {
    if (isSearchStillCurrent()) {
      _searchInProgress = false;
      hideLoading();
    }
  }
}

$searchBtn.addEventListener("click", handleSearch);
$retrySearch.addEventListener("click", handleSearch);

function onDatetimeInputEdit() {
  var selected = _datetimePickerState.selected;
  if (selected && toWallIsoMinutes(selected) === $datetimeInput.value) return;
  invalidateSearchOnlyState();
  syncDatetimeStateFromInputValue();
  hideDatetimeInputTooltip();
}
$datetimeInput.addEventListener("input", onDatetimeInputEdit);
$datetimeInput.addEventListener("change", onDatetimeInputEdit);

// Enter key in inputs triggers search (autocomplete 미선택 시)
[$origin, $destination, $datetimeInput].forEach(function (el) {
  el.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      if (_searchInProgress) return; // 이미 검색 중이면 무시
      // autocomplete dropdown이 열려 있고 아이템이 선택된 경우 무시
      var acOpen = false;
      if (el === $origin) acOpen = !$originAC.classList.contains("hidden");
      if (el === $destination) acOpen = !$destAC.classList.contains("hidden");
      if (!acOpen) {
        e.preventDefault();
        handleSearch();
      }
    }
  });
});

// 최근 검색 전체 삭제
document.getElementById("recent-clear").addEventListener("click", function () {
  clearRecentSearches();
});

/* ── Autocomplete Setup ──────────────────────────── */
// Autocomplete v2 is the single owner for input/composition/debounce/dropdown
// state.  The legacy setupAutocomplete implementation remains above as shared
// historical code and helper context, but it must not mount listeners because
// doing so creates dual controller races (especially blur/timer/dropdown).

/* ── Bootstrap ────────────────────────────────────── */

async function bootstrap() {
  try {
    _config = { ..._config, ...await fetchConfig() };
    if (Number.isFinite(Number(_config.step_minutes)) && Number(_config.step_minutes) > 0) {
      DATETIME_MINUTE_STEP = Number(_config.step_minutes);
    }
    setProviderName(_config.provider);
    setVersionBadge(_config.version);
  } catch (_) {
    // config endpoint failed; continue with the compiled default step.
    setProviderName("unknown");
    setVersionBadge("v0.0.0.0");
  }

  document.getElementById("service-timezone").textContent = "기준 시간대 " + _config.timezone;
  $datetimeInput.value = defaultDatetimeValue();
  initDatetimePicker();
  setInterval(function () {
    if (_datetimePickerState.isOpen) renderDatetimeCalendar();
  }, 30000);

  initMap();
  renderRecentSearches();
}

bootstrap();
