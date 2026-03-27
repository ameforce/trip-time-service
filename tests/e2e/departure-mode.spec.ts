import { expect, test } from '@playwright/test';

import { saveCapture, setupCommonMocks } from './mocks';

test.describe('도착 시각 기준 모드', () => {
  test('참고 baseline 문구와 추천 결과 문맥이 일관된다', async ({ page }, testInfo) => {
    await setupCommonMocks(page);

    await page.route('**/api/route**', async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 700));
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          code: 'Ok',
          routes: [
            {
              geometry: {
                type: 'LineString',
                coordinates: [
                  [127.0276, 37.4979],
                  [127.1112, 37.3948],
                ],
              },
            },
          ],
        }),
      });
    });

    await page.route('**/v1/trip/arrival-time', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          route: { origin: '강남역', destination: '판교역' },
          departure_time: '2099-01-24T08:30:00+09:00',
          arrival_time: '2099-01-24T09:10:00+09:00',
          duration_seconds: 2400,
          provider: 'mock',
          cache_hit: false,
        }),
      });
    });

    await page.route('**/v1/trip/recommended-departure-time/stream', async (route) => {
      const streamPayload = [
        `event: plan\ndata: ${JSON.stringify({
          checked: 0,
          planned: 4,
          remaining: 4,
          total_candidates: 12,
        })}`,
        `event: candidate\ndata: ${JSON.stringify({
          candidate: {
            departure_time: '2099-01-24T08:30:00+09:00',
            arrival_time: '2099-01-24T09:31:00+09:00',
            duration_seconds: 3660,
            meets_deadline: false,
            phase: 'coarse',
            score_total: 0.55,
            score_duration: 0.45,
            score_time_proximity: 0.5,
            score_night_drive: 0.8,
            score_stability: 0.7,
            score_improvement_efficiency: 0.4,
          },
          progress: {
            checked: 1,
            planned: 4,
            remaining: 3,
            total_candidates: 12,
          },
        })}`,
        `event: recommendation\ndata: ${JSON.stringify({
          route: { origin: '강남역', destination: '판교역' },
          desired_arrival_time: '2099-01-24T09:30:00+09:00',
          recommended_departure_time: '2099-01-24T08:41:00+09:00',
          expected_arrival_time: '2099-01-24T09:20:00+09:00',
          duration_seconds: 2340,
          meets_deadline: true,
          provider: 'mock',
          provider_calls: 6,
          candidates_checked: 6,
          planned_queries: 6,
          total_candidates: 12,
          latest_departure_time: '2099-01-24T08:41:00+09:00',
          latest_departure_arrival_time: '2099-01-24T09:20:00+09:00',
          latest_departure_duration_seconds: 2340,
          safe_departure_time: '2099-01-24T08:31:00+09:00',
          safe_departure_duration_seconds: 2880,
          recommended_score_total: 0.77,
          baseline_score_total: 0.63,
          candidate_evaluations: [
            {
              departure_time: '2099-01-24T08:30:00+09:00',
              arrival_time: '2099-01-24T09:31:00+09:00',
              duration_seconds: 3660,
              meets_deadline: false,
              phase: 'coarse',
              score_total: 0.55,
              score_duration: 0.45,
              score_time_proximity: 0.5,
              score_night_drive: 0.8,
              score_stability: 0.7,
              score_improvement_efficiency: 0.4,
            },
            {
              departure_time: '2099-01-24T08:41:00+09:00',
              arrival_time: '2099-01-24T09:20:00+09:00',
              duration_seconds: 2340,
              meets_deadline: true,
              phase: 'refine',
              score_total: 0.77,
              score_duration: 0.8,
              score_time_proximity: 0.72,
              score_night_drive: 0.82,
              score_stability: 0.78,
              score_improvement_efficiency: 0.68,
            },
          ],
        })}`,
        'event: end\ndata: {"ok":true}',
      ].join('\n\n') + '\n\n';

      await route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/event-stream; charset=utf-8' },
        body: streamPayload,
      });
    });

    await page.goto('/');
    await expect(page.locator('#search-btn')).toBeVisible();
    await saveCapture(page, testInfo.outputPath('01-entry.png'));

    await page.click('#tab-departure');
    await page.fill('#origin', '강남역');
    await page.fill('#destination', '판교역');
    await page.fill('#datetime-input', '2099-01-24T09:30');
    await page.click('#search-btn');

    await expect(page.locator('#results')).toContainText('추천 출발 시각 계산 중');
    await expect(page.locator('#results')).toContainText(/분석 \d+개 \/ \d+개/);
    await expect(page.locator('#results')).toContainText(/남은 후보 \d+개/);
    await saveCapture(page, testInfo.outputPath('02-after-action.png'));

    await expect(page.locator('#results')).toBeVisible();
    await expect(page.locator('#results')).toContainText('추천 출발 시각');
    await expect(page.locator('#results')).toContainText('mock 모드 결과 안내');
    await expect(page.locator('#results')).toContainText('참고 단일 조회(현재 시각 출발 기준)');
    await expect(page.locator('#results')).toContainText('참고용: 현재 시각 출발 기준 단일 조회');
    await expect(page.locator('#results')).toContainText('타이트 출발 시간');
    await expect(page.locator('#results')).toContainText('희망 도착 대비 도착 시간 차이');
    await expect(page.locator('.recommendation-card .row-recommended-departure .value')).toHaveText(
      '2099-01-24 08:30'
    );
    await expect(page.locator('.recommendation-card .row-baseline-departure .value')).toHaveText(
      '2099-01-24 08:40'
    );
    await expect(
      page.locator('.recommendation-card .result-row', { hasText: '타이트 소요시간' }).locator('.value')
    ).toHaveText('40분');
    await expect(
      page
        .locator('.recommendation-card .result-row', { hasText: '안정적 소요시간' })
        .locator('.value')
    ).toHaveText('50분');
    await expect(page.locator('.candidate-tooltip-template').first()).toContainText('정시 도착 가능');
    await expect(page.locator('.candidate-tooltip-template').first()).toContainText('총점');
    await expect(page.locator('.candidate-tooltip-template').first()).toContainText('시간 효율');
    const recommendedCalendarLink = page.locator(
      '.recommendation-card .calendar-action-btn.is-recommended'
    );
    const tightCalendarLink = page.locator(
      '.recommendation-card .calendar-action-btn.is-tight'
    );
    await expect(recommendedCalendarLink).toBeVisible();
    await expect(tightCalendarLink).toBeVisible();
    const recommendedHref = await recommendedCalendarLink.getAttribute('href');
    const tightHref = await tightCalendarLink.getAttribute('href');
    expect(recommendedHref).not.toBeNull();
    expect(tightHref).not.toBeNull();
    const recommendedUrl = new URL(recommendedHref!);
    const tightUrl = new URL(tightHref!);
    expect(recommendedUrl.origin).toBe('https://calendar.google.com');
    expect(recommendedUrl.pathname).toBe('/calendar/render');
    expect(recommendedUrl.searchParams.get('action')).toBe('TEMPLATE');
    expect(recommendedUrl.searchParams.get('text')).toContain('추천 출발');
    expect(recommendedUrl.searchParams.get('dates')).toBe(
      '20990123T233000Z/20990124T001000Z'
    );
    expect(tightUrl.origin).toBe('https://calendar.google.com');
    expect(tightUrl.pathname).toBe('/calendar/render');
    expect(tightUrl.searchParams.get('action')).toBe('TEMPLATE');
    expect(tightUrl.searchParams.get('text')).toContain('타이트 출발');
    expect(tightUrl.searchParams.get('dates')).toBe(
      '20990123T234000Z/20990124T002000Z'
    );

    await saveCapture(page, testInfo.outputPath('03-final.png'));
  });

  test('안정적 추천 도착이 늦으면 실패 배지와 도착 차이가 일치한다', async ({ page }) => {
    await setupCommonMocks(page);

    await page.route('**/v1/trip/arrival-time', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          route: { origin: '강남역', destination: '판교역' },
          departure_time: '2099-01-24T08:50:00+09:00',
          arrival_time: '2099-01-24T09:20:00+09:00',
          duration_seconds: 1800,
          provider: 'mock',
          cache_hit: false,
        }),
      });
    });

    await page.route('**/v1/trip/recommended-departure-time/stream', async (route) => {
      const streamPayload = [
        `event: plan\ndata: ${JSON.stringify({
          checked: 0,
          planned: 10,
          remaining: 10,
          total_candidates: 20,
        })}`,
        `event: recommendation\ndata: ${JSON.stringify({
          route: { origin: '강남역', destination: '판교역' },
          desired_arrival_time: '2099-01-24T09:30:00+09:00',
          recommended_departure_time: '2099-01-24T09:10:00+09:00',
          expected_arrival_time: '2099-01-24T09:25:00+09:00',
          duration_seconds: 900,
          meets_deadline: true,
          provider: 'mock',
          provider_calls: 4,
          candidates_checked: 4,
          planned_queries: 10,
          total_candidates: 20,
          latest_departure_time: '2099-01-24T09:10:00+09:00',
          latest_departure_arrival_time: '2099-01-24T09:25:00+09:00',
          latest_departure_duration_seconds: 900,
          safe_departure_time: '2099-01-24T09:20:00+09:00',
          safe_departure_duration_seconds: 1800,
          recommended_score_total: 0.72,
          baseline_score_total: 0.61,
          candidate_evaluations: [
            {
              departure_time: '2099-01-24T09:10:00+09:00',
              arrival_time: '2099-01-24T09:25:00+09:00',
              duration_seconds: 900,
              meets_deadline: true,
              phase: 'coarse',
              score_total: 0.61,
              score_duration: 0.7,
              score_time_proximity: 0.6,
              score_night_drive: 0.8,
              score_stability: 0.7,
              score_improvement_efficiency: 0.5,
            },
          ],
        })}`,
        'event: end\ndata: {"ok":true}',
      ].join('\n\n') + '\n\n';

      await route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/event-stream; charset=utf-8' },
        body: streamPayload,
      });
    });

    await page.goto('/');
    await page.click('#tab-departure');
    await page.fill('#origin', '강남역');
    await page.fill('#destination', '판교역');
    await page.fill('#datetime-input', '2099-01-24T09:30');
    await page.click('#search-btn');

    const recommendationCard = page.locator('.recommendation-card');
    await expect(recommendationCard).toContainText('추천 출발 시각');
    await expect(recommendationCard.locator('.result-meta')).toContainText('정시 도착 불가');
    await expect(recommendationCard).toContainText('늦음');
    await expect(
      recommendationCard.locator('.calendar-action-btn.is-recommended')
    ).toBeVisible();
    await expect(
      recommendationCard.locator('.calendar-action-btn.is-tight')
    ).toBeVisible();
  });
});
