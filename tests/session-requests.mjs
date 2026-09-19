import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {test} from 'node:test';

const source = readFileSync(new URL('../web/session.js', import.meta.url), 'utf8');
function client(fetch, locks) {
  const context = vm.createContext({fetch, navigator: {locks}, AbortController, setTimeout, clearTimeout});
  vm.runInContext(source, context);
  return context.sessionFetch;
}

test('a late poll finishes before a command reads and updates the session', async () => {
  let cookie = 'before';
  let release;
  const wait = new Promise(resolve => { release = resolve; });
  const seen = [];
  const request = client(async url => {
    seen.push([url, cookie]);
    if (url === 'poll') await wait;
    cookie = url;
    return new Response('{}');
  });
  const poll = request('poll');
  const command = request('pause');
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(seen, [['poll', 'before']]);
  release();
  await Promise.all([poll, command]);
  assert.deepEqual(seen, [['poll', 'before'], ['pause', 'poll']]);
  assert.equal(cookie, 'pause');
});

test('a failed connection does not block retry or reset', async () => {
  const request = client(async url => {
    if (url === 'fail') throw new Error('offline');
    return new Response('{"reset":true}');
  });
  await assert.rejects(request('fail'), /offline/);
  assert.deepEqual(await (await request('reset')).json(), {reset: true});
});

test('tabs share one session lock', async () => {
  let tail = Promise.resolve();
  let active = 0;
  const locks = {request(name, send) {
    assert.equal(name, 'loadshift-session');
    const pending = tail.then(send);
    tail = pending.catch(() => {});
    return pending;
  }};
  const fetch = async () => {
    assert.equal(++active, 1);
    await new Promise(resolve => setImmediate(resolve));
    active--;
    return new Response('{}');
  };
  await Promise.all([client(fetch, locks)('poll'), client(fetch, locks)('reset')]);
});
