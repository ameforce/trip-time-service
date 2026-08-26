import { expect, test } from '@playwright/test';

/**
 * 시트 완전 숨김 상태의 복원 UX 검증.
 *
 * 스와이프 다운으로 숨긴 시트를 좌상단 햄버거 외에도 복원할 수 있어야 한다:
 *  - 하단 중앙 복원 핸들 표시(발견 가능성)
 *  - 핸들 탭 / 지도 배경 탭 으로 복원
 */
test.describe('mobile sheet restore', () => {
  async function dismissSheetViaDrag(page: import('@playwright/test').Page) {
    const header = page.locator('.sidebar-header');
    const box = await header.boundingBox();
    expect(box).not.toBeNull();

    await page.mouse.move(box!.x + box!.width / 2, box!.y + 8);
    await page.mouse.down();
    for (let i = 1; i <= 12; i++) {
      await page.mouse.move(box!.x + box!.width / 2, box!.y + 8 + i * 25, {
        steps: 1,
      });
      await page.waitForTimeout(16);
    }
    await page.mouse.up();
    await expect(page.locator('#sidebar')).toHaveClass(/sheet-hidden/);
  }

  test('restore handle appears when sheet is fully hidden', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/');
    const handle = page.locator('#sheet-restore');

    // 초기(peek) 상태에서는 숨김
    await expect(handle).toBeHidden();

    await dismissSheetViaDrag(page);

    // 완전 숨김 상태에서 하단 중앙에 핸들이 나타난다
    await expect(handle).toBeVisible();
    const rect = await handle.boundingBox();
    expect(rect).not.toBeNull();
    // 화면 하단 근처 중앙 배치
    expect(rect!.y).toBeGreaterThan(700);
    expect(Math.abs(rect!.x + rect!.width / 2 - 390 / 2)).toBeLessThanOrEqual(8);
  });

  test('tapping the restore handle brings the sheet back', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/');
    await dismissSheetViaDrag(page);
    const handle = page.locator('#sheet-restore');
    await expect(handle).toBeVisible();

    await handle.click();

    await expect(page.locator('#sidebar')).not.toHaveClass(/sheet-hidden/);
    await expect(handle).toBeHidden();
  });

  test('tapping the map background restores the hidden sheet', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/');
    await dismissSheetViaDrag(page);
    const handle = page.locator('#sheet-restore');
    await expect(handle).toBeVisible();

    // 지도 배경(마커/컨트롤이 없는 위치)을 탭
    await page.mouse.click(300, 250);

    await expect(page.locator('#sidebar')).not.toHaveClass(/sheet-hidden/);
    await expect(handle).toBeHidden();
  });

  test('dragging the handle upward restores the sheet', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/');
    await dismissSheetViaDrag(page);
    const handle = page.locator('#sheet-restore');
    await expect(handle).toBeVisible();
    const box = await handle.boundingBox();
    expect(box).not.toBeNull();

    await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
    await page.mouse.down();
    for (let i = 1; i <= 6; i++) {
      await page.mouse.move(
        box!.x + box!.width / 2,
        box!.y + box!.height / 2 - i * 10,
        { steps: 1 },
      );
      await page.waitForTimeout(16);
    }
    await page.mouse.up();

    await expect(page.locator('#sidebar')).not.toHaveClass(/sheet-hidden/);
    await expect(handle).toBeHidden();
  });
});
