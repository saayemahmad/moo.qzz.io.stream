// Buzz Pals - zero-dependency live server (Node 16+)
// Run: node server.js   then open http://<your-ip>:3000 on both phones

const http = require('http');
const fs = require('fs');
const path = require('path');
const os = require('os');

const PORT = process.env.PORT || 3000;
const rooms = new Map(); // room -> Map(clientId -> res)

function roomOf(name) {
  if (!rooms.has(name)) rooms.set(name, new Map());
  return rooms.get(name);
}

function sse(res, event, data) {
  res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
}

function broadcastPresence(name) {
  const room = rooms.get(name);
  if (!room) return;
  for (const res of room.values()) sse(res, 'presence', { count: room.size });
}

function cleanRoom(s) {
  return String(s || 'fun').toLowerCase().replace(/[^a-z0-9_-]/g, '').slice(0, 24) || 'fun';
}

function validPattern(p) {
  return (
    Array.isArray(p) &&
    p.length > 0 &&
    p.length <= 40 &&
    p.every((n) => Number.isFinite(n) && n >= 0 && n <= 10000)
  );
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);

  // --- page ---
  if (req.method === 'GET' && (url.pathname === '/' || url.pathname === '/index.html')) {
    fs.readFile(path.join(__dirname, 'index.html'), (err, buf) => {
      if (err) {
        res.writeHead(500);
        return res.end('index.html not found next to server.js');
      }
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(buf);
    });
    return;
  }

  // --- live stream ---
  if (req.method === 'GET' && url.pathname === '/events') {
    const name = cleanRoom(url.searchParams.get('room'));
    const id = String(url.searchParams.get('id') || '').slice(0, 40);
    if (!id) {
      res.writeHead(400);
      return res.end('missing id');
    }
    res.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache, no-transform',
      Connection: 'keep-alive',
      'X-Accel-Buffering': 'no',
    });
    res.write('retry: 1500\n\n');

    const room = roomOf(name);
    const old = room.get(id);
    if (old) old.end();
    room.set(id, res);
    broadcastPresence(name);

    const ping = setInterval(() => res.write(': ping\n\n'), 20000);
    req.on('close', () => {
      clearInterval(ping);
      if (room.get(id) === res) room.delete(id);
      if (room.size === 0) rooms.delete(name);
      else broadcastPresence(name);
    });
    return;
  }

  // --- send a vibe to everyone else in the room ---
  if (req.method === 'POST' && url.pathname === '/send') {
    let body = '';
    req.on('data', (c) => {
      body += c;
      if (body.length > 4096) req.destroy();
    });
    req.on('end', () => {
      let msg;
      try {
        msg = JSON.parse(body);
      } catch {
        res.writeHead(400);
        return res.end('bad json');
      }
      const name = cleanRoom(msg.room);
      const room = rooms.get(name);
      const type = msg.type === 'stop' ? 'stop' : 'vibe';
      if (type === 'vibe' && !validPattern(msg.pattern)) {
        res.writeHead(400);
        return res.end('bad pattern');
      }
      let delivered = 0;
      if (room) {
        for (const [id, peer] of room) {
          if (id === msg.id) continue;
          sse(peer, type, {
            pattern: msg.pattern,
            label: String(msg.label || '').slice(0, 20),
            hold: msg.hold === true,
          });
          delivered++;
        }
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ delivered }));
    });
    return;
  }

  res.writeHead(404);
  res.end('not found');
});

server.listen(PORT, '0.0.0.0', () => {
  console.log(`\nBuzz Pals running on port ${PORT}\n`);
  for (const list of Object.values(os.networkInterfaces())) {
    for (const i of list || []) {
      if (i.family === 'IPv4' && !i.internal) {
        console.log(`  Phone-e kholo:  http://${i.address}:${PORT}`);
      }
    }
  }
  console.log('\n(duita phone-i ek-i WiFi-te thakte hobe)\n');
});
