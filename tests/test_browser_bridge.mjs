import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { safeUrl, validateWebUrl } from '../scripts/browser-bridge-lib.mjs';

test('allows http and https URLs', () => {
  assert.equal(validateWebUrl('https://example.com/a?x=1').hostname, 'example.com');
  assert.equal(validateWebUrl('http://example.com/').protocol, 'http:');
});

test('blocks executable, local-file, and credential URLs', () => {
  assert.throws(() => validateWebUrl('javascript:alert(1)'), /Blocked URL scheme/);
  assert.throws(() => validateWebUrl('file:///tmp/private'), /Blocked URL scheme/);
  assert.throws(() => validateWebUrl('https://user:secret@example.com/'), /embedded credentials/);
});

test('redacts query parameters and fragments from output', () => {
  assert.equal(
    safeUrl('https://example.com/account?token=secret&view=full#section'),
    'https://example.com/account',
  );
});

test('generic bridge exposes no arbitrary action and site writes require apply', () => {
  const bridge = readFileSync(new URL('../scripts/browser-bridge.mjs', import.meta.url), 'utf8');
  const preply = readFileSync(new URL('../scripts/agent-browser-preply.mjs', import.meta.url), 'utf8');
  assert.doesNotMatch(bridge, /case ['"](?:click|eval|fill|cookies?)['"]/);
  assert.match(preply, /if \(!args\.apply \|\| args\['dry-run'\]\)/);
  assert.match(preply, /requires_apply: true/);
});
