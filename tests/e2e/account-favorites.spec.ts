import { randomUUID } from 'node:crypto';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';

import { expect, test, type Page } from '@playwright/test';

const GUEST_KEY = 'tts_favorites';
const SEOUL = {
  name: '이 브라우저의 장소', query: '서울역', address: '서울 용산구 한강대로 405',
  lat: 37.5547, lon: 126.9707,
};
// The aliases match the other station so falling back to geocoding by name would swap the coordinates.
const GANGNAM = {
  name: '판교역', query: '강남역', address: '서울 강남구 강남대로 396',
  lat: 37.4979, lon: 127.0276,
};
const PANGYO = {
  name: '강남역', query: '판교역', address: '경기 성남시 분당구 판교역로 160',
  lat: 37.3948, lon: 127.1112,
};
const PRIVATE = {
  name: '계정 전용 장소', query: '수서역', address: '서울 강남구 밤고개로 99',
  lat: 37.4875, lon: 127.1019,
};
type Place = typeof SEOUL;

const desktopViewport = { width: 1366, height: 900 };
const mobileViewport = { width: 390, height: 844 };

test.use({ trace: 'off', screenshot: 'off', video: 'off', actionTimeout: 12_000 });
test.setTimeout(75_000);
test.beforeEach(({ baseURL }) => {
  const hostname = baseURL ? new URL(baseURL).hostname : '';
  test.skip(
    process.env.TTS_E2E_FIXTURE_MODE !== '1' ||
      process.env.TTS_PROVIDER !== 'mock' ||
      !['127.0.0.1', 'localhost', '[::1]'].includes(hostname),
    'Account browser tests run only against the isolated local mock server',
  );
});

function account(prefix: string) {
  return { username: prefix + randomUUID().slice(0, 8), password: randomUUID() };
}

function favoriteList(page: Page) {
  return page.locator('#favorites-list');
}

function favorite(page: Page, place: Place) {
  return favoriteList(page).getByRole('listitem')
    .filter({ has: page.getByText(place.name, { exact: true }) })
    .filter({ has: page.getByText(place.address, { exact: true }) });
}

async function capture(page: Page, name: string) {
  const dir = process.env.TTS_E2E_ACCOUNT_SCREENSHOTS_DIR;
  if (!dir) return;
  mkdirSync(dir, { recursive: true });
  await page.screenshot({ path: join(dir, name + '.png'), animations: 'disabled' });
}

async function open(page: Page, mobile = false) {
  const response = page.waitForResponse((res) => new URL(res.url()).pathname === '/api/auth/me');
  await page.goto('/');
  expect((await response).ok()).toBeTruthy();
  if (mobile && !(await page.locator('#sidebar').evaluate((sidebar) => sidebar.classList.contains('open')))) {
    await page.locator('#mobile-toggle').click();
  }
  await expect(page.getByRole('button', { name: '장소 추가', exact: true })).toBeInViewport({ ratio: 1 });
}

async function submitAccount(
  page: Page,
  mode: 'register' | 'login',
  credentials: ReturnType<typeof account>,
) {
  const action = mode === 'register' ? '회원가입' : '로그인';
  await page.getByRole('button', { name: '로그인', exact: true }).click();
  const dialog = page.getByRole('dialog');
  if (mode === 'register') await dialog.getByRole('button', { name: action, exact: true }).click();
  await page.getByLabel('아이디', { exact: true }).fill(credentials.username);
  await page.getByLabel('비밀번호', { exact: true }).fill(credentials.password);
  const response = page.waitForResponse((res) =>
    new URL(res.url()).pathname === '/api/auth/' + mode && res.request().method() === 'POST'
  );
  await page.locator('#account-form').getByRole('button', { name: action, exact: true }).click();
  expect((await response).ok()).toBeTruthy();
  await expect(dialog).toBeHidden();
  await expect(page.getByRole('button', { name: '내 계정 · ' + credentials.username, exact: true })).toBeVisible();
}

async function fillPlace(page: Page, place: Place) {
  await page.getByRole('button', { name: '장소 추가', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '즐겨찾기 추가' });
  await dialog.getByRole('searchbox', { name: '장소 검색', exact: true }).fill(place.query);
  await dialog.getByRole('button', { name: '검색', exact: true }).click();
  await dialog.getByRole('list', { name: '장소 검색 결과' })
    .getByRole('button').filter({ hasText: place.address }).click();
  await dialog.getByLabel('즐겨찾기 이름').fill(place.name);
}

async function addPlace(page: Page, place: Place) {
  await fillPlace(page, place);
  await page.getByRole('button', { name: '저장', exact: true }).click();
  await expect(favorite(page, place)).toBeVisible();
}

function savedPlace({ name, address, lat, lon }: Place) {
  return { name, address, lat, lon };
}

async function guestPlaces(page: Page) {
  return page.evaluate((key) => JSON.parse(localStorage.getItem(key) ?? '[]'), GUEST_KEY);
}

async function snapshot(page: Page) {
  const response = await page.request.get('/api/auth/me');
  expect(response.ok()).toBeTruthy();
  return response.json();
}

async function logout(page: Page) {
  await page.locator('#account-toggle').click();
  await page.getByRole('button', { name: '로그아웃', exact: true }).click();
  await expect(page.getByRole('button', { name: '로그인', exact: true })).toBeVisible();
}

async function selectPlace(page: Page, place: Place, field: '출발지' | '도착지') {
  await favorite(page, place).getByRole('button', { name: field, exact: true }).click();
  await expect(page.getByRole('combobox', { name: field, exact: true })).toHaveValue(place.address);
}

async function searchWithFavoriteCoordinates(page: Page) {
  const request = page.waitForRequest((req) =>
    new URL(req.url()).pathname.endsWith('/arrival-time-with-recommendation/stream') &&
      req.method() === 'POST'
  );
  await page.getByRole('button', { name: '검색', exact: true }).click();
  expect((await request).postDataJSON()).toMatchObject({
    origin_coords: { lat: GANGNAM.lat, lon: GANGNAM.lon },
    dest_coords: { lat: PANGYO.lat, lon: PANGYO.lon },
    origin_place: { display_name: GANGNAM.name, canonical_query: GANGNAM.address },
    dest_place: { display_name: PANGYO.name, canonical_query: PANGYO.address },
  });
  await expect(page.locator('#search-status')).toContainText('추천 출발 시각을 확인할 수 있습니다.');
  await expect(page.locator('#results')).toBeVisible();
  await page.locator('#results').scrollIntoViewIfNeeded();
}

test('syncs favorites into a fresh browser and uses their exact coordinates', async ({
  baseURL, browser, page,
}) => {
  await page.setViewportSize(desktopViewport);
  const owner = account('장소검증');
  await open(page);
  await capture(page, 'desktop-01-initial');
  await submitAccount(page, 'register', owner);
  expect(await snapshot(page)).toMatchObject({ user: { username: owner.username }, favorites: [] });
  await addPlace(page, GANGNAM);
  await addPlace(page, PANGYO);

  const secondContext = await browser.newContext({ baseURL, viewport: desktopViewport });
  try {
    const second = await secondContext.newPage();
    await open(second);
    await expect(favorite(second, GANGNAM)).toHaveCount(0);
    await submitAccount(second, 'login', owner);
    await expect(favorite(second, GANGNAM)).toBeVisible();
    await expect(favorite(second, PANGYO)).toBeVisible();
    await capture(second, 'desktop-02-synced');
    await selectPlace(second, GANGNAM, '출발지');
    await selectPlace(second, PANGYO, '도착지');
    await searchWithFavoriteCoordinates(second);
    await capture(second, 'desktop-03-route');
    expect(await second.evaluate(() => JSON.parse(localStorage.getItem('tts_recent_searches') ?? '[]')))
      .toEqual([]);

    await logout(second);
    await expect(second.getByRole('combobox', { name: '출발지', exact: true })).toHaveValue('');
    await expect(second.getByRole('combobox', { name: '도착지', exact: true })).toHaveValue('');
    await expect(second.locator('#results')).toBeHidden();
    await expect(favoriteList(second)).toBeEmpty();

    await submitAccount(second, 'login', owner);
    await favorite(second, GANGNAM).getByRole('button', { name: '삭제', exact: true }).click();
    await expect(favorite(second, GANGNAM)).toHaveCount(0);
    await open(page);
    await expect(favorite(page, PANGYO)).toBeVisible();
    await expect(favorite(page, GANGNAM)).toHaveCount(0);
    expect(await snapshot(page)).toMatchObject({ favorites: [savedPlace(PANGYO)] });
  } finally {
    await secondContext.close();
  }
});

test('keeps guest places local until opt-in, and isolates them from another account on mobile', async ({
  baseURL, browser,
}) => {
  const mobile = await browser.newContext({ baseURL, viewport: mobileViewport, isMobile: true, hasTouch: true });
  try {
    const page = await mobile.newPage();
    const owner = account('모바일검증');
    const other = account('다른계정');
    await open(page, true);
    await expect(favoriteList(page)).toBeEmpty();
    await capture(page, 'mobile-01-initial');
    await addPlace(page, SEOUL);
    expect(await guestPlaces(page)).toMatchObject([savedPlace(SEOUL)]);

    await submitAccount(page, 'register', owner);
    await expect(favorite(page, SEOUL)).toHaveCount(0);
    expect(await snapshot(page)).toMatchObject({ user: { username: owner.username }, favorites: [] });
    const importButton = page.getByRole('button', { name: /가져오기/ });
    await expect(importButton).toBeVisible();
    await capture(page, 'mobile-02-opt-in');
    await importButton.click();
    await expect(favorite(page, SEOUL)).toBeVisible();

    const { user } = await snapshot(page);
    expect(user).toMatchObject({ id: expect.any(String), username: owner.username });
    const importResponse = await page.request.post('/api/favorites/import', {
      headers: { 'X-Requested-With': 'TripTime', 'X-TripTime-Account': user.id },
      data: { favorites: [savedPlace(SEOUL)] },
    });
    expect(importResponse.ok()).toBeTruthy();
    expect(await importResponse.json()).toMatchObject({ import: { imported: 0, skipped: 1 } });
    expect(await snapshot(page)).toMatchObject({ favorites: [savedPlace(SEOUL)] });

    await addPlace(page, PRIVATE);
    await expect(favorite(page, PRIVATE)).toBeVisible();
    await capture(page, 'mobile-03-saved');
    await logout(page);
    await expect(favorite(page, SEOUL)).toBeVisible();
    await expect(favorite(page, PRIVATE)).toHaveCount(0);
    expect(await guestPlaces(page)).toMatchObject([savedPlace(SEOUL)]);

    await submitAccount(page, 'register', other);
    await expect(favorite(page, SEOUL)).toHaveCount(0);
    await expect(favorite(page, PRIVATE)).toHaveCount(0);
    expect(await snapshot(page)).toMatchObject({ user: { username: other.username }, favorites: [] });

    const otherPlace = { ...PANGYO, name: PRIVATE.name };
    const extraPlace = { ...SEOUL, name: '추가 시험 장소' };
    for (const place of [otherPlace, GANGNAM, PANGYO]) await addPlace(page, place);
    await fillPlace(page, extraPlace);
    await page.getByRole('button', { name: '저장', exact: true }).click();
    await expect(page.getByRole('dialog')).toBeHidden();
    const expand = page.getByRole('button', { name: '즐겨찾기 1개 더 보기', exact: true });
    await expect(favoriteList(page).getByRole('listitem')).toHaveCount(3);
    await expect(expand).toHaveAttribute('aria-expanded', 'false');
    await expand.click();
    await expect(favorite(page, extraPlace)).toBeVisible();
    const collapse = page.getByRole('button', { name: '즐겨찾기 접기', exact: true });
    await expect(collapse).toHaveAttribute('aria-expanded', 'true');
    await collapse.click();
    await expect(favorite(page, extraPlace)).toHaveCount(0);

    await page.locator('#account-toggle').click();
    const dialog = page.getByRole('dialog');
    const deleteAccount = dialog.getByRole('button', { name: '계정 삭제', exact: true });
    const confirmDelete = dialog.getByRole('button', { name: '계정과 즐겨찾기 삭제', exact: true });
    await deleteAccount.click();
    await expect(confirmDelete).toBeVisible();
    await dialog.getByRole('button', { name: '취소', exact: true }).click();
    await expect(confirmDelete).toBeHidden();
    expect(await snapshot(page)).toMatchObject({
      favorites: [otherPlace, GANGNAM, PANGYO, extraPlace].map(savedPlace),
    });

    await deleteAccount.click();
    await dialog.getByLabel('현재 비밀번호').fill(other.password);
    const removal = page.waitForResponse((res) =>
      new URL(res.url()).pathname === '/api/auth/account' && res.request().method() === 'DELETE'
    );
    await confirmDelete.click();
    expect((await removal).status()).toBe(204);
    await expect(favorite(page, SEOUL)).toBeVisible();
    await expect(favorite(page, otherPlace)).toHaveCount(0);
    expect(await snapshot(page)).toMatchObject({ user: null, favorites: [] });

    await submitAccount(page, 'login', owner);
    await expect(favorite(page, PRIVATE)).toBeVisible();
    expect(await snapshot(page)).toMatchObject({ favorites: [savedPlace(SEOUL), savedPlace(PRIVATE)] });
  } finally {
    await mobile.close();
  }
});

test('rejects stale saves after loss or replacement of the session', async ({ context, page }) => {
  const owner = account('만료검증');
  await page.addInitScript((place) => {
    if (localStorage.getItem('tts_favorites') === null) {
      localStorage.setItem('tts_favorites', JSON.stringify([place]));
    }
  }, savedPlace(SEOUL));
  await open(page);
  await submitAccount(page, 'register', owner);
  await addPlace(page, PRIVATE);
  await fillPlace(page, GANGNAM);
  await context.clearCookies();

  const rejected = page.waitForResponse((res) =>
    new URL(res.url()).pathname === '/api/favorites' && res.request().method() === 'POST'
  );
  await page.locator('#fav-add-btn').click();
  expect((await rejected).status()).toBe(401);
  await expect(page.getByRole('button', { name: '로그인', exact: true })).toBeVisible();
  await expect(favorite(page, SEOUL)).toBeVisible();
  await expect(favorite(page, PRIVATE)).toHaveCount(0);
  await expect(favorite(page, GANGNAM)).toHaveCount(0);

  await submitAccount(page, 'login', owner);
  await expect(favorite(page, PRIVATE)).toBeVisible();
  await expect(favorite(page, GANGNAM)).toHaveCount(0);
  expect(await snapshot(page)).toMatchObject({ favorites: [savedPlace(PRIVATE)] });

  await fillPlace(page, PANGYO);
  const other = account('세션전환');
  // Another login can replace the shared cookie while the first page is still open.
  const switched = await page.request.post('/api/auth/register', {
    headers: { 'X-Requested-With': 'TripTime' },
    data: other,
  });
  expect(switched.status()).toBe(201);
  const stale = page.waitForResponse((res) =>
    new URL(res.url()).pathname === '/api/favorites' && res.request().method() === 'POST'
  );
  await page.getByRole('button', { name: '저장', exact: true }).click();
  expect((await stale).status()).toBe(409);
  await expect(page.getByRole('dialog')).toBeHidden();
  await expect(favoriteList(page)).toBeEmpty();
  await page.getByRole('button', { name: '내 계정 · ' + other.username, exact: true }).click();
  await expect(page.getByRole('dialog').getByText(other.username, { exact: true })).toBeVisible();
  expect(await snapshot(page)).toMatchObject({ user: { username: other.username }, favorites: [] });
});

test('blocks guest saves while a signed-in account snapshot is unresolved', async ({ page }) => {
  const previous = account('이전계정');
  const current = account('연결확인');
  await open(page);
  await addPlace(page, SEOUL);
  await submitAccount(page, 'register', previous);
  await addPlace(page, PRIVATE);

  // A different tab can replace the context cookie before this page reloads.
  const replacement = await page.request.post('/api/auth/register', {
    headers: { 'X-Requested-With': 'TripTime' },
    data: current,
  });
  expect(replacement.status()).toBe(201);

  let markRequested!: () => void;
  let releaseLookup!: () => void;
  const requestStarted = new Promise<void>((resolve) => { markRequested = resolve; });
  const release = new Promise<void>((resolve) => { releaseLookup = resolve; });
  let firstLookup = true;
  await page.route('**/api/auth/me', async (route) => {
    if (!firstLookup || route.request().method() !== 'GET') {
      await route.continue();
      return;
    }
    firstLookup = false;
    markRequested();
    await release;
    await route.fulfill({ status: 503, json: { detail: 'Account fixture temporarily unavailable' } });
  });

  const add = page.getByRole('button', { name: '장소 추가', exact: true });
  const status = page.locator('#favorites-status');
  const retry = page.getByRole('button', { name: '계정 다시 불러오기', exact: true });
  try {
    await page.reload({ waitUntil: 'domcontentloaded' });
    await requestStarted;
    await expect(add).toBeDisabled();
    await expect(status).toContainText(/계정.*확인/);
    await expect(retry).toBeHidden();
    await expect(favorite(page, PRIVATE)).toHaveCount(0);
    expect(await guestPlaces(page)).toMatchObject([savedPlace(SEOUL)]);
  } finally {
    releaseLookup();
  }
  await expect(status).toContainText('계정 상태를 확인하지 못했습니다.');
  await expect(retry).toBeEnabled();
  await expect(add).toBeDisabled();
  await expect(favorite(page, PRIVATE)).toHaveCount(0);
  expect(await guestPlaces(page)).toMatchObject([savedPlace(SEOUL)]);

  await retry.click();
  await expect(page.getByRole('button', { name: '내 계정 · ' + current.username, exact: true })).toBeVisible();
  await expect(add).toBeEnabled();
  await expect(favoriteList(page)).toBeEmpty();
  await addPlace(page, PANGYO);
  expect(await snapshot(page)).toMatchObject({
    user: { username: current.username },
    favorites: [savedPlace(PANGYO)],
  });
  expect(await guestPlaces(page)).toMatchObject([savedPlace(SEOUL)]);
});

test('clears private route state when the same username is registered again', async ({ page }) => {
  const original = account('재가입검증');
  await open(page);
  await submitAccount(page, 'register', original);
  await addPlace(page, PRIVATE);
  await selectPlace(page, PRIVATE, '출발지');
  const { user } = await snapshot(page);
  expect(user).toMatchObject({ id: expect.any(String), username: original.username });

  const deleted = await page.request.delete('/api/auth/account', {
    headers: { 'X-Requested-With': 'TripTime', 'X-TripTime-Account': user.id },
    data: { password: original.password },
  });
  expect(deleted.status()).toBe(204);
  const recreated = await page.request.post('/api/auth/register', {
    headers: { 'X-Requested-With': 'TripTime' },
    data: { username: original.username, password: randomUUID() },
  });
  expect(recreated.status()).toBe(201);
  const fresh = await recreated.json();
  expect(fresh).toMatchObject({ user: { id: expect.any(String), username: original.username }, favorites: [] });
  expect(fresh.user.id).not.toBe(user.id);

  await favorite(page, PRIVATE).getByRole('button', { name: '도착지', exact: true }).click();
  await expect(favoriteList(page)).toBeEmpty();
  await expect(page.getByRole('combobox', { name: '출발지', exact: true })).toHaveValue('');
  await expect(page.getByRole('combobox', { name: '도착지', exact: true })).toHaveValue('');
  expect(await guestPlaces(page)).toEqual([]);
  expect(await snapshot(page)).toMatchObject({ user: fresh.user, favorites: [] });
});

test('matches provider geocode coordinates to the selected favorite address', async ({ page }) => {
  const matching = {
    name: '즐겨찾는 카페', query: '카페 위치 검증', address: '경기 성남시 분당구 판교역로 160',
    lat: 37.3948, lon: 127.1112,
  };
  const conflicting = {
    name: '주소 검증', query: '서울 중구 세종대로 110', address: '서울 중구 세종대로 110',
    lat: 37.5641, lon: 126.9761,
  };
  const matchGeocodeQuery = '판교역로 160';
  const conflictGeocodeQuery = '세종대로 110';
  const candidates = new Map([
    [matching.query, {
      display_name: matching.query, address: matching.address,
      selection_kind: 'poi', canonical_query: matchGeocodeQuery, lat: null, lon: null,
    }],
    [conflicting.query, {
      display_name: conflicting.address, address: conflicting.address,
      selection_kind: 'address', canonical_query: conflictGeocodeQuery, lat: null, lon: null,
    }],
  ]);
  const geocodes = new Map([
    [matchGeocodeQuery, {
      display_name: matchGeocodeQuery + ' (' + matching.address + ')', address: matching.address,
      lat: matching.lat, lon: matching.lon,
    }],
    [conflictGeocodeQuery, {
      // An echoed name does not verify a road address with a different street number.
      display_name: conflictGeocodeQuery, address: '서울 중구 세종대로 99',
      lat: conflicting.lat, lon: conflicting.lon,
    }],
  ]);
  await page.route('**/api/autocomplete?*', async (route) => {
    const candidate = candidates.get(new URL(route.request().url()).searchParams.get('q') ?? '');
    if (candidate) await route.fulfill({ json: [candidate] });
    else await route.fallback();
  });
  const answeredGeocodes = new Set<string>();
  await page.route('**/api/geocode?*', async (route) => {
    const query = new URL(route.request().url()).searchParams.get('q') ?? '';
    const geocode = geocodes.get(query);
    if (!geocode) {
      await route.fallback();
      return;
    }
    answeredGeocodes.add(query);
    await route.fulfill({ json: [geocode] });
  });

  await open(page);
  expect(await snapshot(page)).toEqual({ user: null, favorites: [] });
  await addPlace(page, matching);
  expect(await guestPlaces(page)).toEqual([savedPlace(matching)]);

  await fillPlace(page, conflicting);
  const dialog = page.getByRole('dialog', { name: '즐겨찾기 추가' });
  await expect(dialog.getByText(/선택한 장소의 위치를 확인하지 못했습니다/)).toBeVisible();
  await expect(dialog.getByRole('button', { name: '저장', exact: true })).toBeDisabled();
  await dialog.getByLabel('즐겨찾기 이름').press('Enter');
  await expect(dialog).toBeVisible();
  expect(await guestPlaces(page)).toEqual([savedPlace(matching)]);
  expect(answeredGeocodes).toEqual(new Set([matchGeocodeQuery, conflictGeocodeQuery]));
});
