import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

// Separate delivery receipts: never pair background output with a user turn.
export class BackgroundReplies {
  constructor({directory, send, record = () => {}}) {
    this.directory = directory; this.send = send; this.record = record;
    this.tail = Promise.resolve();
    fs.mkdirSync(directory, {recursive: true, mode: 0o700});
  }
  save(file, item) {
    const tmp = file + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(item), {mode: 0o600});
    fs.renameSync(tmp, file);
  }
  enqueue(event) {
    if (!event?.id || !event?.sessionId || typeof event.text !== 'string' || !event.text.trim()) throw new Error('Invalid background response');
    const id = crypto.createHash('sha256').update(event.sessionId + ':' + event.id).digest('hex');
    const file = path.join(this.directory, id + '.json');
    if (fs.existsSync(file)) return this.tail;
    const item = {...event, status: 'pending', receivedAt: new Date().toISOString()};
    this.save(file, item);
    const run = async () => {
      item.status = 'unknown'; this.save(file, item);
      try { await this.send('后台任务更新：\n' + item.text, {notification: {
        id: 'sdk-background:' + item.sessionId + ':' + item.id,
        source: 'sdk-background',
        ...(item.taskId ? {taskId: item.taskId} : {}),
      }}); }
      catch (error) {
        item.status = error.deliveryUnknown ? 'unknown' : 'rejected';
        this.save(file, item);
        return;
      }
      item.status = 'accepted'; item.acceptedAt = new Date().toISOString(); this.save(file, item);
      this.record(item);
    };
    this.tail = this.tail.then(run, run).catch(error => console.error('[background-replies] receipt failed:', error.message));
    return this.tail;
  }
}
