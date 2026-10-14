import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests/ui', fullyParallel: true, timeout: 20000,
  expect: { timeout: 5000 }, reporter: 'list',
  outputDir: '../../.playwright-mcp/workspace-tests',
  use: { baseURL: 'http://127.0.0.1:5182', viewport: { width: 1440, height: 1000 },
    launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }, screenshot: 'only-on-failure' },
  webServer: { command: 'node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5182 --strictPort',
    url: 'http://127.0.0.1:5182', reuseExistingServer: !process.env.CI },
});
