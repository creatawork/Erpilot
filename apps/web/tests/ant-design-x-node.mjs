// Avoid eagerly loading the browser-only Mermaid dependency from the CommonJS barrel.
// This is the genuine Bubble implementation, not a component mock.
import { createRequire } from 'node:module';
export const Bubble = createRequire(import.meta.url)('@ant-design/x/lib/bubble/index.js').default;
