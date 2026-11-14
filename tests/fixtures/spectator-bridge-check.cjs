const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
class Socket extends EventTarget {
    static OPEN = 1;
    constructor(url) { super(); this.url = url; this.readyState = 1; this.binaryType = 'arraybuffer'; this.sent = []; }
    send(value) { this.sent.push(new Uint8Array(value)); }
}
global.window = global;
global.location = {href: 'https://game.maj-soul.com/1/'};
global.WebSocket = Socket;
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
(async () => {
    const s = new WebSocket('wss://route-2.maj-soul.com/gateway');
    const received = [];
    s.addEventListener('message', event => received.push([...new Uint8Array(event.data)]));
    const receive = packet => s.dispatchEvent(new MessageEvent('message', {data: new Uint8Array(packet).buffer}));
    const request = __pupulinSpectator.list(212);
    const own = s.sent.at(-1);
    const ownId = own[1] | own[2] << 8;
    assert.equal(own[0], 2);
    receive([3, own[1], own[2], 18, 0]);
    assert.deepEqual(await request, {wire: [18, 0]});
    assert.equal(received.length, 0, 'own reply must not reach official client');
    receive([3, own[1], own[2], 18, 0]);
    assert.equal(received.length, 0, 'late/duplicate own reply must remain hidden');
    const original = new Uint8Array([2, 7, 0, 18, 3, 97, 98, 99]);
    s.send(original);
    assert.deepEqual(s.sent.at(-1), original, 'ordinary application packets are untouched');
    receive([3, 7, 0, 18, 0]);
    assert.deepEqual(received.pop(), [3, 7, 0, 18, 0]);
    const collision = new Uint8Array([2, ownId & 255, ownId >> 8, 18, 3, 97, 98, 99]);
    s.send(collision);
    const mapped = s.sent.at(-1);
    assert.notEqual(mapped[1] | mapped[2] << 8, ownId);
    assert.deepEqual([...mapped.slice(3)], [...collision.slice(3)], 'only collision ID may change');
    receive([3, mapped[1], mapped[2], 18, 0]);
    assert.deepEqual(received.pop(), [3, ownId & 255, ownId >> 8, 18, 0], 'original application ID restored');
    receive([1, 12, 0, 42]);
    assert.deepEqual(received.pop(), [1, 12, 0, 42], 'notifications untouched');
    assert.deepEqual(await __pupulinSpectator.list(999), {error: 'invalid_filter'});
    const uuid = '261113-510ble96-8620-41e0-8a5d-68a63d8bf487';
    const observation = __pupulinSpectator.observe(uuid);
    const packet = s.sent.at(-1);
    assert.ok(Buffer.from(packet).includes(Buffer.from('.lq.Lobby.fetchOBToken')));
    assert.ok(Buffer.from(packet).includes(Buffer.from(uuid)));
    receive([3, packet[1], packet[2], 18, 0]);
    assert.deepEqual(await observation, {wire: [18, 0], route: 'wss://route-2.maj-soul.com'});
    assert.deepEqual(await __pupulinSpectator.observe('bad'), {error: 'invalid_uuid'});
    const pending = __pupulinSpectator.list(212);
    s.readyState = 3;
    s.dispatchEvent(new Event('close'));
    assert.deepEqual(await pending, {error: 'not_connected'});
    assert.deepEqual(await __pupulinSpectator.list(212), {error: 'not_connected'});
    console.log('spectator bridge multiplexing passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
