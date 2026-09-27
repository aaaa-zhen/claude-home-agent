import test from 'node:test';
import assert from 'node:assert/strict';
import {validateSendResponse} from '../weixin-send.mjs';
test('HTTP 200 business rejection must fail', () => {
  assert.throws(() => validateSendResponse(200, '{"ret":-14,"errmsg":"expired"}'), /rejected/);
  assert.throws(() => validateSendResponse(200, '{"errcode":401}'), /rejected/);
});
test('accepted response is distinguished from HTTP failure and unknown body', () => {
  assert.deepEqual(validateSendResponse(200, '{"ret":0}'), {ret: 0});
  assert.throws(() => validateSendResponse(500, '{}'), /HTTP 500/);
  assert.throws(() => validateSendResponse(200, '<html>'), e => e.deliveryUnknown === true);
});
