/* Read-only spectator calls on the official client's existing authenticated socket.
 * Never reads login payloads or storage. Application request IDs are tracked only
 * to prevent collisions; spectator replies are hidden from the game client.
 */
(() => {
    if (window.__pupulinSpectator) return;
    const NativeSocket = window.WebSocket;
    const sockets = [];
    const filters = new Set([208, 209, 211, 212, 215, 216, 221, 222, 223, 224, 225, 226]);
    const bytes = data => data instanceof ArrayBuffer ? new Uint8Array(data) :
        ArrayBuffer.isView(data) ? new Uint8Array(data.buffer, data.byteOffset, data.byteLength) : null;
    function integer(n) {
        const output = [];
        do { output.push((n & 127) | (n > 127 ? 128 : 0)); n >>>= 7; } while (n);
        return output;
    }
    const field = (id, value) => [...integer(id * 8 + 2), ...integer(value.length), ...value];
    function WrappedSocket(...args) {
        const socket = new NativeSocket(...args);
        let url;
        try { url = new URL(String(args[0]), location.href); } catch (_) { return socket; }
        if (!url.hostname.endsWith('.maj-soul.com') || url.pathname !== '/gateway') return socket;
        const state = {socket, pending: new Set(), reserved: new Set(), own: new Map(), remap: new Map(), cursor: 65535, forwarded: new WeakSet()};
        sockets.push(state);
        function allocate() {
            for (let i = 0; i < 65535; ++i) {
                const id = state.cursor;
                state.cursor = id <= 1 ? 65535 : id - 1;
                if (!state.pending.has(id) && !state.reserved.has(id)) return id;
            }
            throw new Error('request_ids_exhausted');
        }
        state.allocate = allocate;
        const nativeSend = socket.send.bind(socket);
        state.send = nativeSend;
        socket.send = function(data) {
            const frame = bytes(data);
            if (frame && frame.length >= 3 && frame[0] === 2) {
                let id = frame[1] | frame[2] << 8;
                if (state.reserved.has(id)) {
                    const original = id;
                    id = allocate();
                    state.remap.set(id, original);
                    data = new Uint8Array(frame);
                    data[1] = id & 255; data[2] = id >> 8;
                }
                state.pending.add(id);
            }
            return nativeSend(data);
        };
        socket.addEventListener('message', event => {
            if (state.forwarded.has(event)) return;
            const frame = bytes(event.data);
            if (!frame || frame.length < 3 || frame[0] !== 3) return;
            const id = frame[1] | frame[2] << 8;
            if (state.reserved.has(id)) {
                event.stopImmediatePropagation();
                const call = state.own.get(id);
                if (call) {
                    clearTimeout(call.timer); state.own.delete(id);
                    call.resolve({wire: Array.from(frame.slice(3))});
                }
                return;
            }
            state.pending.delete(id);
            if (state.remap.has(id)) {
                event.stopImmediatePropagation();
                const original = state.remap.get(id);
                state.remap.delete(id);
                const mapped = new Uint8Array(frame);
                mapped[1] = original & 255; mapped[2] = original >> 8;
                const forwarded = new MessageEvent('message', {data: mapped.buffer});
                state.forwarded.add(forwarded);
                socket.dispatchEvent(forwarded);
            }
        });
        socket.addEventListener('close', () => {
            for (const call of state.own.values()) {
                clearTimeout(call.timer); call.resolve({error: 'not_connected'});
            }
            state.own.clear();
        });
        return socket;
    }
    WrappedSocket.prototype = NativeSocket.prototype;
    Object.setPrototypeOf(WrappedSocket, NativeSocket);
    window.WebSocket = WrappedSocket;
    Object.defineProperty(window, '__pupulinSpectator', {value: {
        async list(filter) {
            if (!filters.has(filter)) return {error: 'invalid_filter'};
            const state = [...sockets].reverse().find(s => s.socket.readyState === NativeSocket.OPEN);
            if (!state) return {error: 'not_connected'};
            if (state.socket.binaryType !== 'arraybuffer') return {error: 'unsupported_transport'};
            let id;
            try { id = state.allocate(); } catch (_) { return {error: 'request_ids_exhausted'}; }
            state.reserved.add(id);
            const method = new TextEncoder().encode('.lq.Lobby.fetchGameLiveList');
            const payload = [8, ...integer(filter)];
            const request = new Uint8Array([2, id & 255, id >> 8, ...field(1, method), ...field(2, payload)]);
            return new Promise(resolve => {
                const timer = setTimeout(() => {
                    state.own.delete(id); resolve({error: 'rpc_timeout'});
                }, 8000);
                state.own.set(id, {resolve, timer});
                try { state.send(request); }
                catch (_) { clearTimeout(timer); state.own.delete(id); resolve({error: 'not_connected'}); }
            });
        },
        status() { return {sockets: sockets.length, connected: sockets.some(s => s.socket.readyState === NativeSocket.OPEN)}; }
    }});
})();
