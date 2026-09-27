import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
const marker = 'home-agent correlated background routing v1';
export function patchClaude(src, modulePath) {
  if (src.includes(marker)) return src;
  const old = `            // weixin sequential prompt replay patch: this bridge processes messages one at a time.
            // Claude Code may stream output before replaying the user message, so do not let
            // claude-agent-acp classify the current turn as a background task.
            promptReplayed = true;
`;
  src = src.replace(old, '');
  const before = `        this.sessions[sessionId] = {
            query: q,
            input: input,`;
  if (!src.includes(before)) throw new Error('Unsupported Claude ACP session layout');
  src = `import { createTurnRouter } from ${JSON.stringify(modulePath)};\n` + src;
  return src.replace(before, `        // ${marker}
        const routing = createTurnRouter(q, input, {
            sessionId,
            onBackground: async (event) => this.client.sessionUpdate({
                sessionId,
                update: { sessionUpdate: "agent_message_chunk", content: { type: "text", text: event.text } },
                _meta: { homeAgentBackground: { id: event.id, isError: event.isError } },
            }),
        });
        this.sessions[sessionId] = {
            query: routing.query,
            input: routing.input,`);
}
export function patchWeixin(src) {
  if (src.includes(marker)) return src;
  const before = '\t\t\tsessionUpdate: async (params) => {\n\t\t\t\tconst update = params.update;';
  if (!src.includes(before)) throw new Error('Unsupported WeChat ACP sessionUpdate layout');
  return src.replace(before, `\t\t\tsessionUpdate: async (params) => {
                // ${marker}
                if (params._meta?.homeAgentBackground) {
                    const event = { ...params._meta.homeAgentBackground, sessionId: params.sessionId, text: params.update?.content?.text || "" };
                    if (this.options.onBackgroundResponse) {
                        Promise.resolve().then(() => this.options.onBackgroundResponse(event)).catch(error => log$1("background delivery failed: " + error.message));
                    }
                    return;
                }
\t\t\t\tconst update = params.update;`);
}
export function apply(root) {
  const packages = [path.join(root, 'node_modules')];
  const npx = '/Users/zhen/.npm/_npx';
  if (fs.existsSync(npx)) for (const name of fs.readdirSync(npx)) packages.push(path.join(npx, name, 'node_modules'));
  packages.push('/opt/homebrew/lib/node_modules');
  let count = 0;
  for (const dir of packages) {
    const targets = [{file: path.join(dir, '@zed-industries/claude-agent-acp/dist/acp-agent.js'), transform: s => patchClaude(s, path.join(root, 'scripts/acp-turn-router.mjs'))}];
    const wx = path.join(dir, 'weixin-acp/dist');
    if (fs.existsSync(wx)) for (const name of fs.readdirSync(wx).filter(n => /^acp-agent-.*\.mjs$/.test(n))) targets.push({file: path.join(wx,name),transform:patchWeixin});
    for (const {file,transform} of targets) {
      if (!fs.existsSync(file)) continue;
      const before = fs.readFileSync(file, 'utf8'), after = transform(before);
      if (after === before) continue;
      if (process.env.HOME_AGENT_PATCH_BACKUP_DIR) {
        const dest=process.env.HOME_AGENT_PATCH_BACKUP_DIR;
        fs.mkdirSync(dest,{recursive:true,mode:0o700});
        const name=crypto.createHash('sha256').update(file).digest('hex').slice(0,16)+'-'+path.basename(file);
        fs.writeFileSync(path.join(dest,name),before,{flag:'wx',mode:0o600});
        fs.appendFileSync(path.join(dest,'targets.jsonl'),JSON.stringify({file,backup:name})+'\n',{mode:0o600});
      }
      fs.writeFileSync(file,after); count++;
    }
  }
  console.log('[patch] correlated turn routing: '+count+' bundles updated');
}
if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) apply(process.env.HOME_AGENT_RUNTIME_ROOT || '/Users/zhen/home-agent/weixin-agent');
