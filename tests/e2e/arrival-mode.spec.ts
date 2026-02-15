import { expect, test } from '@playwright/test';

import { saveCapture, setupCommonMocks } from './mocks';

test.describe('출발 시각 기준 모드', () => {
  test('분석 카드 선노출 후 추천 카드로 갱신된다', async ({ page }, testInfo) => {
    await setupCommonMocks(page);

    await page.route('**/v1/trip/arrival-time-with-recommendation/stream', async (route) => {
      const streamPayload = [
        `event: arrival\ndata: ${JSON.stringify({
          arrival: {
            route: { origin: '강남역', destination: '판교역' },
            departure_time: '2099-01-24T09:00:00+09:00',
            arrival_time: '2099-01-24T09:32:00+09:00',
            duration_seconds: 1920,
            provider: 'mock',
            cache_hit: false,
          },
          immediate_safe_departure: {
            safe_departure_time: '2099-01-24T09:00:00+09:00',
            safe_duration_seconds: 2400,
            clamped_to_now: false,
          },
          progress: {
            checked: 0,
            planned: 3,
            remaining: 3,
            total_candidates: 6,
          },
        })}`,
        `event: recommendation\ndata: ${JSON.stringify({
          route: { origin: '강남역', destination: '판교역' },
          desired_arrival_time: '2099-01-24T09:32:00+09:00',
          recommended_departure_time: '2099-01-24T09:15:00+09:00',
          expected_arrival_time: '2099-01-24T09:31:00+09:00',
          duration_seconds: 960,
          meets_deadline: true,
          provider: 'mock',
          provider_calls: 3,
          candidates_checked: 3,
          planned_queries: 3,
          total_candidates: 6,
          latest_departure_time: '2099-01-24T09:00:00+09:00',
          latest_departure_arrival_time: '2099-01-24T09:32:00+09:00',
          latest_departure_duration_seconds: 1920,
          safe_departure_time: '2099-01-24T09:00:00+09:00',
          safe_departure_duration_seconds: 2400,
          recommended_score_total: 0.84,
          baseline_score_total: 0.52,
          candidate_evaluations: [
            {
              departure_time: '2099-01-24T09:00:00+09:00',
              arrival_time: '2099-01-24T09:32:00+09:00',
              duration_seconds: 1920,
              meets_deadline: false,
              phase: 'coarse',
              score_total: 0.52,
              score_duration: 0.5,
              score_time_proximity: 0.4,
              score_night_drive: 0.8,
              score_stability: 0.6,
              score_improvement_efficiency: 0.3,
            },
            {
              departure_time: '2099-01-24T09:15:00+09:00',
              arrival_time: '2099-01-24T09:31:00+09:00',
              duration_seconds: 960,
              meets_deadline: true,
              phase: 'refine',
              score_total: 0.84,
              score_duration: 0.85,
              score_time_proximity: 0.7,
              score_night_drive: 0.8,
              score_stability: 0.9,
              score_improvement_efficiency: 0.8,
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

    await page.fill('#origin', '강남역');
    await page.fill('#destination', '판교역');
    await page.fill('#datetime-input', '2099-01-24T09:00');

    await page.click('#search-btn');

    await expect(page.locator('#results')).toContainText('추천 출발 시각 계산 중');
    await saveCapture(page, testInfo.outputPath('02-after-action.png'));

    await expect(page.locator('#results')).toContainText('추천 출발 시각');
    await expect(page.locator('#results')).toContainText('출발 시각 분석');
    await expect(page.locator('#results')).toContainText('지정 출발 시간');
    await expect(page.locator('#results')).toContainText('지정 출발 대비 도착 시간 차이');
    await expect(page.locator('.candidate-tooltip-template').first()).toContainText(
      '지정 출발보다 빠름'
    );

    await saveCapture(page, testInfo.outputPath('03-final.png'));
  });
});
