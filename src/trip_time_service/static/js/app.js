"use strict";

/* ── State ────────────────────────────────────────── */
let _map = null;
let _markers = [];
let _routeGroup = null;
let _config = {
  naver_map_client_id: null,
  timezone: "Asia/Seoul",
  provider: "unknown",
};
let _currentMode = "arrival"; // "arrival" | "departure"

// 자동완성에서 선택된 좌표 저장
let _selectedOrigin = null;   // {lat, lon, display_name}
let _selectedDest = null;     // {lat, lon, display_name}

// 자동완성 상태
let _acTimerOrigin = null;
let _acTimerDest = null;
let _acActiveIdx = { origin: -1, dest: -1 };
let _searchInProgress = false; // 중복 검색 방지 플래그

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

const DATETIME_MINUTE_STEP = 10;
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
  var h = Math.floor(seconds / 3600);
  var m = Math.floor((seconds % 3600) / 60);
  if (h > 0 && m > 0) return h + "시간 " + m + "분";
  if (h > 0) return h + "시간";
  return m + "분";
}

function formatDatetime(isoStr) {
  var d = new Date(isoStr);
  var month = String(d.getMonth() + 1).padStart(2, "0");
  var day = String(d.getDate()).padStart(2, "0");
  var hours = String(d.getHours()).padStart(2, "0");
  var mins = String(d.getMinutes()).padStart(2, "0");
  return d.getFullYear() + "-" + month + "-" + day + " " + hours + ":" + mins;
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

function setProviderName(provider) {
  if (!$providerBadge && !$providerWarning) {
    return;
  }
  var providerName = (provider || _config.provider || "unknown").toLowerCase();
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

/**
 * 현재 시각을 10분 단위로 올림(ceil).
 * 예: 18:31 → 18:40, 18:40 → 18:40, 18:41 → 18:50
 * 네이버 지도는 과거 시간 조회 불가이므로 항상 미래 시간을 반환.
 */
function nowCeilTo10() {
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
    _datetimeMinIso = nowCeilTo10();
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
    parseLocalIsoMinutes(nowCeilTo10()) ||
    new Date();
  var parsed = parseFlexibleDatetimeInput($datetimeInput.value, fallbackDate);
  if (!parsed) {
    parsed = parseLocalIsoMinutes(nowCeilTo10());
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
    parseLocalIsoMinutes(nowCeilTo10()) ||
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
    parseLocalIsoMinutes(nowCeilTo10()) ||
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
  _datetimeMinIso = nowCeilTo10();
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
    parseLocalIsoMinutes(nowCeilTo10()) ||
    new Date();
  var selected = parseFlexibleDatetimeInput(val, fallbackDate);
  if (!selected) {
    return { corrected: null, invalid: true };
  }

  var now = new Date();
  var correctedDate = null;
  if (selected <= now) {
    correctedDate = parseLocalIsoMinutes(nowCeilTo10());
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
function resolveCoords(address, selectedCoords) {
  if (selectedCoords) return Promise.resolve(selectedCoords);
  if (_geocodeCache[address]) return Promise.resolve(_geocodeCache[address]);
  return fetch("/api/geocode?q=" + encodeURIComponent(address))
    .then(function (res) { return res.json(); })
    .then(function (data) {
      if (!data || data.length === 0) return null;
      var coords = { lat: parseFloat(data[0].lat), lon: parseFloat(data[0].lon) };
      _geocodeCache[address] = coords;
      return coords;
    })
    .catch(function () { return null; });
}

function geocodeAndMark(address, color) {
  if (!_map) return Promise.resolve(null);
  return resolveCoords(address, null)
    .then(function (coords) {
      if (!coords) {
        console.warn("geocode: no results for", address);
        showError('"' + address + '" 위치를 찾을 수 없습니다. 도로명주소 또는 역/지하철명으로 입력해 주세요.');
        return null;
      }
      hideError();
      var label = color === "#03C75A" ? "출발" : "도착";
      addMarker([coords.lat, coords.lon], { color: color, label: label });
      return coords;
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

function fetchAndDrawRoute(lat1, lon1, lat2, lon2) {
  if (!_map) return Promise.resolve();
  var url = "/api/route?olat=" + lat1 + "&olon=" + lon1 +
            "&dlat=" + lat2 + "&dlon=" + lon2;
  return fetch(url)
    .then(function (res) { return res.json(); })
    .then(function (data) {
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

  var pOrigin, pDest;

  // 자동완성 선택 좌표가 있으면 직접 마커 배치
  if (_selectedOrigin) {
    addMarker([_selectedOrigin.lat, _selectedOrigin.lon], { color: "#03C75A", label: "출발" });
    pOrigin = Promise.resolve({ lat: _selectedOrigin.lat, lon: _selectedOrigin.lon });
  } else {
    pOrigin = originText ? geocodeAndMark(originText, "#03C75A") : Promise.resolve(null);
  }

  if (_selectedDest) {
    addMarker([_selectedDest.lat, _selectedDest.lon], { color: "#E53935", label: "도착" });
    pDest = Promise.resolve({ lat: _selectedDest.lat, lon: _selectedDest.lon });
  } else {
    pDest = destText ? geocodeAndMark(destText, "#E53935") : Promise.resolve(null);
  }

  return Promise.all([pOrigin, pDest]).then(function (results) {
    fitBounds();
    if (results[0] && results[1]) {
      return fetchAndDrawRoute(
        results[0].lat, results[0].lon,
        results[1].lat, results[1].lon
      );
    }
  });
}

/* ── Autocomplete ────────────────────────────────── */

function fetchAutocomplete(query, abortState) {
  if (abortState.ctrl) abortState.ctrl.abort();
  abortState.ctrl = new AbortController();
  return fetch("/api/autocomplete?q=" + encodeURIComponent(query), {
    signal: abortState.ctrl.signal,
  })
    .then(function (res) { return res.json(); })
    .catch(function () { return []; });
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
    var typeBadge = it.type ? '<span class="ac-item-type">' + it.type + '</span>' : '';
    html +=
      '<div class="ac-item" data-idx="' + i + '">' +
      '  <div class="ac-item-name">' + (it.display_name || it.address || "") + typeBadge + '</div>' +
      '  <div class="ac-item-addr">' + (it.address || "") + '</div>' +
      '</div>';
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

function setupAutocomplete($input, $dropdown, setSelected, color) {
  var currentItems = [];
  var activeIdx = -1;
  var abortState = { ctrl: null };

  $input.addEventListener("input", function () {
    setSelected(null);
    var q = $input.value.trim();
    if (q.length < 1) {
      closeACDropdown($dropdown);
      currentItems = [];
      activeIdx = -1;
      return;
    }
    fetchAutocomplete(q, abortState).then(function (items) {
      if (!items || !items.length) { closeACDropdown($dropdown); return; }
      currentItems = items;
      activeIdx = -1;
      renderACDropdown($dropdown, items, function (item) {
        $input.value = item.display_name || item.address;
        setSelected({
          lat: parseFloat(item.lat),
          lon: parseFloat(item.lon),
          display_name: item.display_name,
        });
        closeACDropdown($dropdown);
        // 선택 즉시 마커 업데이트
        refreshMapMarkersLive();
      });
    });
  });

  $input.addEventListener("keydown", function (e) {
    var items = $dropdown.querySelectorAll(".ac-item");
    if (!items.length) return;

    if (e.key === "ArrowDown") {
      e.preventDefault();
      activeIdx = Math.min(activeIdx + 1, items.length - 1);
      items.forEach(function (el, i) {
        el.classList.toggle("ac-active", i === activeIdx);
      });
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      activeIdx = Math.max(activeIdx - 1, 0);
      items.forEach(function (el, i) {
        el.classList.toggle("ac-active", i === activeIdx);
      });
    } else if (e.key === "Enter" && activeIdx >= 0 && currentItems[activeIdx]) {
      e.preventDefault();
      e.stopImmediatePropagation(); // 일반 Enter 핸들러의 handleSearch() 중복 호출 방지
      var sel = currentItems[activeIdx];
      $input.value = sel.display_name || sel.address;
      setSelected({
        lat: parseFloat(sel.lat),
        lon: parseFloat(sel.lon),
        display_name: sel.display_name,
      });
      closeACDropdown($dropdown);
      refreshMapMarkersLive();
    } else if (e.key === "Escape") {
      closeACDropdown($dropdown);
      activeIdx = -1;
    }
  });

  $input.addEventListener("blur", function () {
    setTimeout(function () { closeACDropdown($dropdown); }, 150);
  });
}

/* ── API ──────────────────────────────────────────── */

async function fetchConfig() {
  var res = await fetch("/api/config");
  if (!res.ok) throw new Error("Config fetch failed");
  return res.json();
}

async function apiEstimateArrival(origin, destination, departureTime, oCoords, dCoords) {
  var body = {
    origin: origin,
    destination: destination,
    departure_time: departureTime,
  };
  if (oCoords) body.origin_coords = { lat: oCoords.lat, lon: oCoords.lon };
  if (dCoords) body.dest_coords = { lat: dCoords.lat, lon: dCoords.lon };

  var res = await fetch("/v1/trip/arrival-time", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    var err = await res.json().catch(function () { return {}; });
    throw new Error(err.detail || "요청 처리 중 오류가 발생했습니다 (" + res.status + ")");
  }
  return res.json();
}

async function apiRecommendDeparture(origin, destination, desiredArrivalTime, oCoords, dCoords) {
  var body = {
    origin: origin,
    destination: destination,
    desired_arrival_time: desiredArrivalTime,
  };
  if (oCoords) body.origin_coords = { lat: oCoords.lat, lon: oCoords.lon };
  if (dCoords) body.dest_coords = { lat: dCoords.lat, lon: dCoords.lon };

  var res = await fetch("/v1/trip/recommended-departure-time", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    var err = await res.json().catch(function () { return {}; });
    throw new Error(err.detail || "요청 처리 중 오류가 발생했습니다 (" + res.status + ")");
  }
  return res.json();
}

async function apiStreamArrivalWithRecommendation(
  origin,
  destination,
  departureTime,
  oCoords,
  dCoords,
  handlers
) {
  var body = {
    origin: origin,
    destination: destination,
    departure_time: departureTime,
  };
  if (oCoords) body.origin_coords = { lat: oCoords.lat, lon: oCoords.lon };
  if (dCoords) body.dest_coords = { lat: dCoords.lat, lon: dCoords.lon };

  var res = await fetch("/v1/trip/arrival-time-with-recommendation/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
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
    if (evtName === "error") {
      var detail = (evtData && evtData.detail) || "추천 계산 스트림 오류";
      throw new Error(detail);
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
  var durationText = formatDuration(data.duration_seconds);
  var html =
    '<div class="result-card">' +
    '  <div class="result-title">&#128663; 예상 도착 시각</div>' +
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
    tightTag: "타이트",
    tightLabel: "기준 출발",
    safeLabel: "여유 도착 (출발 고정 &times;1.25)",
    calcBaseDurationLabel: "기준 소요시간",
    calcFormulaText: "기준출발 + 보정소요 = 여유 도착시각",
    statusPass: '<span class="result-badge badge-success">&#10003; 지정 출발 소요 시간보다 빠름</span>',
    statusFail: '<span class="result-badge badge-danger">&#9888; 지정 출발 소요 시간보다 느림</span>',
  },
  departure: {
    mode: "departure",
    baselineTitle: "단일 조회(현재 출발 기준)",
    baselineDurationLabel: "현재 출발 기준 소요시간",
    pendingTitle: "추천 출발 시각 계산 중",
    pendingCopy: "단일 조회 결과를 먼저 보여주고, 추천 결과가 준비되면 자동으로 갱신합니다.",
    baselineHint: "추천 계산 중에도 먼저 표시",
    desiredLabel: "희망 도착",
    analysisTitle: "마지노선 분석",
    tightTag: "타이트",
    tightLabel: "마지노선 출발",
    safeLabel: "여유 출발 (&times;1.25)",
    calcBaseDurationLabel: "마지노선 기준 소요시간",
    calcFormulaText: "희망도착 - 보정소요 = 안정적 출발시각",
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

function buildCandidateTooltip(candidates) {
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
      ? "지정 출발보다 빠름"
      : "지정 출발보다 느림";
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
  var tightDur = latestDurationSeconds != null
    ? formatDuration(latestDurationSeconds)
    : "-";
  var safeDur = safeDurationSeconds != null
    ? formatDuration(safeDurationSeconds)
    : "-";
  var bufferSec = (
    latestDurationSeconds != null &&
    safeDurationSeconds != null
  )
    ? (safeDurationSeconds - latestDurationSeconds)
    : 0;
  var bufferMin = Math.ceil(bufferSec / 60);
  var candidateCount = data.candidates_checked || 0;
  var candidateRow = opts.hideCandidateSummary
    ? ""
    : (
      '<div class="calc-explain-row"><span class="label">분석 후보 수</span><span class="val">' +
      candidateCount +
      '개 (역방향 탐색)</span></div>'
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
            (latestDepartureTime ? formatDatetime(latestDepartureTime) : "-") +
    "</div>" +
    '      <div class="deadline-sub">' +
    '        <span>도착 ' +
            (latestArrivalTime ? formatDatetime(latestArrivalTime) : "-") +
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
            (safeDepartureTime ? formatDatetime(safeDepartureTime) : "-") +
    "</div>" +
    '      <div class="deadline-sub">' +
    '        <span>도착 ' +
            (safeArrivalTime ? formatDatetime(safeArrivalTime) : "-") +
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

  var html = "";
  if (arrivalData) {
    html += buildBaselineArrivalCard(arrivalData, ctx);
  }
  if (immediateSafe) {
    html += buildImmediateSafeCard(immediateSafe);
  }
  var progressText = prog.checked > 0
    ? "후보 " + prog.checked + "개 분석 완료"
    : "후보를 수집하는 중입니다.";
  html +=
    '<div class="result-card progress-card">' +
    '  <div class="result-title">&#9201; ' + ctx.pendingTitle + "</div>" +
    '  <p class="progress-copy">' + ctx.pendingCopy + "</p>" +
    '  <p class="progress-copy">' + progressText + "</p>" +
    "</div>";

  $results.innerHTML = html;
  $results.classList.remove("hidden");
  setupAnalysisTooltipPanels();
}

function renderDepartureResult(data, baselineArrivalData, context, immediateSafe) {
  hideCandidateTooltip(true);
  var ctx = context || _renderContext("departure");
  setProviderName(data.provider);
  var durationText = formatDuration(data.duration_seconds);
  var deadlineBadge = data.meets_deadline
    ? ctx.statusPass
    : ctx.statusFail;

  var html = "";
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
  var tightDurationText = latestDurationSeconds != null
    ? formatDuration(latestDurationSeconds)
    : "-";
  var safeDurationText = safeDurationSeconds != null
    ? formatDuration(safeDurationSeconds)
    : "-";
  var arrivalDeltaText = formatArrivalDelta(
    data.expected_arrival_time,
    data.desired_arrival_time
  );

  var analysisData = {
    latest_departure_time: latestDepartureTime,
    latest_departure_arrival_time: latestArrivalTime,
    latest_departure_duration_seconds: latestDurationSeconds,
    safe_departure_time: safeDepartureTime,
    safe_departure_duration_seconds: safeDurationSeconds,
    candidates_checked: data.candidates_checked,
  };
  if (analysisData.latest_departure_time || analysisData.safe_departure_time) {
    html += buildAnalysisCard(analysisData, ctx);
  }

  var candidateTooltipHtml = buildCandidateTooltip(data.candidate_evaluations || []);
  var checkedCount = data.candidates_checked || 0;
  var plannedCount = data.planned_queries || checkedCount;
  var remainingCount = Math.max(0, plannedCount - checkedCount);
  var candidateBadgeText =
    "&#128202; 후보 " + checkedCount + "/" + plannedCount + " 분석";
  if (remainingCount > 0) {
    candidateBadgeText += " (남은 " + remainingCount + ")";
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
  var baselineDepartureText = latestDepartureTime
    ? formatDatetime(latestDepartureTime)
    : "-";
  var recommendedScoreBadge =
    '<span class="result-badge badge-success">' +
    "추천 출발 점수 " +
    recommendedScoreText +
    "</span>";
  var baselineScoreBadge = latestDepartureTime
    ? (
      '<span class="result-badge badge-warn">' +
      "지정 출발 점수 " +
      baselineScoreText +
      "</span>"
    )
    : "";

  // ── 추천 출발 시각 카드 (아래 배치) ──
  html +=
    '<div class="result-card recommendation-card">' +
    '  <div class="result-title">&#128663; 추천 출발 시각</div>' +
    '  <div class="result-rows">' +
    '    <div class="result-row row-recommended-departure">' +
    '      <span class="label">추천 출발 시간</span>' +
    '      <span class="value">' + formatDatetime(data.recommended_departure_time) + "</span>" +
    "    </div>" +
    '    <div class="result-row row-baseline-departure">' +
    '      <span class="label">지정 출발 시간</span>' +
    '      <span class="value">' + baselineDepartureText + "</span>" +
    "    </div>" +
    '    <div class="result-row">' +
    '      <span class="label">추천 출발시 예상 도착 시간</span>' +
    '      <span class="value">' + formatDatetime(data.expected_arrival_time) + "</span>" +
    "    </div>" +
    '    <div class="result-row">' +
    '      <span class="label">' + ctx.desiredLabel + "</span>" +
    '      <span class="value">' + formatDatetime(data.desired_arrival_time) + "</span>" +
    "    </div>" +
    '    <div class="result-row">' +
    '      <span class="label">지정 출발 대비 도착 시간 차이</span>' +
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
    html +=
      '<div class="recent-item" data-idx="' + i + '">' +
      '  <span class="recent-item-route">' +
           r.origin +
           ' <span class="recent-item-arrow">&rarr;</span> ' +
           r.destination +
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
      if (r.origin_coords) {
        _selectedOrigin = r.origin_coords;
      }
      if (r.destination_coords) {
        _selectedDest = r.destination_coords;
      }
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
    html +=
      '<div class="fav-chip" data-idx="' + i + '" title="' + f.address + '">' +
      '  <span class="fav-chip-name">' + f.name + '</span>' +
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
      if (target === $origin || target === $destination) {
        target.value = f.address;
        var coords = { lat: parseFloat(f.lat), lon: parseFloat(f.lon), display_name: f.name };
        if (target === $origin) _selectedOrigin = coords;
        else _selectedDest = coords;
      } else if (!$origin.value) {
        $origin.value = f.address;
        _selectedOrigin = { lat: parseFloat(f.lat), lon: parseFloat(f.lon), display_name: f.name };
      } else if (!$destination.value) {
        $destination.value = f.address;
        _selectedDest = { lat: parseFloat(f.lat), lon: parseFloat(f.lon), display_name: f.name };
      } else {
        $origin.value = f.address;
        _selectedOrigin = { lat: parseFloat(f.lat), lon: parseFloat(f.lon), display_name: f.name };
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

  var pO, pD;
  if (_selectedOrigin && orig) {
    addMarker([_selectedOrigin.lat, _selectedOrigin.lon], { color: "#03C75A", label: "출발" });
    pO = Promise.resolve({ lat: _selectedOrigin.lat, lon: _selectedOrigin.lon });
  } else {
    pO = orig ? geocodeAndMark(orig, "#03C75A") : Promise.resolve(null);
  }
  if (_selectedDest && dest) {
    addMarker([_selectedDest.lat, _selectedDest.lon], { color: "#E53935", label: "도착" });
    pD = Promise.resolve({ lat: _selectedDest.lat, lon: _selectedDest.lon });
  } else {
    pD = dest ? geocodeAndMark(dest, "#E53935") : Promise.resolve(null);
  }

  Promise.all([pO, pD]).then(function (results) {
    fitBounds();
    if (results[0] && results[1]) {
      fetchAndDrawRoute(
        results[0].lat, results[0].lon,
        results[1].lat, results[1].lon
      );
    }
  });
}

$origin.addEventListener("change", function () {
  if (!_selectedOrigin) refreshMapMarkersLive();
});
$destination.addEventListener("change", function () {
  if (!_selectedDest) refreshMapMarkersLive();
});

$mobileToggle.addEventListener("click", function () {
  $sidebar.classList.toggle("open");
});

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

  _searchInProgress = true;
  hideCandidateTooltip(true);
  hideError();
  hideDatetimeInputTooltip();
  showLoading();
  $results.classList.add("hidden");

  var isoTime = datetimeVal + ":00+09:00";

  try {
    // ── Step 1: 출발/도착 좌표를 병렬 resolve ──
    var coordResults = await Promise.all([
      resolveCoords(origin, _selectedOrigin),
      resolveCoords(destination, _selectedDest),
    ]);
    var oCoords = coordResults[0];
    var dCoords = coordResults[1];

    if (!oCoords) {
      showError('"' + origin + '" 위치를 찾을 수 없습니다. 도로명주소 또는 역/지하철명으로 입력해 주세요.');
      return;
    }
    if (!dCoords) {
      showError('"' + destination + '" 위치를 찾을 수 없습니다. 도로명주소 또는 역/지하철명으로 입력해 주세요.');
      return;
    }

    // ── Step 2: 좌표 확보됨 → 지도 그리기 시작 ──
    clearMarkers();
    if (_map) {
      addMarker([oCoords.lat, oCoords.lon], { color: "#03C75A", label: "출발" });
      addMarker([dCoords.lat, dCoords.lon], { color: "#E53935", label: "도착" });
      fitBounds();
    }
    var routePromise = fetchAndDrawRoute(oCoords.lat, oCoords.lon, dCoords.lat, dCoords.lon);

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
            origin,
            destination,
            isoTime,
            oCoords,
            dCoords,
            {
              onArrival: function (arrivalPayload, safePayload, progressPayload) {
                baselineData = arrivalPayload;
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

        if (
          !finalRendered &&
          !finalRenderScheduled &&
          streamResults[0].recommendation
        ) {
          renderDepartureResult(
            streamResults[0].recommendation,
            streamResults[0].arrival || baselineData,
            arrivalContext,
            streamResults[0].immediate_safe_departure || immediateSafe
          );
        }
      } catch (arrivalRecommendErr) {
        console.warn("arrival mode recommendation failed:", arrivalRecommendErr);
        if (baselineData) {
          renderArrivalResult(baselineData);
        } else {
          throw arrivalRecommendErr;
        }
      }
    } else {
      var departureContext = _renderContext("departure");
      var recommendPromise = apiRecommendDeparture(
        origin,
        destination,
        isoTime,
        oCoords,
        dCoords
      );
      var baselineDepartureTime = nowCeilTo10() + ":00+09:00";
      var baselinePromise = apiEstimateArrival(
        origin,
        destination,
        baselineDepartureTime,
        oCoords,
        dCoords
      );

      var baselineData = null;
      try {
        baselineData = await baselinePromise;
        renderDeparturePending(
          baselineData,
          departureContext,
          null,
          { checked: 0, planned: 0, remaining: 0, total_candidates: 0 }
        );
      } catch (baselineErr) {
        console.warn("baseline arrival failed:", baselineErr);
        renderDeparturePending(
          null,
          departureContext,
          null,
          { checked: 0, planned: 0, remaining: 0, total_candidates: 0 }
        );
      }

      hideLoading();

      var departureResults = await Promise.all([recommendPromise, routePromise]);
      renderDepartureResult(
        departureResults[0],
        baselineData,
        departureContext,
        null
      );
    }

    // ── Step 3: 최근 검색 저장 ──
    addRecentSearch(origin, destination,
      _selectedOrigin || oCoords, _selectedDest || dCoords);

    // On mobile, close sidebar after search
    $sidebar.classList.remove("open");
  } catch (err) {
    showError(err.message || "알 수 없는 오류가 발생했습니다.");
  } finally {
    _searchInProgress = false;
    hideLoading();
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
    fetch("/api/geocode?q=" + encodeURIComponent(addr))
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
setupAutocomplete($origin, $originAC, function (v) { _selectedOrigin = v; }, "#03C75A");
setupAutocomplete($destination, $destAC, function (v) { _selectedDest = v; }, "#E53935");

/* ── Bootstrap ────────────────────────────────────── */

async function bootstrap() {
  $datetimeInput.value = nowCeilTo10();
  initDatetimePicker();
  enforceMinDatetime();
  // 매 30초마다 min 속성 갱신 (시간 경과에 따른 과거 시간 차단)
  setInterval(enforceMinDatetime, 30000);

  try {
    _config = await fetchConfig();
    setProviderName(_config.provider);
  } catch (_) {
    // config endpoint failed; continue
    setProviderName("unknown");
  }

  initMap();
  renderRecentSearches();
  renderFavorites();

  // 즐겨찾기가 있으면 섹션 표시
  if (getFavorites().length > 0) {
    document.getElementById("favorites-section").classList.remove("hidden");
  }
}

bootstrap();
