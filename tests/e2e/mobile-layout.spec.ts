import { expect, test } from '@playwright/test';

test.describe('mobile layout', () => {
  test('keeps sidebar controls from overlapping content when open', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/');
    await page.locator('#mobile-toggle').click();
    await page.locator('#sidebar.open').waitFor({ state: 'visible' });
    await page.waitForTimeout(350);

    const geometry = await page.evaluate(() => {
      function rect(selector: string) {
        const element = document.querySelector(selector);
        if (!element) return null;
        const box = element.getBoundingClientRect();
        return {
          left: box.left,
          top: box.top,
          right: box.right,
          bottom: box.bottom,
          width: box.width,
          height: box.height,
        };
      }

      function overlap(
        left: ReturnType<typeof rect>,
        right: ReturnType<typeof rect>,
      ) {
        if (!left || !right) return 0;
        const x = Math.max(
          0,
          Math.min(left.right, right.right) - Math.max(left.left, right.left),
        );
        const y = Math.max(
          0,
          Math.min(left.bottom, right.bottom) - Math.max(left.top, right.top),
        );
        return x * y;
      }

      const versionStyle = window.getComputedStyle(
        document.querySelector('#version-badge')!,
      );

      return {
        toggleLogoOverlap: overlap(rect('#mobile-toggle'), rect('.logo')),
        versionDisplay: versionStyle.display,
      };
    });

    expect(geometry.toggleLogoOverlap).toBe(0);
    expect(geometry.versionDisplay).toBe('none');
  });
});
