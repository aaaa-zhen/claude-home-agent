#!/usr/bin/env node
import { openVisible } from './browser-bridge-lib.mjs';

const url = process.argv[2] || 'https://preply.com/en/lessons';

try {
  const result = await openVisible(url);
  process.stdout.write(`${JSON.stringify({ ok: true, action: 'open', data: result }, null, 2)}\n`);
} catch (error) {
  process.stdout.write(`${JSON.stringify({ ok: false, error: error.message }, null, 2)}\n`);
  process.exitCode = 1;
}
