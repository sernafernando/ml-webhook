import { test, expect } from '@playwright/test';

test.describe('home-webhooks', () => {
  test('loads first page and shows webhook table', async ({ page }) => {
    await page.route('**/api/webhooks/topics', async route => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([{ topic: 'items', count: 2 }]),
      });
    });

    await page.route('**/api/webhooks?**', async route => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          topic: 'items',
          events: [{
            resource: '/items/MLA1',
            received_at: '2026-04-10 15:00:00',
            db_preview: { title: 'Producto demo', price: 10, currency_id: 'ARS' },
          }],
          pagination: { limit: 100, total: 1, mode: 'offset', next_cursor: null, offset: 0 },
        }),
      });
    });

    await page.goto('/');

    await expect(page.getByTestId('home-webhooks-title')).toBeVisible();
    await expect(page.getByTestId('preview-title-/items/MLA1')).toHaveText('Producto demo');
    await expect(page.getByTestId('pagination-range-badge')).toHaveText(/Mostrando 1 - 1 de 1/i);
  });

  test('shows paused polling badge when tab is hidden', async ({ page }) => {
    await page.route('**/api/webhooks/topics', async route => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([{ topic: 'items', count: 1 }]),
      });
    });

    await page.route('**/api/webhooks?**', async route => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          topic: 'items',
          events: [],
          pagination: { limit: 100, total: 0, mode: 'offset', next_cursor: null, offset: 0 },
        }),
      });
    });

    await page.goto('/');
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
      document.dispatchEvent(new Event('visibilitychange'));
    });

    await expect(page.getByTestId('polling-paused-badge')).toHaveText(/Polling pausado/i);
  });

  test('preserves advanced filter semantics with field and negation tokens', async ({ page }) => {
    await page.route('**/api/webhooks/topics', async route => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([{ topic: 'items', count: 2 }]),
      });
    });

    await page.route('**/api/webhooks?**', async route => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          topic: 'items',
          events: [
            {
              resource: '/items/MLA1',
              received_at: '2026-04-10 15:00:00',
              db_preview: { title: 'Producto Acme', price: 10, currency_id: 'ARS', brand: 'Acme', status: 'winning' },
            },
            {
              resource: '/items/MLA2',
              received_at: '2026-04-10 15:01:00',
              db_preview: { title: 'Producto Rival', price: 20, currency_id: 'ARS', brand: 'Rival', status: 'competing' },
            },
          ],
          pagination: { limit: 100, total: 2, mode: 'offset', next_cursor: null, offset: 0 },
        }),
      });
    });

    await page.goto('/');
    await page.getByTestId('resource-filter-input').fill('brand:acme -status:competing');

    await expect(page.getByTestId('preview-title-/items/MLA1')).toHaveText('Producto Acme');
    await expect(page.getByTestId('preview-title-/items/MLA2')).toHaveCount(0);
  });
});
