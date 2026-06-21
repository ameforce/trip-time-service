"use strict";

/* ── State ────────────────────────────────────────── */
let _map = null;
let _markers = [];
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
let _searchInProgress = false; // 중복 검색 방지 플래그
let _routeInputRevision = 0;

// geocode 결과 캐시 (address → {lat, lon})
var _geocodeCache = {};

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
const $loading = document.getElementById("loading");
const $mapEl = document.getElementById("map");
const $mapPlaceholder = document.getElementById("map-placeholder");
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

let DATETIME_MINUTE_STEP = 10;
const MAX_AUTOCOMPLETE_WARMUP_QUERIES = 8;
let _datetimeMinIso = "";
let _datetimePickerState = {
  selected: null,
  viewYear: 0,
  viewMonth: 0,
  isOpen: false,
};
let _datetimeRefocusFromValidation = false;

/* ── Helpers ──────────────────────────────────────── */

function formatDuration(seconds) {
  var normalized = Number(seconds);
  if (!isFinite(normalized) || normalized < 0) {
    normalized = 0;
  }
  var h = Math.floor(normalized / 3600);
  var m = Math.floor((normalized % 3600) / 60);
  if (h > 0 && m > 0) return h + "시간 " + m + "분";
  if (h > 0) return h + "시간";
  return m + "분";
}

function formatDateParts(dateObj) {
  var month = String(dateObj.getMonth() + 1).padStart(2, "0");
  var day = String(dateObj.getDate()).padStart(2, "0");
  var hours = String(dateObj.getHours()).padStart(2, "0");
  var mins = String(dateObj.getMinutes()).padStart(2, "0");
  return dateObj.getFullYear() + "-" + month + "-" + day + " " + hours + ":" + mins;
}

function formatDatetime(isoStr) {
  var d = new Date(isoStr);
  if (isNaN(d.getTime())) {
    return "-";
  }
  return formatDateParts(d);
}

function floorDateToMinuteStep(dateObj, stepMinutes) {
  var floored = new Date(dateObj.getTime());
  floored.setSeconds(0, 0);
  var remainder = floored.getMinutes() % stepMinutes;
  if (remainder !== 0) {
    floored.setMinutes(floored.getMinutes() - remainder, 0, 0);
  }
  return floored;
}

function ceilDurationSecondsToMinuteStep(durationSeconds, stepMinutes) {
  if (durationSeconds == null) {
    return null;
  }
  var normalized = Number(durationSeconds);
  if (!isFinite(normalized) || normalized < 0) {
    return null;
  }
  if (normalized === 0) {
    return 0;
  }
  var stepSeconds = Math.max(1, stepMinutes) * 60;
  return Math.ceil(normalized / stepSeconds) * stepSeconds;
}

function formatRoundedDatetime(isoStr) {
  if (!isoStr) {
    return "-";
  }
  var d = new Date(isoStr);
  if (isNaN(d.getTime())) {
    return "-";
  }
  var rounded = floorDateToMinuteStep(d, DATETIME_MINUTE_STEP);
  return formatDateParts(rounded);
}

function formatRoundedDuration(durationSeconds) {
  if (durationSeconds == null) {
    return "-";
  }
  var roundedSeconds = ceilDurationSecondsToMinuteStep(
    durationSeconds,
    DATETIME_MINUTE_STEP
  );
  if (roundedSeconds == null) {
    return "-";
  }
  return formatDuration(roundedSeconds);
}

function formatScorePercent(score) {
  if (typeof score !== "number" || !isFinite(score)) {
    return "-";
  }
  var normalized = Math.min(1, Math.max(0, score));
  return (normalized * 100).toFixed(1);
}

function formatArrivalDelta(recommendedArrivalIso, baselineArrivalIso) {
  var recommendedArrival = new Date(recommendedArrivalIso);
  var baselineArrival = new Date(baselineArrivalIso);
  if (
    isNaN(recommendedArrival.getTime()) ||
    isNaN(baselineArrival.getTime())
  ) {
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

function formatUtcOffset(offsetMinutes) {
  var sign = offsetMinutes >= 0 ? "+" : "-";
  var abs = Math.abs(offsetMinutes);
  var hours = String(Math.floor(abs / 60)).padStart(2, "0");
  var minutes = String(abs % 60).padStart(2, "0");
  return sign + hours + ":" + minutes;
}

function resolveTimezoneOffsetMinutes(dateObj) {
  var timezone = _config && _config.timezone ? _config.timezone : null;
  if (timezone && typeof Intl !== "undefined" && Intl.DateTimeFormat) {
    try {
      var formatter = new Intl.DateTimeFormat("en-US", {
        timeZone: timezone,
        timeZoneName: "shortOffset",
      });
      var parts = formatter.formatToParts(dateObj);
      var zonePart = "";
      for (var i = 0; i < parts.length; i++) {
        if (parts[i].type === "timeZoneName") {
          zonePart = parts[i].value || "";
          break;
        }
      }
      var match = zonePart.match(/GMT([+-])(\d{1,2})(?::?(\d{2}))?/i);
      if (match) {
        var sign = match[1] === "-" ? -1 : 1;
        var hours = parseInt(match[2], 10) || 0;
        var minutes = parseInt(match[3] || "0", 10) || 0;
        return sign * (hours * 60 + minutes);
      }
    } catch (_) {
      // fallback below
    }
  }
  return -dateObj.getTimezoneOffset();
}

function toApiDatetimeString(localDatetimeValue) {
  var parsed = parseLocalIsoMinutes(localDatetimeValue);
  if (!parsed) {
    return null;
  }
  var offsetMinutes = resolveTimezoneOffsetMinutes(parsed);
  return localDatetimeValue + ":00" + formatUtcOffset(offsetMinutes);
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

function toDisplayAlignedIsoDatetime(isoStr) {
  var parsed = parseIsoDatetime(isoStr);
  if (!parsed) {
    return null;
  }
  return floorDateToMinuteStep(parsed, DATETIME_MINUTE_STEP).toISOString();
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
    "출발: " + formatRoundedDatetime(options.departureIso),
    "도착: " + formatRoundedDatetime(options.arrivalIso),
    "출발지: " + origin,
    "도착지: " + destination,
    "희망 도착: " + formatRoundedDatetime(options.desiredArrivalIso),
    "예상 소요시간: " + formatRoundedDuration(options.durationSeconds),
  ];
  var alignedStartIso = toDisplayAlignedIsoDatetime(options.departureIso);
  var alignedEndIso = toDisplayAlignedIsoDatetime(options.arrivalIso);
  var alignedDurationSeconds = ceilDurationSecondsToMinuteStep(
    options.durationSeconds,
    DATETIME_MINUTE_STEP
  );

  return {
    title: "[Trip Time] " + label + " - " + origin + " -> " + destination,
    startIso: alignedStartIso || options.departureIso,
    endIso: alignedEndIso || options.arrivalIso,
    durationSeconds: alignedDurationSeconds != null
      ? alignedDurationSeconds
      : options.durationSeconds,
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
  if (!isMockProvider(provider)) {
    return "";
  }
  return (
    '<div class="result-card provider-notice-card">' +
    '  <div class="provider-notice-title">&#9888; mock 모드 결과 안내</div>' +
    '  <p class="provider-notice-copy">' +
         "현재 소요시간은 실시간 교통이 아닌 테스트용 가상 데이터입니다. " +
         "실제 교통 기준 분석이 필요하면 naver_selenium provider로 실행해 주세요." +
    "</p>" +
    "</div>"
  );
}

function setProviderName(provider) {
  if (!$providerBadge && !$providerWarning) {
    return;
  }
  var providerName = resolveProviderName(provider);
  if ($providerBadge) {
    $providerBadge.textContent = providerName;
    $providerBadge.classList.toggle("is-warning", providerName === "mock");
  }
  if ($providerWarning) {
    if (providerName === "mock") {
      $providerWarning.classList.remove("hidden");
    } else {
      $providerWarning.classList.add("hidden");
    }
  }
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
function nowCeilToStep() {
  var d = new Date();
  var m = d.getMinutes();
  var ceil = Math.ceil(m / DATETIME_MINUTE_STEP) * DATETIME_MINUTE_STEP;
  if (ceil === m && (d.getSeconds() > 0 || d.getMilliseconds() > 0)) {
    ceil += DATETIME_MINUTE_STEP;
  }
  d.setMinutes(ceil, 0, 0);
  return toLocalIsoMinutes(d);
}

function toLocalIsoMinutes(dateObj) {
  var offset = dateObj.getTimezoneOffset();
  var local = new Date(dateObj.getTime() - offset * 60000);
  return local.toISOString().slice(0, 16);
}

function parseLocalIsoMinutes(value) {
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
  var parsed = new Date(year, month - 1, day, hour, minute, 0, 0);

  if (
    parsed.getFullYear() !== year ||
    parsed.getMonth() !== month - 1 ||
    parsed.getDate() !== day ||
    parsed.getHours() !== hour ||
    parsed.getMinutes() !== minute
  ) {
    return null;
  }
  return parsed;
}

function parseFlexibleDatetimeInput(value, fallbackDate) {
  if (!value) {
    return null;
  }

  var strict = parseLocalIsoMinutes(value);
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
      : new Date();
  var year;
  var month;
  var day;
  var hour = base.getHours();
  var minute = base.getMinutes();

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

function ceilDateToMinuteStep(dateObj, stepMinutes) {
  var rounded = new Date(dateObj.getTime());
  rounded.setSeconds(0, 0);
  var minute = rounded.getMinutes();
  var ceil = Math.ceil(minute / stepMinutes) * stepMinutes;
  if (ceil !== minute) {
    rounded.setMinutes(ceil, 0, 0);
  }
  return rounded;
}

function clampNumber(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function getDatetimeMinDate() {
  if (!_datetimeMinIso) {
    _datetimeMinIso = nowCeilToStep();
  }
  return parseLocalIsoMinutes(_datetimeMinIso);
}

function getSelectedDatetimeOrNow() {
  if (_datetimePickerState.selected) {
    return new Date(_datetimePickerState.selected.getTime());
  }
  var parsed = parseFlexibleDatetimeInput($datetimeInput.value, new Date());
  if (parsed) {
    return parsed;
  }
  var minDate = getDatetimeMinDate();
  return minDate ? new Date(minDate.getTime()) : new Date();
}

function syncDatetimeDateFields() {
  if (!$datetimeYear || !$datetimeMonth || !$datetimeDay) {
    return;
  }
  var selected = getSelectedDatetimeOrNow();
  $datetimeYear.value = String(selected.getFullYear());
  $datetimeMonth.value = String(selected.getMonth() + 1).padStart(2, "0");
  $datetimeDay.value = String(selected.getDate()).padStart(2, "0");
}

function syncDatetimeTimeFields() {
  if (!$datetimePeriod || !$datetimeHour || !$datetimeMinute) {
    return;
  }
  var selected = getSelectedDatetimeOrNow();
  var hours24 = selected.getHours();
  var period = hours24 >= 12 ? "pm" : "am";
  var hours12 = hours24 % 12;
  if (hours12 === 0) {
    hours12 = 12;
  }
  $datetimePeriod.value = period;
  $datetimeHour.value = String(hours12).padStart(2, "0");
  $datetimeMinute.value = String(selected.getMinutes()).padStart(2, "0");
}

function renderDatetimeCalendar() {
  if (!$datetimeCalendarTitle || !$datetimeCalendarDays) {
    return;
  }

  var viewYear = _datetimePickerState.viewYear;
  var viewMonth = _datetimePickerState.viewMonth;
  $datetimeCalendarTitle.textContent =
    viewYear +
    "년 " +
    String(viewMonth + 1).padStart(2, "0") +
    "월";

  var firstWeekday = new Date(viewYear, viewMonth, 1).getDay();
  var daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate();
  var selected = getSelectedDatetimeOrNow();
  var minDate = getDatetimeMinDate();
  var minDateOnly = minDate
    ? new Date(minDate.getFullYear(), minDate.getMonth(), minDate.getDate())
    : null;
  var today = new Date();
  today.setHours(0, 0, 0, 0);

  var html = "";
  for (var i = 0; i < firstWeekday; i++) {
    html += '<span class="datetime-day-empty"></span>';
  }
  for (var day = 1; day <= daysInMonth; day++) {
    var cls = "datetime-day-btn";
    var cellDate = new Date(viewYear, viewMonth, day, 0, 0, 0, 0);
    var isSelected =
      selected.getFullYear() === viewYear &&
      selected.getMonth() === viewMonth &&
      selected.getDate() === day;
    var isToday = cellDate.getTime() === today.getTime();
    var disabled =
      minDateOnly && cellDate.getTime() < minDateOnly.getTime();
    if (isSelected) {
      cls += " is-selected";
    }
    if (isToday) {
      cls += " is-today";
    }
    html +=
      '<button class="' +
      cls +
      '" type="button" data-day="' +
      day +
      '"' +
      (disabled ? " disabled" : "") +
      ">" +
      day +
      "</button>";
  }
  $datetimeCalendarDays.innerHTML = html;
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
  var normalized = ceilDateToMinuteStep(dateObj, DATETIME_MINUTE_STEP);
  if (opts.enforceMin !== false) {
    var minDate = getDatetimeMinDate();
    if (minDate && normalized.getTime() < minDate.getTime()) {
      normalized = new Date(minDate.getTime());
    }
  }

  _datetimePickerState.selected = normalized;
  if (!opts.keepView) {
    _datetimePickerState.viewYear = normalized.getFullYear();
    _datetimePickerState.viewMonth = normalized.getMonth();
  }
  $datetimeInput.value = toLocalIsoMinutes(normalized);
  renderDatetimePicker();
}

function syncDatetimePickerFromInput(options) {
  var fallbackDate =
    _datetimePickerState.selected ||
    parseLocalIsoMinutes(nowCeilToStep()) ||
    new Date();
  var parsed = parseFlexibleDatetimeInput($datetimeInput.value, fallbackDate);
  if (!parsed) {
    parsed = parseLocalIsoMinutes(nowCeilToStep());
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
    parseLocalIsoMinutes(nowCeilToStep()) ||
    new Date();
  var parsed = parseFlexibleDatetimeInput($datetimeInput.value, fallbackDate);
  if (!parsed) {
    return false;
  }

  _datetimePickerState.selected = new Date(parsed.getTime());
  _datetimePickerState.viewYear = parsed.getFullYear();
  _datetimePickerState.viewMonth = parsed.getMonth();
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
    parseLocalIsoMinutes(nowCeilToStep()) ||
    new Date();
  var selected = parseFlexibleDatetimeInput($datetimeInput.value, fallbackDate);
  if (!selected) {
    hideDatetimeInputTooltip();
    return true;
  }

  var now = new Date();
  if (selected.getTime() > now.getTime()) {
    hideDatetimeInputTooltip();
    return true;
  }

  var selectedDateOnly = new Date(
    selected.getFullYear(),
    selected.getMonth(),
    selected.getDate()
  );
  var nowDateOnly = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate()
  );
  var segment =
    selectedDateOnly.getTime() < nowDateOnly.getTime() ? "date" : "time";
  showDatetimeInputTooltip(
    "현재 시점 이전으로는 조회를 할 수 없습니다.",
    segment
  );
  return false;
}

function moveDatetimeMonth(delta) {
  var base = new Date(
    _datetimePickerState.viewYear,
    _datetimePickerState.viewMonth + delta,
    1
  );
  _datetimePickerState.viewYear = base.getFullYear();
  _datetimePickerState.viewMonth = base.getMonth();
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
  var maxDay = new Date(year, month, 0).getDate();
  day = clampNumber(day, 1, maxDay);

  var selected = getSelectedDatetimeOrNow();
  var nextDate = new Date(
    year,
    month - 1,
    day,
    selected.getHours(),
    selected.getMinutes(),
    0,
    0
  );
  setDatetimeFromDate(nextDate, { enforceMin: true });
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
  var nextDate = new Date(
    selected.getFullYear(),
    selected.getMonth(),
    selected.getDate(),
    hour24,
    minute,
    0,
    0
  );
  setDatetimeFromDate(nextDate, { keepView: true, enforceMin: true });
}

function positionDatetimePanel() {
  if (!$datetimePanel || !$datetimePicker || !_datetimePickerState.isOpen) {
    return;
  }
  positionDatetimeInputTooltip();
  var anchorRect = $datetimePicker.getBoundingClientRect();
  var panelRect = $datetimePanel.getBoundingClientRect();
  var gap = 8;
  var margin = 8;
  var viewportWidth = window.innerWidth;
  var viewportHeight = window.innerHeight;
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
  var maxTop = viewportHeight - margin - panelRect.height;
  if (top > maxTop) {
    top = Math.max(margin, anchorRect.top - panelRect.height - gap);
  }
  if (top < margin) {
    top = margin;
  }

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
  $input.addEventListener("input", sanitizeDateFieldInput);
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
  for (var minute = 0; minute < 60; minute += DATETIME_MINUTE_STEP) {
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
      if ($datetimePicker && $datetimePicker.contains(document.activeElement)) {
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
    var nextDate = new Date(
      _datetimePickerState.viewYear,
      _datetimePickerState.viewMonth,
      day,
      selected.getHours(),
      selected.getMinutes(),
      0,
      0
    );
    setDatetimeFromDate(nextDate, { enforceMin: true });
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

/**
 * 현재 시각 하한값을 내부 상태로 갱신.
 */
function enforceMinDatetime() {
  _datetimeMinIso = nowCeilToStep();
}

/**
 * 선택된 시각이 과거인지 검사하고, 과거면 자동 보정.
 * 반환값: { corrected, invalid } 객체.
 */
function validateAndCeilDatetime() {
  var val = $datetimeInput.value;
  if (!val) {
    return { corrected: null, invalid: false };
  }
  var fallbackDate =
    _datetimePickerState.selected ||
    parseLocalIsoMinutes(nowCeilToStep()) ||
    new Date();
  var selected = parseFlexibleDatetimeInput(val, fallbackDate);
  if (!selected) {
    return { corrected: null, invalid: true };
  }

  var now = new Date();
  var correctedDate = null;
  if (selected <= now) {
    correctedDate = parseLocalIsoMinutes(nowCeilToStep());
  } else {
    var ceilSelected = ceilDateToMinuteStep(selected, DATETIME_MINUTE_STEP);
    if (ceilSelected.getTime() !== selected.getTime()) {
      correctedDate = ceilSelected;
    }
  }

  if (correctedDate) {
    var minDate = getDatetimeMinDate();
    if (minDate && correctedDate.getTime() < minDate.getTime()) {
      correctedDate = minDate;
    }
    var corrected = toLocalIsoMinutes(correctedDate);
    setDatetimeFromDate(correctedDate, { enforceMin: true });
    return { corrected: corrected, invalid: false };
  }

  setDatetimeFromDate(selected, { keepView: true, enforceMin: true });
  return { corrected: null, invalid: false };
}

function showError(msg) {
  $errorMsg.textContent = msg;
  $errorBox.classList.remove("hidden");
  $results.classList.add("hidden");
}

function hideError() {
  $errorBox.classList.add("hidden");
}

function showLoading() {
  $loading.classList.remove("hidden");
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

function addFavorite(name, address, lat, lon) {
  var list = loadJSON(FAV_KEY, []);
  if (list.length >= FAV_MAX) return false;
  // 중복 체크
  for (var i = 0; i < list.length; i++) {
    if (list[i].name === name) return false;
  }
  list.push({ name: name, address: address, lat: lat, lon: lon });
  saveJSON(FAV_KEY, list);
  renderFavorites();
  return true;
}

function removeFavorite(idx) {
  var list = loadJSON(FAV_KEY, []);
  list.splice(idx, 1);
  saveJSON(FAV_KEY, list);
  renderFavorites();
}

function getFavorites() {
  return loadJSON(FAV_KEY, []);
}

/* ── Map (Leaflet + OpenStreetMap) ────────────────── */

function initMap() {
  $mapPlaceholder.classList.add("hidden");
  _map = L.map($mapEl, { zoomControl: true }).setView([37.5665, 126.978], 12);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    maxZoom: 19,
  }).addTo(_map);
}

function clearMarkers() {
  _markers.forEach(function (m) { _map.removeLayer(m); });
  _markers = [];
  if (_routeGroup) {
    _map.removeLayer(_routeGroup);
    _routeGroup = null;
  }
}

function invalidateRouteInputState() {
  _routeInputRevision += 1;
  _searchInProgress = false;
  if (_map) {
    clearMarkers();
  }
  $results.classList.add("hidden");
  hideLoading();
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
function geocodeQueryToCoords(query, cacheAlias) {
  var geocodeQuery = String(query || "").trim();
  if (!geocodeQuery) return Promise.resolve(null);
  if (_geocodeCache[geocodeQuery]) return Promise.resolve(_geocodeCache[geocodeQuery]);
  return fetch(
    "/api/geocode?q=" +
      encodeURIComponent(geocodeQuery) +
      buildMapCenterQueryString()
  )
    .then(function (res) { return res.json(); })
    .then(function (data) {
      if (!data || data.length === 0) return null;
      var coords = { lat: parseFloat(data[0].lat), lon: parseFloat(data[0].lon) };
      _geocodeCache[geocodeQuery] = coords;
      if (cacheAlias && cacheAlias !== geocodeQuery) _geocodeCache[cacheAlias] = coords;
      if (data[0].source) {
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

function resolveCoords(address, selectedCoords) {
  var normalizedSelectedCoords = normalizeCoords(selectedCoords);
  if (normalizedSelectedCoords) {
    return Promise.resolve(normalizedSelectedCoords);
  }
  if (hasStableSelection(selectedCoords) && selectedCoords.coords_ready === false) {
    return Promise.resolve(null);
  }
  var geocodeQuery = String(address || "").trim();
  return geocodeQuery ? geocodeQueryToCoords(geocodeQuery, address) : Promise.resolve(null);
}

function resolveRouteCriticalCoords(address, selectedCoords) {
  var normalizedSelectedCoords = normalizeCoords(selectedCoords);
  if (normalizedSelectedCoords) {
    return Promise.resolve(normalizedSelectedCoords);
  }
  if (hasStableSelection(selectedCoords)) {
    var query = getStableSearchQuery(address, selectedCoords);
    if (!query) return Promise.resolve(null);
    return geocodeQueryToCoords(query, address);
  }
  return resolveCoords(address, selectedCoords);
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

function fetchAndDrawRoute(lat1, lon1, lat2, lon2, isCurrentRoute) {
  if (!_map) return Promise.resolve();
  if (typeof isCurrentRoute === "function" && !isCurrentRoute()) {
    return Promise.resolve();
  }
  var url = "/api/route?olat=" + lat1 + "&olon=" + lon1 +
            "&dlat=" + lat2 + "&dlon=" + lon2;
  return fetch(url)
    .then(function (res) { return res.json(); })
    .then(function (data) {
      if (typeof isCurrentRoute === "function" && !isCurrentRoute()) {
        return;
      }
      if (!data.routes || !data.routes.length) return;
      var geom = data.routes[0].geometry;
      var border = L.geoJSON(geom, {
        style: { color: "#0D47A1", weight: 9, opacity: 0.45, lineCap: "round", lineJoin: "round" },
      });
      var fill = L.geoJSON(geom, {
        style: { color: "#2979FF", weight: 5, opacity: 0.9, lineCap: "round", lineJoin: "round" },
      });
      _routeGroup = L.layerGroup([border, fill]).addTo(_map);
      fitBounds();
    })
    .catch(function (err) { console.error("route failed:", err); });
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
      addMarker([originCoords.lat, originCoords.lon], { color: "#03C75A", label: "출발" });
    }
    if (destCoords) {
      addMarker([destCoords.lat, destCoords.lon], { color: "#E53935", label: "도착" });
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
  if (!_map || typeof _map.getCenter !== "function") return "";
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
  if (!_map || typeof _map.getCenter !== "function") return normalized + "|nomap";
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

function queueAutocompleteWarmup() {
  if (_autocompleteWarmupQueued) return;
  var warmupQueries = collectWarmupQueries();
  _autocompleteWarmupQueued = true;

  var requestBody = {
    queries: warmupQueries,
  };
  if (_map && typeof _map.getCenter === "function") {
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
    $dropdown.classList.add("hidden");
    $dropdown.innerHTML = "";
    return;
  }
  var html = "";
  for (var i = 0; i < items.length; i++) {
    var it = items[i];
    var safeType = escapeHtml(it.type || "");
    var safeDisplayName = escapeHtml(it.display_name || it.address || "");
    var safeAddress = escapeHtml(it.address || "");
    var typeBadge = safeType ? '<span class="ac-item-type">' + safeType + "</span>" : "";
    html +=
      '<div class="ac-item" data-idx="' + i + '">' +
      '  <div class="ac-item-name">' + safeDisplayName + typeBadge + "</div>" +
      '  <div class="ac-item-addr">' + safeAddress + "</div>" +
      "</div>";
  }
  $dropdown.innerHTML = html;
  $dropdown.classList.remove("hidden");

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
  $dropdown.innerHTML = "";
}

function applyAutocompleteSelection($input, $dropdown, setSelected, item) {
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
  refreshMapMarkersLive();
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
    invalidateRouteInputState();
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
          applyAutocompleteSelection($input, $dropdown, setSelected, item);
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
      applyAutocompleteSelection($input, $dropdown, setSelected, currentItems[activeIdx]);
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

async function apiEstimateArrival(
  origin,
  destination,
  departureTime,
  oCoords,
  dCoords,
  originSelection,
  destSelection
) {
  var body = {
    origin: origin,
    destination: destination,
    departure_time: departureTime,
  };
  attachRouteContractFields(
    body,
    origin,
    destination,
    oCoords,
    dCoords,
    originSelection,
    destSelection
  );

  var res = await fetch("/v1/trip/arrival-time", {
    method: "POST",
    headers: strictRouteContractHeaders(),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    var err = await res.json().catch(function () { return {}; });
    throw new Error(err.detail || "요청 처리 중 오류가 발생했습니다 (" + res.status + ")");
  }
  return res.json();
}

async function apiRecommendDeparture(
  origin,
  destination,
  desiredArrivalTime,
  oCoords,
  dCoords,
  originSelection,
  destSelection
) {
  var body = {
    origin: origin,
    destination: destination,
    desired_arrival_time: desiredArrivalTime,
  };
  attachRouteContractFields(
    body,
    origin,
    destination,
    oCoords,
    dCoords,
    originSelection,
    destSelection
  );

  var res = await fetch("/v1/trip/recommended-departure-time", {
    method: "POST",
    headers: strictRouteContractHeaders(),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    var err = await res.json().catch(function () { return {}; });
    throw new Error(err.detail || "요청 처리 중 오류가 발생했습니다 (" + res.status + ")");
  }
  return res.json();
}

async function apiStreamDepartureRecommendation(
  origin,
  destination,
  desiredArrivalTime,
  oCoords,
  dCoords,
  originSelection,
  destSelection,
  handlers
) {
  var body = {
    origin: origin,
    destination: destination,
    desired_arrival_time: desiredArrivalTime,
  };
  attachRouteContractFields(
    body,
    origin,
    destination,
    oCoords,
    dCoords,
    originSelection,
    destSelection
  );

  var res = await fetch("/v1/trip/recommended-departure-time/stream", {
    method: "POST",
    headers: strictRouteContractHeaders(),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    var err = await res.json().catch(function () { return {}; });
    throw new Error(err.detail || "스트림 요청 처리 중 오류가 발생했습니다 (" + res.status + ")");
  }
  if (!res.body) {
    throw new Error("브라우저가 스트리밍 응답을 지원하지 않습니다.");
  }

  var reader = res.body.getReader();
  var decoder = new TextDecoder("utf-8");
  var buffer = "";
  var state = {
    recommendation: null,
    candidates: [],
    progress: {
      checked: 0,
      planned: 0,
      remaining: 0,
      total_candidates: 0,
    },
  };

  function dispatchEvent(evtName, evtData) {
    if (evtName === "plan") {
      state.progress = evtData || state.progress;
      if (handlers && handlers.onPlan) {
        handlers.onPlan(state.progress);
      }
      return;
    }
    if (evtName === "candidate") {
      var candidatePayload = evtData.candidate || evtData;
      if (evtData.progress) {
        state.progress = evtData.progress;
      }
      state.candidates.push(candidatePayload);
      if (handlers && handlers.onCandidate) {
        handlers.onCandidate(
          candidatePayload,
          state.candidates.slice(),
          state.progress
        );
      }
      return;
    }
    if (evtName === "recommendation") {
      state.recommendation = evtData;
      state.progress = {
        checked: evtData.candidates_checked || state.progress.checked || 0,
        planned: evtData.planned_queries || state.progress.planned || 0,
        remaining: Math.max(
          0,
          (evtData.planned_queries || state.progress.planned || 0) -
            (evtData.candidates_checked || state.progress.checked || 0)
        ),
        total_candidates: evtData.total_candidates || state.progress.total_candidates || 0,
      };
      if (handlers && handlers.onRecommendation) {
        handlers.onRecommendation(
          evtData,
          state.candidates.slice(),
          state.progress
        );
      }
      return;
    }
    if (evtName === "error" || evtName === "busy") {
      var detail = (evtData && evtData.detail) || "추천 계산 스트림 오류";
      throw new Error(detail);
    }
    if (evtName === "end" && evtData && evtData.ok === false) {
      throw new Error(evtData.detail || "추천 계산 스트림 오류");
    }
  }

  function consumeBuffer() {
    var normalized = buffer.replace(/\r/g, "");
    var split = normalized.split("\n\n");
    if (split.length <= 1) {
      buffer = normalized;
      return;
    }
    buffer = split.pop() || "";

    for (var i = 0; i < split.length; i++) {
      var block = split[i].trim();
      if (!block) continue;
      var lines = block.split("\n");
      var evtName = "message";
      var dataLines = [];
      for (var j = 0; j < lines.length; j++) {
        var line = lines[j];
        if (line.indexOf("event:") === 0) {
          evtName = line.slice(6).trim();
        } else if (line.indexOf("data:") === 0) {
          dataLines.push(line.slice(5).trim());
        }
      }
      if (!dataLines.length) continue;
      var payloadText = dataLines.join("\n");
      var payload = {};
      try {
        payload = JSON.parse(payloadText);
      } catch (_) {
        payload = {};
      }
      dispatchEvent(evtName, payload);
    }
  }

  while (true) {
    var chunk = await reader.read();
    if (chunk.done) {
      break;
    }
    buffer += decoder.decode(chunk.value, { stream: true });
    consumeBuffer();
  }
  if (buffer.trim()) {
    buffer += "\n\n";
    consumeBuffer();
  }

  if (!state.recommendation) {
    throw new Error("추천 결과를 수신하지 못했습니다.");
  }
  if (
    state.candidates.length > 0 &&
    (
      !state.recommendation.candidate_evaluations ||
      state.recommendation.candidate_evaluations.length === 0
    )
  ) {
    state.recommendation.candidate_evaluations = state.candidates.slice();
  }
  return state;
}

async function apiStreamArrivalWithRecommendation(
  origin,
  destination,
  departureTime,
  oCoords,
  dCoords,
  originSelection,
  destSelection,
  handlers
) {
  var body = {
    origin: origin,
    destination: destination,
    departure_time: departureTime,
  };
  attachRouteContractFields(
    body,
    origin,
    destination,
    oCoords,
    dCoords,
    originSelection,
    destSelection
  );

  var res = await fetch("/v1/trip/arrival-time-with-recommendation/stream", {
    method: "POST",
    headers: strictRouteContractHeaders(),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    var err = await res.json().catch(function () { return {}; });
    throw new Error(err.detail || "스트림 요청 처리 중 오류가 발생했습니다 (" + res.status + ")");
  }
  if (!res.body) {
    throw new Error("브라우저가 스트리밍 응답을 지원하지 않습니다.");
  }

  var reader = res.body.getReader();
  var decoder = new TextDecoder("utf-8");
  var buffer = "";
  var state = {
    arrival: null,
    immediate_safe_departure: null,
    recommendation: null,
    candidates: [],
    progress: {
      checked: 0,
      planned: 0,
      remaining: 0,
      total_candidates: 0,
    },
  };

  function dispatchEvent(evtName, evtData) {
    if (evtName === "arrival") {
      state.arrival = evtData.arrival || null;
      state.immediate_safe_departure = evtData.immediate_safe_departure || null;
      if (evtData.progress) {
        state.progress = evtData.progress;
      }
      if (handlers && handlers.onArrival) {
        handlers.onArrival(
          state.arrival,
          state.immediate_safe_departure,
          state.progress
        );
      }
      return;
    }
    if (evtName === "plan") {
      state.progress = evtData || state.progress;
      if (handlers && handlers.onPlan) {
        handlers.onPlan(state.progress);
      }
      return;
    }
    if (evtName === "candidate") {
      var candidatePayload = evtData.candidate || evtData;
      if (evtData.progress) {
        state.progress = evtData.progress;
      }
      state.candidates.push(candidatePayload);
      if (handlers && handlers.onCandidate) {
        handlers.onCandidate(
          candidatePayload,
          state.candidates.slice(),
          state.progress
        );
      }
      return;
    }
    if (evtName === "recommendation") {
      state.recommendation = evtData;
      state.progress = {
        checked: evtData.candidates_checked || state.progress.checked || 0,
        planned: evtData.planned_queries || state.progress.planned || 0,
        remaining: Math.max(
          0,
          (evtData.planned_queries || state.progress.planned || 0) -
            (evtData.candidates_checked || state.progress.checked || 0)
        ),
        total_candidates: evtData.total_candidates || state.progress.total_candidates || 0,
      };
      if (handlers && handlers.onRecommendation) {
        handlers.onRecommendation(
          evtData,
          state.candidates.slice(),
          state.progress
        );
      }
      return;
    }
    if (evtName === "error" || evtName === "busy") {
      var detail = (evtData && evtData.detail) || "추천 계산 스트림 오류";
      throw new Error(detail);
    }
    if (evtName === "end" && evtData && evtData.ok === false) {
      throw new Error(evtData.detail || "추천 계산 스트림 오류");
    }
  }

  function consumeBuffer() {
    var normalized = buffer.replace(/\r/g, "");
    var split = normalized.split("\n\n");
    if (split.length <= 1) {
      buffer = normalized;
      return;
    }
    buffer = split.pop() || "";

    for (var i = 0; i < split.length; i++) {
      var block = split[i].trim();
      if (!block) continue;
      var lines = block.split("\n");
      var evtName = "message";
      var dataLines = [];
      for (var j = 0; j < lines.length; j++) {
        var line = lines[j];
        if (line.indexOf("event:") === 0) {
          evtName = line.slice(6).trim();
        } else if (line.indexOf("data:") === 0) {
          dataLines.push(line.slice(5).trim());
        }
      }
      if (!dataLines.length) continue;
      var payloadText = dataLines.join("\n");
      var payload = {};
      try {
        payload = JSON.parse(payloadText);
      } catch (_) {
        payload = {};
      }
      dispatchEvent(evtName, payload);
    }
  }

  while (true) {
    var chunk = await reader.read();
    if (chunk.done) {
      break;
    }
    buffer += decoder.decode(chunk.value, { stream: true });
    consumeBuffer();
  }
  if (buffer.trim()) {
    buffer += "\n\n";
    consumeBuffer();
  }

  if (!state.recommendation) {
    throw new Error("추천 결과를 수신하지 못했습니다.");
  }
  if (
    state.candidates.length > 0 &&
    (
      !state.recommendation.candidate_evaluations ||
      state.recommendation.candidate_evaluations.length === 0
    )
  ) {
    state.recommendation.candidate_evaluations = state.candidates.slice();
  }
  return state;
}

/* ── Rendering ────────────────────────────────────── */

function renderArrivalResult(data) {
  hideCandidateTooltip(true);
  setProviderName(data.provider);
  var durationText = formatRoundedDuration(data.duration_seconds);
  var providerNoticeHtml = buildProviderNoticeCard(data.provider);
  var html =
    providerNoticeHtml +
    '<div class="result-card">' +
    '  <div class="result-title">&#128663; 예상 도착 시각</div>' +
    '  <div class="result-rows">' +
    '    <div class="result-row">' +
    '      <span class="label">출발</span>' +
    '      <span class="value">' + formatRoundedDatetime(data.departure_time) + "</span>" +
    "    </div>" +
    '    <div class="result-row">' +
    '      <span class="label">도착</span>' +
    '      <span class="value">' + formatRoundedDatetime(data.arrival_time) + "</span>" +
    "    </div>" +
    "  </div>" +
    '  <div class="result-duration">' +
    '    <div class="duration-big">' + durationText + "</div>" +
    '    <div class="duration-label">예상 소요시간</div>' +
    "  </div>" +
    '  <div class="result-meta">' +
    '    <span class="result-badge ' + (data.cache_hit ? "badge-success" : "badge-warn") + '">' +
         (data.cache_hit ? "&#9889; 캐시 히트" : "&#128268; 신규 조회") + "</span>" +
    "  </div>" +
    "</div>";
  $results.innerHTML = html;
  $results.classList.remove("hidden");
}

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
  var safeDur = formatRoundedDuration(immediateSafe.safe_duration_seconds || 0);
  var clampMsg = immediateSafe.clamped_to_now
    ? '<span class="result-badge badge-warn">&#9888; 현재 시각 하한 적용</span>'
    : '<span class="result-badge badge-success">&#10003; 즉시 산출</span>';
  return (
    '<div class="result-card quick-safe-card">' +
    '  <div class="result-title">&#127793; 즉시 여유 출발 제안</div>' +
    '  <div class="result-rows">' +
    '    <div class="result-row">' +
    '      <span class="label">여유 출발</span>' +
    '      <span class="value">' + formatRoundedDatetime(immediateSafe.safe_departure_time) + "</span>" +
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
  var tightRoundedSeconds = latestDurationSeconds != null
    ? ceilDurationSecondsToMinuteStep(
      latestDurationSeconds,
      DATETIME_MINUTE_STEP
    )
    : null;
  var safeRoundedSeconds = safeDurationSeconds != null
    ? ceilDurationSecondsToMinuteStep(
      safeDurationSeconds,
      DATETIME_MINUTE_STEP
    )
    : null;
  var tightDur = tightRoundedSeconds != null
    ? formatDuration(tightRoundedSeconds)
    : "-";
  var safeDur = safeRoundedSeconds != null
    ? formatDuration(safeRoundedSeconds)
    : "-";
  var bufferSec = (
    tightRoundedSeconds != null &&
    safeRoundedSeconds != null
  )
    ? (safeRoundedSeconds - tightRoundedSeconds)
    : 0;
  var bufferMin = Math.ceil(bufferSec / 60);
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
            formatRoundedDatetime(latestDepartureTime) +
    "</div>" +
    '      <div class="deadline-sub">' +
    '        <span>도착 ' +
            formatRoundedDatetime(latestArrivalTime) +
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
            formatRoundedDatetime(safeDepartureTime) +
    "</div>" +
    '      <div class="deadline-sub">' +
    '        <span>도착 ' +
            formatRoundedDatetime(safeArrivalTime) +
    "</span>" +
    '        <span class="deadline-dur">' + safeDur + "</span>" +
    "      </div>" +
    "    </div>" +
    "  </div>" +
    '  <div class="calc-explain analysis-tooltip-panel">' +
    '    <div class="calc-explain-title">&#128209; 보정 산출 근거</div>' +
         candidateRow +
    '    <div class="calc-explain-row"><span class="label">' +
          ctx.calcBaseDurationLabel +
          '</span><span class="val">' + tightDur + "</span></div>" +
    '    <div class="calc-explain-row"><span class="label">안정적 보정 계수</span><span class="val">&times;1.25 (25% 여유)</span></div>' +
    '    <div class="calc-explain-row"><span class="label">보정 적용 소요시간</span><span class="val">' + safeDur + "</span></div>" +
    '    <div class="calc-explain-row"><span class="label">추가된 여유 시간</span><span class="val">+' +
          bufferMin +
          '분</span></div>' +
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
  var durationText = formatRoundedDuration(data.duration_seconds);
  var cacheBadge = data.cache_hit
    ? "&#9889; 캐시 히트"
    : "&#128268; 신규 조회";

  return (
    '<div class="result-card baseline-card">' +
    '  <div class="result-title">&#128345; ' + ctx.baselineTitle + "</div>" +
    '  <div class="result-rows">' +
    '    <div class="result-row">' +
    '      <span class="label">출발</span>' +
    '      <span class="value">' + formatRoundedDatetime(data.departure_time) + "</span>" +
    "    </div>" +
    '    <div class="result-row">' +
    '      <span class="label">도착</span>' +
    '      <span class="value">' + formatRoundedDatetime(data.arrival_time) + "</span>" +
    "    </div>" +
    "  </div>" +
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
        { hideCandidateSummary: true }
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

function renderDepartureResult(data, baselineArrivalData, context, immediateSafe) {
  hideCandidateTooltip(true);
  var ctx = context || _renderContext("departure");
  setProviderName(data.provider);
  var html = buildProviderNoticeCard(data.provider);
  if (baselineArrivalData && ctx.mode !== "arrival") {
    html += buildBaselineArrivalCard(baselineArrivalData, ctx);
  }
  if (immediateSafe && ctx.mode !== "arrival") {
    html += buildImmediateSafeCard(immediateSafe);
  }

  var safeDepartureFallback = immediateSafe
    ? immediateSafe.safe_departure_time
    : null;
  var safeDurationFallback = immediateSafe
    ? immediateSafe.safe_duration_seconds
    : null;
  var latestDepartureTime = data.latest_departure_time || null;
  var latestArrivalTime = data.latest_departure_arrival_time || null;
  var latestDurationSeconds = data.latest_departure_duration_seconds != null
    ? data.latest_departure_duration_seconds
    : null;

  if (ctx.mode === "arrival" && baselineArrivalData) {
    if (!latestDepartureTime) {
      latestDepartureTime = baselineArrivalData.departure_time;
    }
    if (!latestArrivalTime) {
      latestArrivalTime = baselineArrivalData.arrival_time;
    }
    if (latestDurationSeconds == null) {
      latestDurationSeconds = baselineArrivalData.duration_seconds;
    }
  }

  var safeDepartureTime = data.safe_departure_time || safeDepartureFallback;
  var safeDurationSeconds = data.safe_departure_duration_seconds != null
    ? data.safe_departure_duration_seconds
    : safeDurationFallback;
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

  var durationText = formatRoundedDuration(recommendedDurationDisplaySeconds);
  var tightDurationText = formatRoundedDuration(latestDurationSeconds);
  var safeDurationText = formatRoundedDuration(safeDurationSeconds);
  var displayMeetsDeadline = !!data.meets_deadline;
  if (ctx.mode === "departure") {
    var displayArrival = new Date(expectedArrivalDisplayTime);
    var desiredArrival = new Date(data.desired_arrival_time);
    if (
      !isNaN(displayArrival.getTime()) &&
      !isNaN(desiredArrival.getTime())
    ) {
      displayMeetsDeadline = displayArrival.getTime() <= desiredArrival.getTime();
    }
  }
  var deadlineBadge = displayMeetsDeadline
    ? ctx.statusPass
    : ctx.statusFail;
  var arrivalDeltaText = formatArrivalDelta(
    expectedArrivalDisplayTime,
    data.desired_arrival_time
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
    html += buildAnalysisCard(analysisData, ctx);
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
  var baselineDepartureText = formatRoundedDatetime(latestDepartureTime);
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
    desiredArrivalIso: data.desired_arrival_time,
    durationSeconds: recommendedDurationDisplaySeconds,
  });
  var tightCalendarEvent = buildCalendarEventPayload({
    label: "타이트 출발",
    route: data.route,
    departureIso: latestDepartureTime,
    arrivalIso: tightArrivalForCalendar,
    desiredArrivalIso: data.desired_arrival_time,
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

  // ── 추천 출발 시각 카드 (아래 배치) ──
  html +=
    '<div class="result-card recommendation-card">' +
    '  <div class="result-title">&#128663; 추천 출발 시각</div>' +
    '  <div class="result-rows">' +
    '    <div class="result-row row-recommended-departure">' +
    '      <span class="label">추천 출발 시간</span>' +
    '      <span class="value">' + formatRoundedDatetime(recommendedDepartureDisplayTime) + "</span>" +
    "    </div>" +
    '    <div class="result-row row-baseline-departure">' +
    '      <span class="label">' + baselineDepartureLabel + "</span>" +
    '      <span class="value">' + baselineDepartureText + "</span>" +
    "    </div>" +
    '    <div class="result-row">' +
    '      <span class="label">추천 출발시 예상 도착 시간</span>' +
    '      <span class="value">' + formatRoundedDatetime(expectedArrivalDisplayTime) + "</span>" +
    "    </div>" +
    '    <div class="result-row">' +
    '      <span class="label">' + ctx.desiredLabel + "</span>" +
    '      <span class="value">' + formatRoundedDatetime(data.desired_arrival_time) + "</span>" +
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

  $results.innerHTML = html;
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
      refreshMapMarkersLive();
    });
  });
}

/* ── Favorites UI ────────────────────────────────── */

function renderFavorites() {
  var $section = document.getElementById("favorites-section");
  var $list = document.getElementById("favorites-list");
  var items = getFavorites();

  if (items.length === 0 && !$section.classList.contains("manage-mode")) {
    $section.classList.add("hidden");
    return;
  }
  $section.classList.remove("hidden");

  var html = "";
  for (var i = 0; i < items.length; i++) {
    var f = items[i];
    var safeAddress = escapeHtml(f.address || "");
    var safeName = escapeHtml(f.name || "");
    html +=
      '<div class="fav-chip" data-idx="' + i + '" title="' + safeAddress + '">' +
      '  <span class="fav-chip-name">' + safeName + '</span>' +
      '  <span class="fav-chip-del" data-delidx="' + i + '">&times;</span>' +
      '</div>';
  }
  $list.innerHTML = html;

  // 칩 클릭: 활성 입력 필드에 주소 자동 입력
  $list.querySelectorAll(".fav-chip").forEach(function (el) {
    el.addEventListener("click", function (e) {
      if (e.target.classList.contains("fav-chip-del")) return;
      var idx = parseInt(el.getAttribute("data-idx"), 10);
      var f = items[idx];
      // 현재 포커스된 필드 또는 비어있는 필드에 입력
      var target = document.activeElement;
      var coords = buildStableSelection(
        {
          lat: parseFloat(f.lat),
          lon: parseFloat(f.lon),
          display_name: f.name,
          address: f.address,
          selection_kind: "poi",
          canonical_query: f.address,
          source: "favorite",
        },
        {
          display_name: f.name,
          address: f.address,
          selection_kind: "poi",
          canonical_query: f.address,
          source: "favorite",
        }
      );
      if (target === $origin || target === $destination) {
        target.value = f.address;
        if (target === $origin) _selectedOrigin = coords;
        else _selectedDest = coords;
      } else if (!$origin.value) {
        $origin.value = f.address;
        _selectedOrigin = coords;
      } else if (!$destination.value) {
        $destination.value = f.address;
        _selectedDest = coords;
      } else {
        $origin.value = f.address;
        _selectedOrigin = coords;
      }
      refreshMapMarkersLive();
    });
  });

  // 삭제 버튼
  $list.querySelectorAll(".fav-chip-del").forEach(function (el) {
    el.addEventListener("click", function (e) {
      e.stopPropagation();
      var idx = parseInt(el.getAttribute("data-delidx"), 10);
      removeFavorite(idx);
    });
  });
}

/* ── Events ───────────────────────────────────────── */

function switchMode(mode) {
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
  refreshMapMarkersLive();
});

/* Live marker update on input change */
function refreshMapMarkersLive() {
  clearMarkers();
  if (!_map) return;
  var orig = $origin.value.trim();
  var dest = $destination.value.trim();
  var originSelection = _selectedOrigin;
  var destSelection = _selectedDest;
  var isLiveMarkerRefreshCurrent = function () {
    return (
      $origin.value.trim() === orig &&
      $destination.value.trim() === dest &&
      _selectedOrigin === originSelection &&
      _selectedDest === destSelection
    );
  };

  var pO = orig
    ? resolveRouteCriticalCoords(orig, originSelection)
    : Promise.resolve(null);
  var pD = dest
    ? resolveRouteCriticalCoords(dest, destSelection)
    : Promise.resolve(null);

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
    clearMarkers();
    if (originCoords) {
      addMarker([originCoords.lat, originCoords.lon], { color: "#03C75A", label: "출발" });
    }
    if (destCoords) {
      addMarker([destCoords.lat, destCoords.lon], { color: "#E53935", label: "도착" });
    }
    fitBounds();
    if (originCoords && destCoords) {
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

$mobileToggle.addEventListener("click", function () {
  $sidebar.classList.toggle("open");
});

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

  var origin = $origin.value.trim();
  var destination = $destination.value.trim();
  var datetimeVal = $datetimeInput.value;

  if (!origin) { showError("출발지를 입력해 주세요."); $origin.focus(); return; }
  if (!destination) { showError("도착지를 입력해 주세요."); $destination.focus(); return; }
  if (!datetimeVal) { showError("시각을 선택해 주세요."); $datetimeInput.focus(); return; }

  // 과거 시간 보정 + 10분 올림
  var datetimeValidation = validateAndCeilDatetime();
  if (datetimeValidation.invalid) {
    showError("시각 형식이 올바르지 않습니다. 예: 2026-02-17T10:00");
    $datetimeInput.focus();
    return;
  }
  if (datetimeValidation.corrected) {
    datetimeVal = datetimeValidation.corrected;
  } else {
    datetimeVal = $datetimeInput.value;
  }

  var searchOriginSelection = _selectedOrigin;
  var searchDestSelection = _selectedDest;
  var searchMode = _currentMode;
  var searchRouteInputRevision = _routeInputRevision;
  var isSearchStillCurrent = function () {
    return (
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

  var isoTime = toApiDatetimeString(datetimeVal);
  if (!isoTime) {
    showError("시각 값을 해석할 수 없습니다. 다시 선택해 주세요.");
    if (_routeInputRevision === searchRouteInputRevision) {
      _searchInProgress = false;
      hideLoading();
    }
    return;
  }

  try {
    // ── Step 1: 출발/도착 좌표를 병렬 resolve ──
    var coordResults = await Promise.all([
      resolveRouteCriticalCoords(origin, searchOriginSelection),
      resolveRouteCriticalCoords(destination, searchDestSelection),
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
        addMarker([oCoords.lat, oCoords.lon], { color: "#03C75A", label: "출발" });
      }
      if (dCoords) {
        addMarker([dCoords.lat, dCoords.lon], { color: "#E53935", label: "도착" });
      }
      fitBounds();
    }
    var routePromise = oCoords && dCoords
      ? fetchAndDrawRoute(
          oCoords.lat,
          oCoords.lon,
          dCoords.lat,
          dCoords.lon,
          isSearchStillCurrent
        )
      : Promise.resolve();

    if (_currentMode === "arrival") {
      var arrivalContext = _renderContext("arrival");
      var baselineData = null;
      var immediateSafe = null;
      var finalRendered = false;
      var finalRenderScheduled = false;
      var pendingRenderedAt = 0;
      var minPendingVisibleMs = 600;
      var progressState = {
        checked: 0,
        planned: 0,
        remaining: 0,
        total_candidates: 0,
      };
      try {
        var streamResults = await Promise.all([
          apiStreamArrivalWithRecommendation(
            requestOrigin,
            requestDestination,
            isoTime,
            oCoords,
            dCoords,
            searchOriginSelection,
            searchDestSelection,
            {
              onArrival: function (arrivalPayload, safePayload, progressPayload) {
                if (!isSearchStillCurrent()) return;
                baselineData = withDisplayRoute(
                  arrivalPayload,
                  displayOrigin,
                  displayDestination
                );
                immediateSafe = safePayload;
                if (progressPayload) {
                  progressState = progressPayload;
                }
                renderDeparturePending(
                  baselineData,
                  arrivalContext,
                  immediateSafe,
                  progressState
                );
                pendingRenderedAt = Date.now();
                hideLoading();
              },
              onPlan: function (progressPayload) {
                if (!isSearchStillCurrent()) return;
                progressState = progressPayload || progressState;
                if (!baselineData) {
                  return;
                }
                renderDeparturePending(
                  baselineData,
                  arrivalContext,
                  immediateSafe,
                  progressState
                );
              },
              onCandidate: function (_candidate, allCandidates, progressPayload) {
                if (!isSearchStillCurrent()) return;
                if (!baselineData) return;
                if (progressPayload) {
                  progressState = progressPayload;
                } else {
                  progressState.checked = allCandidates.length;
                  progressState.remaining = Math.max(
                    0,
                    (progressState.planned || 0) - progressState.checked
                  );
                }
                renderDeparturePending(
                  baselineData,
                  arrivalContext,
                  immediateSafe,
                  progressState
                );
              },
              onRecommendation: function (recommendPayload, candidates, progressPayload) {
                if (!isSearchStillCurrent()) return;
                recommendPayload = withDisplayRoute(
                  recommendPayload,
                  displayOrigin,
                  displayDestination
                );
                if (
                  candidates &&
                  candidates.length > 0 &&
                  (
                    !recommendPayload.candidate_evaluations ||
                    recommendPayload.candidate_evaluations.length === 0
                  )
                ) {
                  recommendPayload.candidate_evaluations = candidates.slice();
                }
                if (progressPayload) {
                  progressState = progressPayload;
                }
                var renderFinalRecommendation = function () {
                  if (!isSearchStillCurrent()) {
                    finalRenderScheduled = false;
                    return;
                  }
                  renderDepartureResult(
                    recommendPayload,
                    baselineData,
                    arrivalContext,
                    immediateSafe
                  );
                  finalRendered = true;
                  finalRenderScheduled = false;
                };
                if (pendingRenderedAt > 0) {
                  var elapsedSincePending = Date.now() - pendingRenderedAt;
                  var delayMs = Math.max(
                    0,
                    minPendingVisibleMs - elapsedSincePending
                  );
                  finalRenderScheduled = true;
                  setTimeout(function () {
                    if (finalRendered) {
                      finalRenderScheduled = false;
                      return;
                    }
                    if (!isSearchStillCurrent()) {
                      finalRenderScheduled = false;
                      return;
                    }
                    renderFinalRecommendation();
                  }, delayMs);
                  return;
                }
                renderFinalRecommendation();
              },
            }
          ),
          routePromise,
        ]);

        if (!isSearchStillCurrent()) return;
        if (
          !finalRendered &&
          !finalRenderScheduled &&
          streamResults[0].recommendation
        ) {
          renderDepartureResult(
            withDisplayRoute(
              streamResults[0].recommendation,
              displayOrigin,
              displayDestination
            ),
            withDisplayRoute(
              streamResults[0].arrival || baselineData,
              displayOrigin,
              displayDestination
            ),
            arrivalContext,
            streamResults[0].immediate_safe_departure || immediateSafe
          );
        }
      } catch (arrivalRecommendErr) {
        if (!isSearchStillCurrent()) return;
        console.warn("arrival mode recommendation failed:", arrivalRecommendErr);
        if (baselineData) {
          renderArrivalResult(baselineData);
        } else {
          throw arrivalRecommendErr;
        }
      }
    } else {
      var departureContext = _renderContext("departure");
      var progressState = {
        checked: 0,
        planned: 0,
        remaining: 0,
        total_candidates: 0,
      };
      var baselineDepartureLocal = nowCeilToStep();
      var baselineDepartureTime = toApiDatetimeString(baselineDepartureLocal) || isoTime;
      var baselinePromise = apiEstimateArrival(
        requestOrigin,
        requestDestination,
        baselineDepartureTime,
        oCoords,
        dCoords,
        searchOriginSelection,
        searchDestSelection
      );

      var baselineData = null;
      var recommendationData = null;
      var baselineResolved = false;
      var streamErr = null;
      var recommendStreamPromise = apiStreamDepartureRecommendation(
        requestOrigin,
        requestDestination,
        isoTime,
        oCoords,
        dCoords,
        searchOriginSelection,
        searchDestSelection,
        {
          onPlan: function (progressPayload) {
            if (!isSearchStillCurrent()) return;
            progressState = progressPayload || progressState;
            if (!baselineResolved) {
              return;
            }
            renderDeparturePending(
              baselineData,
              departureContext,
              null,
              progressState
            );
          },
          onCandidate: function (_candidate, allCandidates, progressPayload) {
            if (!isSearchStillCurrent()) return;
            if (progressPayload) {
              progressState = progressPayload;
            } else {
              progressState.checked = allCandidates.length;
              progressState.remaining = Math.max(
                0,
                (progressState.planned || 0) - progressState.checked
              );
            }
            if (!baselineResolved) {
              return;
            }
            renderDeparturePending(
              baselineData,
              departureContext,
              null,
              progressState
            );
          },
          onRecommendation: function (recommendPayload, candidates, progressPayload) {
            if (!isSearchStillCurrent()) return;
            recommendPayload = withDisplayRoute(
              recommendPayload,
              displayOrigin,
              displayDestination
            );
            if (
              candidates &&
              candidates.length > 0 &&
              (
                !recommendPayload.candidate_evaluations ||
                recommendPayload.candidate_evaluations.length === 0
              )
            ) {
              recommendPayload.candidate_evaluations = candidates.slice();
            }
            if (progressPayload) {
              progressState = progressPayload;
            }
            recommendationData = recommendPayload;
          },
        }
      ).catch(function (err) {
        streamErr = err;
        console.warn("departure mode recommendation stream failed:", err);
        return null;
      });

      try {
        baselineData = await baselinePromise;
        if (!isSearchStillCurrent()) return;
        baselineData = withDisplayRoute(
          baselineData,
          displayOrigin,
          displayDestination
        );
        baselineResolved = true;
        renderDeparturePending(
          baselineData,
          departureContext,
          null,
          progressState
        );
      } catch (baselineErr) {
        if (!isSearchStillCurrent()) return;
        console.warn("baseline arrival failed:", baselineErr);
        baselineResolved = true;
        renderDeparturePending(
          null,
          departureContext,
          null,
          progressState
        );
      }

      hideLoading();

      var departureResults = await Promise.all([recommendStreamPromise, routePromise]);
      if (!isSearchStillCurrent()) return;
      if (
        !recommendationData &&
        departureResults[0] &&
        departureResults[0].recommendation
      ) {
        recommendationData = withDisplayRoute(
          departureResults[0].recommendation,
          displayOrigin,
          displayDestination
        );
      }
      if (!recommendationData) {
        if (streamErr) {
          console.warn("falling back to single departure recommendation API");
        }
        recommendationData = await apiRecommendDeparture(
          requestOrigin,
          requestDestination,
          isoTime,
          oCoords,
          dCoords,
          searchOriginSelection,
          searchDestSelection
        );
        if (!isSearchStillCurrent()) return;
        recommendationData = withDisplayRoute(
          recommendationData,
          displayOrigin,
          displayDestination
        );
      }
      renderDepartureResult(
        recommendationData,
        baselineData,
        departureContext,
        null
      );
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

    // On mobile, close sidebar after search
    $sidebar.classList.remove("open");
  } catch (err) {
    if (!isSearchStillCurrent()) return;
    showError(err.message || "알 수 없는 오류가 발생했습니다.");
  } finally {
    if (_routeInputRevision === searchRouteInputRevision) {
      _searchInProgress = false;
      hideLoading();
    }
  }
}

$searchBtn.addEventListener("click", handleSearch);

// 시간 변경 시 입력값만 동기화 (강제 보정은 검색 시점 수행)
$datetimeInput.addEventListener("change", function () {
  if (syncDatetimeStateFromInputValue()) {
    hideDatetimeInputTooltip();
    hideError();
  }
});

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

/* ── Favorites Event Setup ───────────────────────── */
(function () {
  var $toggle = document.getElementById("favorites-toggle");
  var $section = document.getElementById("favorites-section");
  var $addForm = document.getElementById("favorites-add-form");
  var $favName = document.getElementById("fav-name");
  var $favAddr = document.getElementById("fav-address");
  var $favAddBtn = document.getElementById("fav-add-btn");

  $toggle.addEventListener("click", function () {
    var managing = $section.classList.toggle("manage-mode");
    $addForm.classList.toggle("hidden", !managing);
    $toggle.textContent = managing ? "완료" : "관리";
    if (managing) $section.classList.remove("hidden");
  });

  $favAddBtn.addEventListener("click", function () {
    var name = $favName.value.trim();
    var addr = $favAddr.value.trim();
    if (!name || !addr) return;
    // 주소를 지오코딩해서 좌표 저장
    fetch("/api/geocode?q=" + encodeURIComponent(addr) + buildMapCenterQueryString())
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (data && data.length > 0) {
          addFavorite(name, addr, data[0].lat, data[0].lon);
          $favName.value = "";
          $favAddr.value = "";
        } else {
          alert("주소를 찾을 수 없습니다. 다시 입력해 주세요.");
        }
      });
  });
})();

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
    _config = await fetchConfig();
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

  $datetimeInput.value = nowCeilToStep();
  initDatetimePicker();
  enforceMinDatetime();
  // 매 30초마다 min 속성 갱신 (시간 경과에 따른 과거 시간 차단)
  setInterval(enforceMinDatetime, 30000);

  initMap();
  queueAutocompleteWarmup();
  renderRecentSearches();
  renderFavorites();

  // 즐겨찾기가 있으면 섹션 표시
  if (getFavorites().length > 0) {
    document.getElementById("favorites-section").classList.remove("hidden");
  }
}

bootstrap();
