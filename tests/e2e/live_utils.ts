import {
  expect,
  type APIRequestContext,
  type Locator,
  type Page,
  type Request as PlaywrightRequest,
} from '@playwright/test';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';

export type AutocompleteCase = {
  query: string;
  expected_any: string[];
  min_results: number;
  require_coords: boolean;
  retry_once_on_empty: boolean;
  category: string;
};

export type RouteCase = {
  origin_query: string;
  destination_query: string;
  mode: 'arrival' | 'departure';
  future_offset_days: number;
  time_hhmm: string;
  selection_kind: string;
};

export const BASE_URL = process.env.E2E_BASE_URL ?? 'http://127.0.0.1:39080';
export const LIVE_ARTIFACTS_DIR =
  process.env.TTS_E2E_ARTIFACTS_DIR ?? resolve(process.cwd(), '.artifacts', 'live');
const E2E_DEBUG_TOKEN = process.env.TTS_E2E_DEBUG_TOKEN ?? '';

export type AutocompleteApiResult = {
  items: Record<string, unknown>[];
  attempts_used: number;
  retried: boolean;
  matched_token: string | null;
};

export type AutocompleteSmokeResult = {
  query: string;
  input_selector: string;
  api_count: number;
  attempts_used: number;
  retried: boolean;
  matched_token: string | null;
  dropdown_texts: string[];
};

export type RouteSmokeResult = {
  mode: RouteCase['mode'];
  stream_path: string;
  origin_selection: SelectedAutocompleteEntry;
  destination_selection: SelectedAutocompleteEntry;
  recommended_departure_time: string;
  expected_arrival_time: string;
  duration_seconds: number;
  candidate_count: number;
  geocode_request_count: number;
  trip_payload_count: number;
};

export type SelectedAutocompleteEntry = {
  query: string;
  clicked_text: string;
  selected_value: string;
  canonical_query: string;
  selection_kind: string;
  coords_ready: boolean;
  lat: number;
  lon: number;
  matched_token: string | null;
  api_count: number;
  attempts_used: number;
  retried: boolean;
  source: string | null;
  confidence: number | string | null;
};

export function saveCapture(page: Page, filePath: string): Promise<void> {
  mkdirSync(dirname(filePath), { recursive: true });
  return page.screenshot({ path: filePath, fullPage: true });
}

export function writeJsonArtifact(filePath: string, payload: unknown): void {
  mkdirSync(dirname(filePath), { recursive: true });
  writeFileSync(filePath, JSON.stringify(payload, null, 2), 'utf-8');
}

export function loadAutocompleteCases(kind: 'blocking' | 'extended'): AutocompleteCase[] {
  return loadJson<AutocompleteCase[]>(`tests/live/data/autocomplete-${kind}.json`);
}

export function loadRouteCases(kind: 'blocking' | 'extended'): RouteCase[] {
  return loadJson<RouteCase[]>(`tests/live/data/routes-${kind}.json`);
}

export async function clearAutocompleteCache(request: APIRequestContext): Promise<void> {
  const response = await request.post(`${BASE_URL}/api/debug/autocomplete/cache-clear`, {
    headers: debugHeaders(),
  });
  expect(response.ok()).toBeTruthy();
}

function debugHeaders(): Record<string, string> {
  return E2E_DEBUG_TOKEN ? { 'X-TTS-Debug-Token': E2E_DEBUG_TOKEN } : {};
}

export async function verifyAutocompleteApi(
  request: APIRequestContext,
  testCase: AutocompleteCase,
  options?: {
    clearCacheOnRetry?: boolean;
    cacheClearFallbackOnFailure?: boolean;
  },
): Promise<AutocompleteApiResult> {
  const maxAttempts = testCase.retry_once_on_empty ? 2 : 1;
  const clearCacheOnRetry = options?.clearCacheOnRetry ?? true;
  const cacheClearFallbackOnFailure =
    options?.cacheClearFallbackOnFailure ?? false;
  let lastItems: Record<string, unknown>[] = [];
  let lastAttemptUsed = 0;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    lastAttemptUsed = attempt + 1;
    const response = await request.get(
      `${BASE_URL}/api/autocomplete?q=${encodeURIComponent(testCase.query)}`,
    );
    expect(response.ok()).toBeTruthy();
    const payload = await response.json();
    const items = Array.isArray(payload) ? payload : [];
    lastItems = items as Record<string, unknown>[];
    assertAutocompleteItemsShape(lastItems, testCase.query);
    const matchedToken = findMatchedTokenInItems(lastItems, testCase.expected_any);
    if (matchesAutocompleteExpectation(lastItems, testCase) && matchedToken) {
      return {
        items: lastItems,
        attempts_used: attempt + 1,
        retried: attempt > 0,
        matched_token: matchedToken,
      };
    }
    if (clearCacheOnRetry && attempt + 1 < maxAttempts) {
      await clearAutocompleteCache(request);
    }
  }
  if (cacheClearFallbackOnFailure && !clearCacheOnRetry) {
    await clearAutocompleteCache(request);
    const response = await request.get(
      `${BASE_URL}/api/autocomplete?q=${encodeURIComponent(testCase.query)}`,
    );
    expect(response.ok()).toBeTruthy();
    const payload = await response.json();
    const items = Array.isArray(payload) ? payload : [];
    lastItems = items as Record<string, unknown>[];
    const matchedToken = findMatchedTokenInItems(lastItems, testCase.expected_any);
    if (matchesAutocompleteExpectation(lastItems, testCase) && matchedToken) {
      return {
        items: lastItems,
        attempts_used: lastAttemptUsed + 1,
        retried: true,
        matched_token: matchedToken,
      };
    }
  }
  throw new Error(
    `autocomplete API did not satisfy case=${testCase.query} items=${JSON.stringify(lastItems)}`,
  );
}

export async function assertAutocompleteInput(
  page: Page,
  request: APIRequestContext,
  inputSelector: string,
  dropdownSelector: string,
  testCase: AutocompleteCase,
): Promise<AutocompleteSmokeResult> {
  const apiResult = await verifyAutocompleteApi(request, testCase, {
    clearCacheOnRetry: false,
    cacheClearFallbackOnFailure: true,
  });
  const dropdownState = await populateAutocompleteDropdown(
    page,
    request,
    inputSelector,
    dropdownSelector,
    testCase.query,
    testCase.expected_any,
    testCase.retry_once_on_empty,
  );
  return {
    query: testCase.query,
    input_selector: inputSelector,
    api_count: apiResult.items.length,
    attempts_used: Math.max(apiResult.attempts_used, dropdownState.attemptsUsed),
    retried: apiResult.retried || dropdownState.attemptsUsed > 1,
    matched_token: dropdownState.matchedToken,
    dropdown_texts: dropdownState.texts,
  };
}

export async function selectAutocompleteEntry(
  page: Page,
  request: APIRequestContext,
  inputSelector: string,
  dropdownSelector: string,
  query: string,
): Promise<SelectedAutocompleteEntry> {
  const expectedAny = buildQueryTokens(query);
  const testCase: AutocompleteCase = {
    query,
    expected_any: expectedAny,
    min_results: 1,
    require_coords: false,
    retry_once_on_empty: true,
    category: 'route',
  };
  const apiResult = await verifyAutocompleteApi(request, testCase, {
    clearCacheOnRetry: false,
    cacheClearFallbackOnFailure: true,
  });
  const dropdownItems = page.locator(`${dropdownSelector} .ac-item`);
  const dropdownState = await populateAutocompleteDropdown(
    page,
    request,
    inputSelector,
    dropdownSelector,
    query,
    expectedAny,
    true,
  );
  const texts = dropdownState.texts;
  const matchedIndex = findMatchingDropdownIndex(texts, expectedAny);
  const preferredIndex = findPreferredDropdownIndex(texts, query, expectedAny);
  const indexToClick =
    preferredIndex >= 0 ? preferredIndex : matchedIndex >= 0 ? matchedIndex : 0;
  const clickedText = (texts[indexToClick] ?? '').trim();
  const matchedToken =
    matchedIndex >= 0 ? findMatchedTokenInTexts([clickedText], expectedAny) : null;
  await dropdownItems.nth(indexToClick).click();
  const input = page.locator(inputSelector);
  await expect(input).not.toHaveValue('', { timeout: 3000 });
  const selectedValue = (await input.inputValue()).trim();
  const selectedItem = findSelectedAutocompleteItem(
    apiResult.items,
    clickedText,
    selectedValue,
    expectedAny,
  );
  expect(
    selectedItem,
    `autocomplete payload missing stable selection candidate for ${query}`,
  ).not.toBeNull();
  const stableItem = selectedItem as Record<string, unknown>;
  const lat = parseFiniteNumber(stableItem.lat);
  const lon = parseFiniteNumber(stableItem.lon);
  expect(lat, `selection lat missing for ${query}`).not.toBeNull();
  expect(lon, `selection lon missing for ${query}`).not.toBeNull();
  expect(stableItem.coords_ready, `selection coords_ready missing for ${query}`).toBe(true);
  const confidenceValue = stableItem.confidence;
  return {
    query,
    clicked_text: clickedText,
    selected_value: selectedValue,
    canonical_query: String(stableItem.canonical_query ?? selectedValue),
    selection_kind: String(stableItem.selection_kind ?? ''),
    coords_ready: true,
    lat: lat as number,
    lon: lon as number,
    matched_token: matchedToken ?? apiResult.matched_token,
    api_count: apiResult.items.length,
    attempts_used: Math.max(apiResult.attempts_used, dropdownState.attemptsUsed),
    retried: apiResult.retried || dropdownState.attemptsUsed > 1,
    source: stableItem.source ? String(stableItem.source) : null,
    confidence:
      typeof confidenceValue === 'number' || typeof confidenceValue === 'string'
        ? confidenceValue
        : null,
  };
}

export async function runRouteScenario(
  page: Page,
  request: APIRequestContext,
  scenario: RouteCase,
  artifacts?: {
    preSearchPath?: string;
  },
): Promise<RouteSmokeResult> {
  await clearAutocompleteCache(request);
  const geocodeRequests: string[] = [];
  const tripPayloads: Record<string, unknown>[] = [];
  const onRequest = (pendingRequest: PlaywrightRequest): void => {
    const requestUrl = pendingRequest.url();
    if (requestUrl.includes('/api/geocode?')) {
      geocodeRequests.push(requestUrl);
    }
    if (requestUrl.includes('/v1/trip/')) {
      try {
        tripPayloads.push(pendingRequest.postDataJSON() as Record<string, unknown>);
      } catch {
        // Request payload shape is asserted after the run when available.
      }
    }
  };
  page.on('request', onRequest);
  try {
    if (scenario.mode === 'departure') {
      await page.click('#tab-departure');
    } else {
      await page.click('#tab-arrival');
    }

    const originSelection = await selectAutocompleteEntry(
      page,
      request,
      '#origin',
      '#origin-ac',
      scenario.origin_query,
    );
    const destinationSelection = await selectAutocompleteEntry(
      page,
      request,
      '#destination',
      '#dest-ac',
      scenario.destination_query,
    );

    await page.fill(
      '#datetime-input',
      formatFutureLocalDatetime(scenario.future_offset_days, scenario.time_hhmm),
    );
    if (artifacts?.preSearchPath) {
      await saveCapture(page, artifacts.preSearchPath);
    }
    const streamPath =
      scenario.mode === 'departure'
        ? '/v1/trip/recommended-departure-time/stream'
        : '/v1/trip/arrival-time-with-recommendation/stream';
    await page.click('#search-btn');

    const results = page.locator('#results');
    const recommendationCard = page.locator('.recommendation-card').last();
    try {
      await expect(recommendationCard).toBeVisible({ timeout: 180000 });
    } catch {
      const errorText = ((await page.locator('#error-msg').textContent()) ?? '').trim();
      throw new Error(
        `route UI did not render recommendation for ${scenario.origin_query} -> ` +
          `${scenario.destination_query}; error=${errorText || '-'}`,
      );
    }
    await expect(results).toBeVisible({ timeout: 30000 });
    await expect(page.locator('#error-msg')).toBeHidden({ timeout: 5000 });
    if (process.env.TTS_PROVIDER === 'mock') {
      await expect(results).toContainText('mock 모드 결과 안내', { timeout: 5000 });
    } else {
      await expect(results).not.toContainText('mock 모드 결과 안내', { timeout: 5000 });
    }
    await expect(recommendationCard).toContainText('추천 출발 시간', { timeout: 30000 });
    if (scenario.mode === 'departure') {
      await expect(results).toContainText('추천 출발 시각', { timeout: 180000 });
    } else {
      await expect(results).toContainText('출발 시각 분석', { timeout: 180000 });
    }
    const candidateBadge = recommendationCard.locator('.candidate-badge').first();
    await expect(candidateBadge).toBeVisible({ timeout: 120000 });
    const candidateBadgeText = ((await candidateBadge.textContent()) ?? '').trim();
    const candidateCount = parseCandidateCount(candidateBadgeText);
    expect(candidateCount, 'candidate badge count').toBeGreaterThan(0);
    const tooltipTemplate = candidateBadge.locator('.candidate-tooltip-panel');
    const tooltipText = ((await tooltipTemplate.textContent()) ?? '').trim();
    await candidateBadge.hover({ force: true }).catch(() => {});
    const tooltipVisible = await page
      .locator('.candidate-tooltip-portal.is-visible')
      .first()
      .isVisible()
      .catch(() => false);
    expect(
      tooltipVisible || tooltipText.length > 0,
      `candidate tooltip missing for ${scenario.origin_query} -> ${scenario.destination_query}`,
    ).toBeTruthy();
    await expect
      .poll(async () => page.locator('.leaflet-marker-icon').count(), {
        timeout: 10000,
      })
      .toBeGreaterThanOrEqual(2);
    await expect
      .poll(async () => page.locator('.leaflet-overlay-pane path, .leaflet-overlay-pane .leaflet-interactive').count(), {
        timeout: 15000,
      })
      .toBeGreaterThanOrEqual(1);

    const durationText = ((await recommendationCard.locator('.duration-big').textContent()) ?? '').trim();
    const durationMinutes = parseDurationMinutes(durationText);
    expect(durationMinutes, 'recommendation duration').toBeGreaterThan(0);
    const recommendedDeparture = await readResultValue(
      recommendationCard,
      '추천 출발 시간',
    );
    const expectedArrival = await readResultValue(
      recommendationCard,
      '추천 출발시 예상 도착 시간',
    );
    const recommendedDepartureTs = parseDisplayedDatetime(recommendedDeparture);
    const expectedArrivalTs = parseDisplayedDatetime(expectedArrival);
    expect(recommendedDepartureTs, 'recommended departure').not.toBeNull();
    expect(expectedArrivalTs, 'expected arrival').not.toBeNull();
    expect(expectedArrivalTs as number).toBeGreaterThanOrEqual(recommendedDepartureTs as number);
    expect(
      geocodeRequests,
      `selected autocomplete entries should not trigger /api/geocode for ${scenario.origin_query} -> ${scenario.destination_query}`,
    ).toHaveLength(0);
    expect(
      tripPayloads.length,
      `trip request should be submitted for ${scenario.origin_query} -> ${scenario.destination_query}`,
    ).toBeGreaterThan(0);
    for (const payload of tripPayloads) {
      assertStrictRoutePlacePayload(payload, 'origin');
      assertStrictRoutePlacePayload(payload, 'dest');
    }

    return {
      mode: scenario.mode,
      stream_path: streamPath,
      origin_selection: originSelection,
      destination_selection: destinationSelection,
      recommended_departure_time: recommendedDeparture,
      expected_arrival_time: expectedArrival,
      duration_seconds: durationMinutes * 60,
      candidate_count: candidateCount,
      geocode_request_count: geocodeRequests.length,
      trip_payload_count: tripPayloads.length,
    };
  } finally {
    page.off('request', onRequest);
  }
}

export function assertStrictRoutePlacePayload(
  payload: Record<string, unknown>,
  side: 'origin' | 'dest',
): void {
  const placeKey = side === 'origin' ? 'origin_place' : 'dest_place';
  const coordsKey = side === 'origin' ? 'origin_coords' : 'dest_coords';
  const place = payload[placeKey] as Record<string, unknown> | undefined;
  const coords = payload[coordsKey] as Record<string, unknown> | undefined;
  expect(place, `${placeKey} missing from route request`).toBeTruthy();
  expect(place?.coords_ready, `${placeKey}.coords_ready`).toBe(true);
  expect(parseFiniteNumber(place?.lat), `${placeKey}.lat`).not.toBeNull();
  expect(parseFiniteNumber(place?.lon), `${placeKey}.lon`).not.toBeNull();
  expect(coords, `${coordsKey} transitional mirror missing`).toBeTruthy();
  expect(parseFiniteNumber(coords?.lat), `${coordsKey}.lat`).not.toBeNull();
  expect(parseFiniteNumber(coords?.lon), `${coordsKey}.lon`).not.toBeNull();
}

function loadJson<T>(relativePath: string): T {
  const absolutePath = resolve(process.cwd(), relativePath);
  return JSON.parse(readFileSync(absolutePath, 'utf-8')) as T;
}

function matchesAutocompleteExpectation(
  items: Record<string, unknown>[],
  testCase: AutocompleteCase,
): boolean {
  if (items.length < testCase.min_results) {
    return false;
  }

  const topItems = items.slice(0, 5);
  const tokenMatched = testCase.expected_any.some((token) =>
    topItems.some((item) => {
      const text = `${item.display_name ?? ''} ${item.address ?? ''}`;
      return normalizeText(text).includes(normalizeText(token));
    }),
  );
  if (!tokenMatched) {
    return false;
  }

  if (!testCase.require_coords) {
    return true;
  }

  return hasReadyCoords(topItems);
}

function normalizeText(value: string): string {
  return value.replace(/\s+/g, '').toLowerCase();
}

function hasReadyCoords(items: Record<string, unknown>[]): boolean {
  return items.some((item) => {
    const lat = parseFiniteNumber(item.lat);
    const lon = parseFiniteNumber(item.lon);
    return item.coords_ready === true && lat !== null && lon !== null;
  });
}

function buildQueryTokens(query: string): string[] {
  const tokens = [query, ...query.split(/\s+/)];
  const seen = new Set<string>();
  const normalizedTokens: string[] = [];
  for (const token of tokens) {
    const trimmed = token.trim();
    if (!trimmed) {
      continue;
    }
    const normalized = normalizeText(trimmed);
    if (!normalized || seen.has(normalized)) {
      continue;
    }
    seen.add(normalized);
    normalizedTokens.push(trimmed);
  }
  return normalizedTokens;
}

function stripTransitSuffix(value: string): string {
  return value
    .replace(/\s*\([^)]*\)\s*/g, ' ')
    .replace(
      /\s*(\d+호선|신분당선|수인분당선|경의중앙선|공항철도|경춘선|경강선|ktx|srt)\s*$/i,
      '',
    )
    .trim();
}

function findMatchingDropdownIndex(texts: string[], tokens: string[]): number {
  const normalizedTokens = tokens.map((token) => normalizeText(token));
  return texts.findIndex((value) => {
    const normalizedValue = normalizeText(value);
    return normalizedTokens.some((token) => normalizedValue.includes(token));
  });
}

function findMatchedTokenInItems(
  items: Record<string, unknown>[],
  tokens: string[],
): string | null {
  const normalizedTokens = tokens.map((token) => ({
    raw: token,
    normalized: normalizeText(token),
  }));
  for (const token of normalizedTokens) {
    const matched = items.slice(0, 5).some((item) => {
      const text = `${item.display_name ?? ''} ${item.address ?? ''}`;
      return normalizeText(text).includes(token.normalized);
    });
    if (matched) {
      return token.raw;
    }
  }
  return null;
}

function findMatchedTokenInTexts(texts: string[], tokens: string[]): string | null {
  const normalizedTexts = texts.map((value) => normalizeText(value));
  for (const token of tokens) {
    const normalizedToken = normalizeText(token);
    if (normalizedTexts.some((value) => value.includes(normalizedToken))) {
      return token;
    }
  }
  return null;
}

function findPreferredDropdownIndex(
  texts: string[],
  query: string,
  tokens: string[],
): number {
  const normalizedQuery = normalizeText(query);
  let bestIndex = -1;
  let bestScore = Number.NEGATIVE_INFINITY;
  for (const [index, value] of texts.entries()) {
    const normalizedValue = normalizeText(value);
    if (!normalizedValue) {
      continue;
    }
    let score = 0;
    if (normalizedValue.includes(normalizedQuery)) {
      score += 120;
    }
    if (normalizedValue.startsWith(normalizedQuery)) {
      score += 160;
    }
    if (normalizedValue.includes('검색어')) {
      score -= 120;
      if (normalizedValue.startsWith(normalizedQuery)) {
        score += 220;
      }
    } else {
      score += 120;
    }
    if (!normalizedQuery.includes('맛집') && normalizedValue.includes('맛집')) {
      score -= 200;
    }
    if (!normalizedQuery.includes('카페') && normalizedValue.includes('카페')) {
      score -= 180;
    }
    if (!normalizedQuery.includes('스타벅스') && normalizedValue.includes('스타벅스')) {
      score -= 180;
    }
    if (
      !normalizedQuery.includes('모바일테스트룸') &&
      normalizedValue.includes('모바일테스트룸')
    ) {
      score -= 320;
    }
    if (
      !normalizedQuery.includes('전기차충전소') &&
      normalizedValue.includes('전기차충전소')
    ) {
      score -= 300;
    }
    if (!normalizedQuery.includes('atm') && normalizedValue.includes('atm')) {
      score -= 260;
    }
    if (!normalizedQuery.includes('휴맥스') && normalizedValue.includes('휴맥스')) {
      score -= 120;
    }
    if (!normalizedQuery.includes('은행') && normalizedValue.includes('은행')) {
      score -= 150;
    }
    if (!normalizedQuery.includes('srt') && normalizedValue.includes('srt')) {
      score -= 40;
    }
    if (!normalizedQuery.includes('ktx') && normalizedValue.includes('ktx')) {
      score -= 20;
    }
    for (const token of tokens) {
      const normalizedToken = normalizeText(token);
      if (normalizedToken && normalizedValue.includes(normalizedToken)) {
        score += 30;
      }
    }
    if (score > bestScore) {
      bestScore = score;
      bestIndex = index;
    }
  }
  return bestIndex;
}

function parseFiniteNumber(value: unknown): number | null {
  if (value === null || value === undefined) {
    return null;
  }
  if (typeof value === 'string' && !value.trim()) {
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function autocompleteItemText(item: Record<string, unknown>): string {
  return `${item.display_name ?? ''} ${item.address ?? ''} ${item.canonical_query ?? ''}`.trim();
}

function assertAutocompleteItemsShape(
  items: Record<string, unknown>[],
  query: string,
): void {
  for (const [index, item] of items.entries()) {
    const lat = parseFiniteNumber(item.lat);
    const lon = parseFiniteNumber(item.lon);
    if (typeof item.coords_ready !== 'boolean') {
      throw new Error(
        `autocomplete returned candidate without coords_ready for ${query} index=${index} item=${JSON.stringify(item)}`,
      );
    }
    if (
      (item.coords_ready === true && (lat === null || lon === null)) ||
      (item.coords_ready === false && (lat !== null || lon !== null))
    ) {
      throw new Error(
        `autocomplete returned candidate with inconsistent coords for ${query} index=${index} item=${JSON.stringify(item)}`,
      );
    }
    if (
      !String(item.canonical_query ?? '').trim() ||
      !String(item.selection_kind ?? '').trim()
    ) {
      throw new Error(
        `autocomplete returned candidate without stable fields for ${query} index=${index} item=${JSON.stringify(item)}`,
      );
    }
  }
}

function findSelectedAutocompleteItem(
  items: Record<string, unknown>[],
  clickedText: string,
  selectedValue: string,
  tokens: string[],
): Record<string, unknown> | null {
  const normalizedClickedText = normalizeText(clickedText);
  const normalizedSelectedValue = normalizeText(selectedValue);
  const strippedSelectedValue = normalizeText(stripTransitSuffix(selectedValue));
  let bestItem: Record<string, unknown> | null = null;
  let bestScore = Number.NEGATIVE_INFINITY;

  for (const item of items.slice(0, 10)) {
    const combinedText = normalizeText(autocompleteItemText(item));
    const displayName = normalizeText(String(item.display_name ?? ''));
    if (!combinedText) {
      continue;
    }
    let score = 0;
    if (normalizedClickedText && combinedText.includes(normalizedClickedText)) {
      score += 240;
    }
    if (normalizedSelectedValue && combinedText.includes(normalizedSelectedValue)) {
      score += 220;
    }
    if (strippedSelectedValue && combinedText.includes(strippedSelectedValue)) {
      score += 140;
    }
    if (normalizedSelectedValue && displayName === normalizedSelectedValue) {
      score += 180;
    }
    if (strippedSelectedValue && displayName === strippedSelectedValue) {
      score += 120;
    }
    for (const token of tokens) {
      const normalizedToken = normalizeText(token);
      if (normalizedToken && combinedText.includes(normalizedToken)) {
        score += 30;
      }
    }
    if (item.coords_ready === true) {
      score += 80;
    }
    if (parseFiniteNumber(item.lat) !== null && parseFiniteNumber(item.lon) !== null) {
      score += 80;
    }
    if (score > bestScore) {
      bestScore = score;
      bestItem = item;
    }
  }

  return bestScore > 0 ? bestItem : null;
}

function formatFutureLocalDatetime(offsetDays: number, hhmm: string): string {
  const [hoursText, minutesText] = hhmm.split(':');
  const hours = Number(hoursText);
  const minutes = Number(minutesText);
  const current = new Date();
  current.setSeconds(0, 0);
  current.setDate(current.getDate() + offsetDays);
  current.setHours(hours, minutes, 0, 0);
  const yyyy = String(current.getFullYear());
  const mm = String(current.getMonth() + 1).padStart(2, '0');
  const dd = String(current.getDate()).padStart(2, '0');
  const hh = String(current.getHours()).padStart(2, '0');
  const mi = String(current.getMinutes()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}T${hh}:${mi}`;
}

export async function saveInitialAndFinalCaptures(
  page: Page,
  initialPath: string,
  finalPath: string,
  action: () => Promise<void>,
): Promise<void> {
  await saveCapture(page, initialPath);
  await action();
  await saveCapture(page, finalPath);
}

export async function locatorTexts(locator: Locator, count: number): Promise<string[]> {
  return locator.evaluateAll(
    (nodes, targetCount) =>
      nodes
        .slice(0, targetCount as number)
        .map((node) => (node.textContent ?? '').trim()),
    count,
  );
}

type DropdownState = {
  texts: string[];
  matchedToken: string | null;
  attemptsUsed: number;
};

async function populateAutocompleteDropdown(
  page: Page,
  request: APIRequestContext,
  inputSelector: string,
  dropdownSelector: string,
  query: string,
  tokens: string[],
  retryOnceOnEmpty: boolean,
): Promise<DropdownState> {
  const input = page.locator(inputSelector);
  const dropdownItems = page.locator(`${dropdownSelector} .ac-item`);
  const maxAttempts = retryOnceOnEmpty ? 2 : 1;
  const visibilityTimeoutMs = /\d/.test(query) ? 45000 : 20000;
  let lastTexts: string[] = [];

  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    await input.click();
    await input.fill('');
    await page.waitForTimeout(150);
    await input.fill(query);

    try {
      await expect(dropdownItems.first()).toBeVisible({ timeout: visibilityTimeoutMs });
    } catch {
      if (attempt + 1 < maxAttempts) {
        continue;
      }
      throw new Error(`autocomplete dropdown not visible for ${query}`);
    }

    lastTexts = await locatorTexts(dropdownItems, 5);
    const matchedToken = findMatchedTokenInTexts(lastTexts, tokens);
    if (matchedToken) {
      return {
        texts: lastTexts,
        matchedToken,
        attemptsUsed: attempt + 1,
      };
    }
    if (attempt + 1 >= maxAttempts) {
      break;
    }
  }

  throw new Error(`autocomplete dropdown mismatch for ${query}: ${JSON.stringify(lastTexts)}`);
}

async function readResultValue(card: Locator, label: string): Promise<string> {
  const row = card.locator('.result-row').filter({ hasText: label }).first();
  await expect(row).toBeVisible({ timeout: 15000 });
  return ((await row.locator('.value').textContent()) ?? '').trim();
}

function parseDisplayedDatetime(value: string): number | null {
  const match = value.match(
    /(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})/,
  );
  if (!match) {
    return null;
  }
  const [, yearText, monthText, dayText, hourText, minuteText] = match;
  const date = new Date(
    Number(yearText),
    Number(monthText) - 1,
    Number(dayText),
    Number(hourText),
    Number(minuteText),
    0,
    0,
  );
  const millis = date.getTime();
  return Number.isNaN(millis) ? null : millis;
}

function parseDurationMinutes(value: string): number {
  const normalized = value.replace(/\s+/g, ' ').trim();
  const hourMatch = normalized.match(/(\d+)\s*시간/);
  const minuteMatch = normalized.match(/(\d+)\s*분/);
  const hours = hourMatch ? Number(hourMatch[1]) : 0;
  const minutes = minuteMatch ? Number(minuteMatch[1]) : 0;
  return hours * 60 + minutes;
}

function parseCandidateCount(value: string): number {
  const match = value.match(/분석\s*(\d+)개/);
  return match ? Number(match[1]) : 0;
}
