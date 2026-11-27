import { expect, test, type Page } from '@playwright/test';

const route = { origin: '서울역', destination: '판교역' };
const arrival = {
  route,
  provider: 'mock',
  departure_time: '2030-06-04T06:10:00Z',
  arrival_time: '2030-06-04T06:41:12Z',
  duration_seconds: 1872,
};

function recommendationFor(deadline: string) {
  const desired = new Date(deadline);
  const departure = new Date(desired.getTime() - 1872 * 1000).toISOString();
  return {
    ...arrival,
    recommended_departure_time: departure,
    latest_departure_time: departure,
    latest_departure_arrival_time: deadline,
    latest_departure_duration_seconds: 1872,
    safe_departure_time: new Date(desired.getTime() - 2340 * 1000).toISOString(),
    safe_departure_duration_seconds: 2340,
    expected_arrival_time: deadline,
    desired_arrival_time: deadline,
    meets_deadline: true,
  };
}

function frame(name: string, data: unknown) {
  return 'event: ' + name + '\r\ndata: ' + JSON.stringify(data) + '\r\n\r\n';
}

async function configure(page: Page, timezone = 'Asia/Seoul') {
  await page.route('https://unpkg.com/**', (request) => request.abort());
  await page.route('https://fonts.googleapis.com/**', (request) => request.abort());
  await page.route('**/api/config', (request) =>
    request.fulfill({ json: { timezone, step_minutes: 10, provider: 'mock', version: 'v0.4.3' } })
  );
  await page.route('**/api/geocode?*', (request) =>
    request.fulfill({ json: [{ lat: 37.55, lon: 126.97 }] })
  );
}

async function enterRoute(page: Page) {
  await page.locator('#origin').fill(route.origin);
  await page.locator('#destination').fill(route.destination);
}

for (const [serviceZone, browserZone, deadline] of [
  ['Asia/Seoul', 'America/Los_Angeles', '2030-06-04T07:27:00.000Z'],
  ['America/Los_Angeles', 'UTC', '2030-06-04T23:27:00.000Z'],
  ['UTC', 'America/Los_Angeles', '2030-06-04T16:27:00.000Z'],
]) {
  test.describe('service clock ' + serviceZone, () => {
    test.use({ timezoneId: browserZone });

    test('preserves selected minute through request, recommendation, and calendar', async ({ page }) => {
      await configure(page, serviceZone);
      await page.clock.setFixedTime(new Date('2030-06-04T06:00:00Z'));
      await page.route('**/v1/trip/arrival-time', (request) => request.fulfill({ json: arrival }));
      await page.route('**/v1/trip/recommended-departure-time/stream', (request) => {
        const requested = request.request().postDataJSON().desired_arrival_time;
        return request.fulfill({
          contentType: 'text/event-stream',
          body: frame('recommendation', recommendationFor(requested)) + frame('end', { ok: true }),
        });
      });
      await page.goto('/');
      await expect(page.locator('#service-timezone')).toHaveText('기준 시간대 ' + serviceZone);
      await enterRoute(page);
      await page.locator('#tab-departure').click();
      await page.locator('#datetime-input').fill('2030-06-04T16:30');
      await page.locator('#datetime-toggle').click();
      await page.locator('#datetime-minute').selectOption('27');
      await page.locator('#datetime-toggle').click();
      const request = page.waitForRequest('**/v1/trip/recommended-departure-time/stream');
      await page.locator('#search-btn').click();
      expect((await request).postDataJSON().desired_arrival_time).toBe(deadline);
      await expect(page.locator('#datetime-input')).toHaveValue('2030-06-04T16:27');
      const result = page.locator('#results > *').first();
      await expect(result).toHaveClass(/recommendation-card/);
      await expect(result).toContainText('2030-06-04 16:27');
      await expect(result.locator('.duration-big')).toHaveText('39분');
      await expect(page.locator('.calc-explain-row').filter({ hasText: '추가된 여유 시간' }))
        .toContainText('+7분 48초');
      const href = await page.getByRole('link', { name: '추천 일정 추가' }).getAttribute('href');
      const params = new URL(href!).searchParams;
      expect(params.get('ctz')).toBe(serviceZone);
      expect(params.get('dates')?.split('/')[1]).toBe(deadline.slice(0, 19).replace(/[-:]/g, '') + 'Z');
      expect(params.get('details')).toContain('희망 도착: 2030-06-04 16:27');
    });
  });
}

test('validates the current instant in the service zone and rejects a DST gap', async ({ page }) => {
  await configure(page, 'America/Los_Angeles');
  await page.clock.setFixedTime(new Date('2030-03-10T08:05:00Z'));
  await page.goto('/');
  await enterRoute(page);
  await page.locator('#datetime-input').fill('2030-03-09T23:59');
  await page.locator('#search-btn').click();
  await expect(page.locator('#error-msg')).toContainText('현재 시점 이전');
  await page.locator('#datetime-input').fill('2030-03-10T02:25');
  await page.locator('#search-btn').click();
  await expect(page.locator('#error-msg')).toContainText('존재하지 않는 시각');
  await expect(page.locator('#datetime-input')).toHaveValue('2030-03-10T02:25');
});

test('keeps baseline on late failure and retries only by explicit action', async ({ page }) => {
  await configure(page);
  await page.clock.setFixedTime(new Date('2030-06-04T06:00:00Z'));
  await page.addInitScript(({ origin, destination }) => {
    localStorage.setItem('tts_recent_searches', JSON.stringify([{ origin, destination, ts: Date.now() }]));
    localStorage.setItem('tts_favorites', JSON.stringify([{ name: '즐겨찾는 출발지', address: origin, lat: 37.55, lon: 126.97 }]));
  }, route);
  let streamRequests = 0;
  let syncRecommendations = 0;
  await page.route('**/v1/trip/recommended-departure-time', (request) => {
    syncRecommendations++;
    return request.abort();
  });
  await page.route('**/v1/trip/arrival-time-with-recommendation/stream', (request) => {
    streamRequests++;
    const response = streamRequests === 1
      ? frame('error', { reason: 'provider_degraded', provider_bucket: 'panel_parse_timeout' })
      : frame('recommendation', recommendationFor('2030-06-04T06:41:12Z')) + frame('end', { ok: true });
    return request.fulfill({
      contentType: 'text/event-stream',
      body: frame('arrival', { arrival }) + response,
    });
  });
  await page.goto('/');
  await expect(page.locator('#map-status')).toContainText('이동 시간 검색은 계속');
  await expect(page.locator('#recent-list')).toContainText(route.origin);
  await expect(page.locator('#favorites-list')).toContainText('즐겨찾는 출발지');
  await enterRoute(page);
  await page.locator('#search-btn').click();
  await expect(page.locator('.baseline-card')).toContainText('31분 12초');
  await expect(page.locator('#search-status')).toContainText('네이버 교통 정보를 제시간에 읽지 못했습니다');
  await expect(page.locator('#search-btn')).toBeEnabled();
  expect(streamRequests).toBe(1);
  expect(syncRecommendations).toBe(0);
  await page.getByRole('button', { name: '다시 시도' }).click();
  await expect(page.locator('#results > *').first()).toHaveClass(/recommendation-card/);
  await expect(page.locator('#error-box')).toBeHidden();
  expect(streamRequests).toBe(2);
  expect(syncRecommendations).toBe(0);
});

test('shows busy and validation errors from HTTP responses without automatic retry', async ({ page }) => {
  await configure(page);
  await page.clock.setFixedTime(new Date('2030-06-04T06:00:00Z'));
  let attempts = 0;
  await page.route('**/v1/trip/arrival-time-with-recommendation/stream', (request) => {
    attempts++;
    return attempts === 1
      ? request.fulfill({ status: 503, json: { detail: 'busy' } })
      : request.fulfill({ status: 422, json: { detail: [{ msg: '잘못된 시간' }, { msg: '경로 확인 필요' }] } });
  });
  await page.goto('/');
  await enterRoute(page);
  await page.locator('#search-btn').click();
  await expect(page.locator('#error-msg')).toContainText('검색 요청이 많습니다');
  expect(attempts).toBe(1);
  await page.getByRole('button', { name: '다시 시도' }).click();
  await expect(page.locator('#error-msg')).toHaveText('잘못된 시간 / 경로 확인 필요');
  await expect(page.locator('#retry-search')).toBeHidden();
  expect(attempts).toBe(2);
});

test('picker edit and mode change abort the active reader and restore search controls', async ({ page }) => {
  await configure(page);
  await page.clock.setFixedTime(new Date('2030-06-04T06:00:00Z'));
  await page.addInitScript(({ baselineFrame }) => {
    const origFetch = window.fetch.bind(window);
    const probe = Object.assign(window, { tripStreamRequests: 0, tripReaderCancels: 0, tripFetchAborts: 0 });
    window.fetch = (resource, options) => {
      if (!String(resource).endsWith('/v1/trip/arrival-time-with-recommendation/stream')) {
        return origFetch(resource, options);
      }
      probe.tripStreamRequests++;
      options?.signal?.addEventListener('abort', () => probe.tripFetchAborts++, { once: true });
      const stream = new ReadableStream({
        start: (controller) => controller.enqueue(new TextEncoder().encode(baselineFrame)),
        cancel: () => { probe.tripReaderCancels++; },
      });
      return Promise.resolve(new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } }));
    };
  }, { baselineFrame: frame('arrival', { arrival }) });
  await page.goto('/');
  await enterRoute(page);
  await page.locator('#search-btn').click();
  await expect(page.locator('#results')).toBeVisible();
  await expect(page.locator('#search-btn')).toBeDisabled();
  await page.locator('#datetime-toggle').click();
  await page.locator('#datetime-minute').selectOption('27');
  await expect(page.locator('#results')).toBeHidden();
  await expect(page.locator('#search-btn')).toBeEnabled();
  await page.locator('#datetime-toggle').click();
  await page.locator('#search-btn').click();
  await expect(page.locator('#results')).toBeVisible();
  await page.locator('#tab-departure').click();
  await expect(page.locator('#results')).toBeHidden();
  await expect(page.locator('#search-btn')).toBeEnabled();
  await expect.poll(() => page.evaluate(() => {
    const probe = window as unknown as { tripStreamRequests: number; tripReaderCancels: number; tripFetchAborts: number };
    return [probe.tripStreamRequests, probe.tripReaderCancels, probe.tripFetchAborts];
  })).toEqual([2, 2, 2]);
});

test('combobox identifies its active option and clears it on close', async ({ page }) => {
  await configure(page);
  await page.route('**/api/autocomplete?*', (request) =>
    request.fulfill({ json: [{
      display_name: route.origin, address: route.origin, lat: 37.55, lon: 126.97,
      source: 'fixture', selection_kind: 'poi', canonical_query: route.origin, coords_ready: true,
    }] })
  );
  await page.goto('/');
  const origin = page.getByRole('combobox', { name: '출발지' });
  await origin.fill(route.origin);
  const option = page.getByRole('listbox').getByRole('option');
  await expect(option).toBeVisible();
  await origin.press('ArrowDown');
  await expect(origin).toHaveAttribute('aria-activedescendant', (await option.getAttribute('id'))!);
  await expect(option).toHaveAttribute('aria-selected', 'true');
  await origin.press('Escape');
  await expect(origin).toHaveAttribute('aria-expanded', 'false');
  await expect(origin).not.toHaveAttribute('aria-activedescendant');
});

test('a busy deadline baseline leaves an explicit retry without starting a recommendation', async ({ page }) => {
  await configure(page);
  await page.clock.setFixedTime(new Date('2030-06-04T06:00:00Z'));
  let baselineRequests = 0;
  let recommendationRequests = 0;
  let baselineCompleted = false;
  await page.route('**/v1/trip/arrival-time', async (request) => {
    baselineRequests++;
    if (baselineRequests === 1) {
      await request.fulfill({ status: 503, json: { reason: 'capacity_exhausted' } });
      return;
    }
    baselineCompleted = true;
    await request.fulfill({ json: arrival });
  });
  await page.route('**/v1/trip/recommended-departure-time/stream', (request) => {
    recommendationRequests++;
    expect(baselineCompleted).toBe(true);
    return request.fulfill({
      contentType: 'text/event-stream',
      body: frame('error', { reason: 'stream_stall_timeout' }),
    });
  });
  await page.goto('/');
  await enterRoute(page);
  await page.locator('#tab-departure').click();
  await page.locator('#search-btn').click();
  await expect(page.locator('#error-msg')).toContainText('검색 요청이 많습니다');
  expect(baselineRequests).toBe(1);
  expect(recommendationRequests).toBe(0);

  await page.getByRole('button', { name: '다시 시도' }).click();
  await expect(page.locator('#error-msg')).toContainText('검색 응답이 지연되었습니다');
  await expect(page.locator('.baseline-card')).toBeVisible();
  expect(baselineRequests).toBe(2);
  expect(recommendationRequests).toBe(1);
});


test('deadline search succeeds on the fixture service at capacity one', async ({ page }) => {
  test.skip(process.env.TTS_E2E_FIXTURE_MODE !== '1' || process.env.TTS_PROVIDER !== 'mock' ||
    process.env.TTS_STREAM_WORKERS !== '1', 'requires the mock fixture service with one trip worker');
  await page.route('https://unpkg.com/**', (request) => request.abort());
  await page.goto('/');
  const input = page.locator('#datetime-input');
  await expect(input).not.toHaveValue('');
  const tomorrow = new Date((await input.inputValue()) + ':00Z');
  tomorrow.setUTCDate(tomorrow.getUTCDate() + 1);
  const deadline = tomorrow.toISOString().slice(0, 14) + '27';
  await enterRoute(page);
  await page.locator('#tab-departure').click();
  await input.fill(deadline);
  await page.locator('#search-btn').click();
  const recommendation = page.locator('.recommendation-card');
  await expect(recommendation).toContainText(deadline.replace('T', ' '));
  await expect(page.locator('.baseline-card')).toBeVisible();
  await expect(page.locator('#error-box')).toBeHidden();
});

test('search button text contrast is sufficient at rest and on hover', async ({ page }) => {
  await configure(page);
  await page.goto('/');
  const button = page.getByRole('button', { name: '검색', exact: true });
  const contrast = () => button.evaluate((element) => {
    function luminance(color: string) {
      const channels = color.match(/\d+/g)!.slice(0, 3).map((channel) => {
        const value = Number(channel) / 255;
        return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
      });
      return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
    }
    const style = getComputedStyle(element);
    const text = luminance(style.color);
    const background = luminance(style.backgroundColor);
    return (Math.max(text, background) + 0.05) / (Math.min(text, background) + 0.05);
  });
  expect(await contrast()).toBeGreaterThanOrEqual(4.5);
  await button.hover();
  await expect.poll(contrast).toBeGreaterThanOrEqual(4.5);
});
