// Photon relay (ARCHITECTURE.md §9.1). Holds no state and makes no decisions:
// inbound iMessage events → POST to the backend; backend → /send* → iMessage.
import { Spectrum, UnsupportedError, poll, type Message, type Space } from "spectrum-ts";
import { imessage } from "spectrum-ts/providers/imessage";

const PROJECT_ID = process.env.PHOTON_PROJECT_ID ?? "";
const PROJECT_SECRET = process.env.PHOTON_PROJECT_SECRET ?? "";
const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";
const PORT = Number(process.env.BRIDGE_PORT ?? 3001);

if (!PROJECT_ID || !PROJECT_SECRET) {
  console.error("PHOTON_PROJECT_ID and PHOTON_PROJECT_SECRET must be set in bridge/.env");
  process.exit(1);
}

// Never log full handles or chat ids.
const tail = (s: string | undefined) => (s ? `…${s.slice(-4)}` : "?");

const app = await Spectrum({
  projectId: PROJECT_ID,
  projectSecret: PROJECT_SECRET,
  providers: [imessage.config()],
});
const im = imessage(app);
console.log("[bridge] connected to Photon Spectrum");

async function postToBackend(path: string, body: unknown): Promise<void> {
  try {
    const res = await fetch(`${BACKEND_URL}${path}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) console.warn(`[bridge] backend ${path} → HTTP ${res.status}`);
  } catch {
    console.warn(`[bridge] backend unreachable, dropped ${path}`);
  }
}

async function handleInbound(space: Space, message: Message): Promise<void> {
  if (message.direction === "outbound" || message.sender?.kind === "agent") return;

  const isGroup = imessage(space).type === "group";
  const handle = message.sender?.id ?? "";
  const content = message.content;
  const where = isGroup ? `group ${tail(space.id)}` : "dm";
  console.log(`[in] ${where} from ${tail(handle)} type=${content.type}`);

  if (content.type === "text") {
    await postToBackend("/webhooks/photon/message", {
      message_id: message.id,
      chat_id: space.id,
      is_group: isGroup,
      sender_handle: handle,
      text: content.text,
      ts: message.timestamp.toISOString(),
    });
    return;
  }

  if (content.type === "poll_option") {
    if (!content.selected) return; // un-votes are ignored; the backend upserts one vote per user
    await postToBackend("/webhooks/photon/poll_vote", {
      // Inbound vote ids are "<pollMessageGuid>:<sender>:..."; the guid matches /send_poll's poll_id.
      poll_id: message.id.split(":")[0],
      chat_id: space.id,
      voter_handle: handle,
      option_index: content.poll.options.findIndex((o) => o.title === content.option.title),
    });
    return;
  }

  if (content.type === "attachment") {
    console.log(`[in] attachment mime=${content.mimeType} name=${content.name}`);
  }
}

// Inbound loop. One bad message must never stop the stream.
(async () => {
  for await (const [space, message] of app.messages) {
    try {
      await handleInbound(space, message);
    } catch (err) {
      console.error("[bridge] inbound handling failed:", err);
    }
  }
})();

const json = (body: unknown, status = 200) => Response.json(body, { status });

Bun.serve({
  port: PORT,
  async fetch(req) {
    const path = new URL(req.url).pathname;
    if (req.method === "GET" && path === "/health") return json({ ok: true });
    if (req.method !== "POST") return json({ error: "not found" }, 404);

    try {
      const body = (await req.json()) as Record<string, any>;

      if (path === "/send") {
        const space = await im.space.get(body.chat_id);
        await space.send(body.text);
        console.log(`[out] group ${tail(body.chat_id)}`);
        return json({ ok: true });
      }

      if (path === "/send_dm") {
        const space = await im.space.create(body.handle);
        await space.send(body.text);
        console.log(`[out] dm ${tail(body.handle)}`);
        return json({ ok: true });
      }

      if (path === "/send_poll") {
        const space = await im.space.get(body.chat_id);
        const sent = await space.send(poll(body.title, body.options));
        console.log(`[out] poll ${tail(body.chat_id)} options=${body.options.length}`);
        return json({ poll_id: sent?.id ?? null });
      }

      if (path === "/send_image") {
        return json({ error: "send_image not implemented (stretch)" }, 501);
      }

      return json({ error: "not found" }, 404);
    } catch (err) {
      if (err instanceof UnsupportedError) {
        console.warn(`[out] ${path} unsupported: ${err.message}`);
        return json({ error: err.message }, 501);
      }
      console.error(`[out] ${path} failed:`, err);
      return json({ error: String(err) }, 502);
    }
  },
});
console.log(`[bridge] listening on http://localhost:${PORT}`);
