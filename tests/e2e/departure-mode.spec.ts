import { expect, test } from '@playwright/test';

import { saveCapture, setupCommonMocks } from './mocks';

test.describe('도착 시각 기준 모드', () => {
  test('참고 baseline 문구와 추천 결과 문맥이 일관된다', async ({ page }, testInfo) => {
    await setupCommonMocks(page);

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

    await page.route('**/v1/trip/recommended-departure-time', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          route: { origin: '강남역', destination: '판교역' },
          desired_arrival_time: '2099-01-24T09:30:00+09:00',
          recommended_departure_time: '2099-01-24T08:42:00+09:00',
          expected_arrival_time: '2099-01-24T09:26:00+09:00',
          duration_seconds: 2640,
          meets_deadline: true,
          provider: 'mock',
          provider_calls: 6,
          candidates_checked: 6,
          planned_queries: 6,
          total_candidates: 12,
          latest_departure_time: '2099-01-24T08:40:00+09:00',
          latest_departure_arrival_time: '2099-01-24T09:30:00+09:00',
          latest_departure_duration_seconds: 3000,
          safe_departure_time: '2099-01-24T08:20:00+09:00',
          safe_departure_duration_seconds: 3750,
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
              departure_time: '2099-01-24T08:42:00+09:00',
              arrival_time: '2099-01-24T09:26:00+09:00',
              duration_seconds: 2640,
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
        }),
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

    await expect(page.locator('#results')).toBeVisible();
    await saveCapture(page, testInfo.outputPath('02-after-action.png'));

    await expect(page.locator('#results')).toContainText('추천 출발 시각');
    await expect(page.locator('#results')).toContainText('참고 단일 조회(현재 시각 출발 기준)');
    await expect(page.locator('#results')).toContainText('참고용: 현재 시각 출발 기준 단일 조회');
    await expect(page.locator('#results')).toContainText('참고 출발 시간(현재 기준)');
    await expect(page.locator('#results')).toContainText('희망 도착 대비 도착 시간 차이');
    await expect(page.locator('.candidate-tooltip-template').first()).toContainText('정시 도착 가능');

    await saveCapture(page, testInfo.outputPath('03-final.png'));
  });
});
