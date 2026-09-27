// Inbound credential detection for the WeChat bridge (2026-09-22).
//
// The user types things like「XX网站 账号 abc 密码 123」. This module finds the
// password (and account, if any), proposes a vault name, and returns the text
// with the password replaced so nothing downstream ever sees it. It is a
// heuristic by design: a miss means the old behaviour (plaintext, no worse than
// before); a false hit stores something harmless that the user can delete.

// 「密码箱」is the vault itself, never a credential.
const PASSWORD_KEY = /((?:密码(?!箱)|口令|令牌)|(?<![A-Za-z_])(?:password|passwd|pwd|passcode|api[_-]?key|token|secret)(?![A-Za-z_-]))/i;
const ACCOUNT_KEY = /((?:账号|帐号|账户|用户名|用户|邮箱|手机号|手机)|(?<![A-Za-z_])(?:account|username|user|login)(?![A-Za-z_-]))/i;
const SEPARATOR = /(?:是|为|[:=：])?/;
const VALUE = /([^\s,，。;；、!！?？（）()【】\[\]「」]+)/;
// A credential value has to contain at least one ASCII letter or digit; pure
// Chinese after 密码 (密码错了 / 密码忘了 / 密码是什么) is conversation, not a secret.
const LOOKS_LIKE_VALUE = /[A-Za-z0-9]/;
const NOT_VALUE = /^(?:是什么|多少|错了|不对|忘了|忘记|改了|呢|吗|啥|什么|怎么|如何|重置|修改|已存入|已移入)/;

const PASSWORD_RE = new RegExp(PASSWORD_KEY.source + '\\s*' + SEPARATOR.source + '\\s*' + VALUE.source, 'i');
const ACCOUNT_RE = new RegExp(ACCOUNT_KEY.source + '\\s*' + SEPARATOR.source + '\\s*' + VALUE.source, 'i');

// Words that describe the request rather than name the site.
const NAME_NOISE = /(帮我|请|麻烦|记一下|记下|记住|记录|保存|存一下|存下|存好|一下|这个|那个|我的|的|是|登录|登陆|账号|帐号|账户|用户名|密码|口令|网站|网址|平台|app|APP|软件|系统|和|与|及|,|，|。|：|:|、|\s)+/g;

function cleanValue(raw) {
  return String(raw ?? '').replace(/^[`"'“”‘’]+/u, '').replace(/[`"'“”‘’]+$/u, '').replace(/[。.,，;；!！?？]+$/u, '');
}

// A credential starts with a letter or digit and is mostly ASCII; prose such
// as「主卧空调25度」or markdown fragments like「>…」/「-…」are not credentials.
function isCredential(value) {
  if (!value || value.length < 4 || value.length > 128) return false;
  if (!/^[A-Za-z0-9]/.test(value) || !LOOKS_LIKE_VALUE.test(value) || NOT_VALUE.test(value)) return false;
  const ascii = value.replace(/[^\x21-\x7e]/g, '').length;
  return ascii / value.length >= 0.6;
}

export function proposeName(text, passwordIndex, account, now = new Date()) {
  const head = String(text).slice(0, passwordIndex);
  const cut = head.search(ACCOUNT_KEY);
  const candidate = (cut >= 0 ? head.slice(0, cut) : head).replace(NAME_NOISE, ' ').trim().split(/\s+/).filter(Boolean).pop() || '';
  const name = candidate.replace(/[^\p{L}\p{N}._-]/gu, '').slice(0, 32);
  if (name) return name;
  if (account && /^[\w.@+-]+$/.test(account)) return account.slice(0, 32);
  const pad = n => String(n).padStart(2, '0');
  return `secret-${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}`;
}

// Returns null when the text carries no credential. Otherwise:
// {name, user, password, redactedText, keyword}
export function detectSecret(text, {now = new Date()} = {}) {
  const source = String(text ?? '');
  if (!source.trim()) return null;
  const match = PASSWORD_RE.exec(source);
  if (!match) return null;
  const password = cleanValue(match[2]);
  if (!isCredential(password)) return null;
  // Token-ish keywords need something that looks like a token: long or with digits,
  // not the next English word in a sentence (「token launchctl」).
  if (/^(token|令牌|secret|api[_-]?key)$/i.test(match[1]) && (password.length < 8 || !(/\d/.test(password) || password.length >= 20))) return null;

  // First account-like value that is not prose:「账号：手机号 `155…`」skips「手机号」and takes the number.
  let user = '';
  const accountRe = new RegExp(ACCOUNT_RE.source, 'gi');
  for (let am; (am = accountRe.exec(source));) {
    let candidate = cleanValue(am[2]);
    // 「账号abc123密码xxx」without spaces: stop the account at the password keyword.
    const overlap = candidate.search(PASSWORD_KEY);
    if (overlap > 0) candidate = candidate.slice(0, overlap);
    if (overlap === 0 || candidate === password || !LOOKS_LIKE_VALUE.test(candidate)) { accountRe.lastIndex = am.index + am[1].length; continue; }
    user = candidate;
    break;
  }
  const name = proposeName(source, match.index, user, now);

  const valueStart = match.index + match[0].length - match[2].length;
  const redactedText = source.slice(0, valueStart) + `[已存入密码箱：${name}]` + source.slice(valueStart + match[2].length);
  return {name, user, password, redactedText, keyword: match[1]};
}

export function redactedPlaceholder(name) {
  return `[已存入密码箱：${name}]`;
}
