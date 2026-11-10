// A minimal RFC 6455 server endpoint: text messages, ping/pong and close. Enough for one loopback
// page; not a general WebSocket server.

import { createHash } from "node:crypto";

const GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
const MAX_MESSAGE_BYTES = 16 * 1024 * 1024;

/**
 * Complete the upgrade handshake on `socket` and return `{ send(text), close() }`. `onMessage`
 * receives each text message; `onClose` runs once when the connection ends for any reason.
 */
export function acceptWebSocket(request, socket, { onMessage, onClose }) {
  const key = request.headers["sec-websocket-key"];
  const accept = createHash("sha1").update(key + GUID).digest("base64");
  socket.write(
    "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
    + `Sec-WebSocket-Accept: ${accept}\r\n\r\n`,
  );
  socket.setNoDelay(true);

  let buffer = Buffer.alloc(0);
  let fragments = [];
  let fragmentBytes = 0;
  let closed = false;

  const finish = () => {
    if (closed) return;
    closed = true;
    socket.destroy();
    onClose();
  };

  const frame = (opcode, payload) => {
    const length = payload.length;
    let header;
    if (length < 126) {
      header = Buffer.from([0x80 | opcode, length]);
    } else if (length < 65536) {
      header = Buffer.alloc(4);
      header[0] = 0x80 | opcode;
      header[1] = 126;
      header.writeUInt16BE(length, 2);
    } else {
      header = Buffer.alloc(10);
      header[0] = 0x80 | opcode;
      header[1] = 127;
      header.writeBigUInt64BE(BigInt(length), 2);
    }
    socket.write(Buffer.concat([header, payload]));
  };

  const fail = (code) => {
    if (closed) return;
    const payload = Buffer.alloc(2);
    payload.writeUInt16BE(code, 0);
    frame(0x8, payload);
    socket.end();
    finish();
  };

  const consume = () => {
    while (!closed) {
      if (buffer.length < 2) return;
      const fin = (buffer[0] & 0x80) !== 0;
      const opcode = buffer[0] & 0x0f;
      const masked = (buffer[1] & 0x80) !== 0;
      let length = buffer[1] & 0x7f;
      let offset = 2;
      if (length === 126) {
        if (buffer.length < 4) return;
        length = buffer.readUInt16BE(2);
        offset = 4;
      } else if (length === 127) {
        if (buffer.length < 10) return;
        const big = buffer.readBigUInt64BE(2);
        if (big > BigInt(MAX_MESSAGE_BYTES)) return fail(1009);
        length = Number(big);
        offset = 10;
      }
      if (!masked) return fail(1002);
      if (length > MAX_MESSAGE_BYTES) return fail(1009);
      if (buffer.length < offset + 4 + length) return;
      const mask = buffer.subarray(offset, offset + 4);
      const payload = Buffer.from(buffer.subarray(offset + 4, offset + 4 + length));
      for (let i = 0; i < payload.length; i += 1) payload[i] ^= mask[i & 3];
      buffer = buffer.subarray(offset + 4 + length);

      if (opcode === 0x8) {
        frame(0x8, payload.subarray(0, 2));
        socket.end();
        finish();
        return;
      }
      if (opcode === 0x9) {
        frame(0xa, payload);
        continue;
      }
      if (opcode === 0xa) continue;
      if (opcode === 0x2) return fail(1003);
      if (opcode !== 0x1 && opcode !== 0x0) return fail(1002);
      if ((opcode === 0x1) !== (fragments.length === 0)) return fail(1002);
      fragments.push(payload);
      fragmentBytes += payload.length;
      if (fragmentBytes > MAX_MESSAGE_BYTES) return fail(1009);
      if (fin) {
        const text = Buffer.concat(fragments).toString("utf8");
        fragments = [];
        fragmentBytes = 0;
        onMessage(text);
      }
    }
  };

  socket.on("data", (chunk) => {
    buffer = Buffer.concat([buffer, chunk]);
    consume();
  });
  socket.on("close", finish);
  socket.on("error", finish);

  return {
    send(text) {
      if (!closed) frame(0x1, Buffer.from(text, "utf8"));
    },
    close() {
      fail(1000);
    },
  };
}
