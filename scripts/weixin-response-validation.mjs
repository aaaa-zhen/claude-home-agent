// Shared by foreground SDK replies and proactive sends.
export function validateSendResponse(status, raw) {
  if (status < 200 || status >= 300) {
    const error = new Error(`sendmessage HTTP ${status}`);
    error.deliveryUnknown = status >= 500;
    throw error;
  }
  let body;
  try { body = JSON.parse(raw); }
  catch { const error = new Error('sendmessage response is not JSON; outcome unknown'); error.deliveryUnknown = true; throw error; }
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    const error = new Error('sendmessage invalid response; outcome unknown'); error.deliveryUnknown = true; throw error;
  }
  for (const key of ['ret', 'errcode', 'code']) {
    if (body[key] !== undefined && Number(body[key]) !== 0) throw new Error(`sendmessage rejected (${key}=${Number(body[key])})`);
  }
  if (body.ok === false || body.success === false) throw new Error('sendmessage explicitly rejected');
  return body;
}
