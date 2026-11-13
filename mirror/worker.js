/**
 * L4D2 工坊镜像 · Cloudflare Worker
 *
 * 作用：把 Steam 的 CDN / 工坊页面套一层自己的域名，解决
 *      「cdn.steamusercontent.com 能连但慢」「steamcommunity.com 打不开」。
 *
 * 用法：
 *   GET /file?url=<encodeURIComponent(直链)>
 *   可选：部署时设置环境变量 MIRROR_TOKEN，请求需带 &token=xxx
 *
 * 只放行 Steam 的域名，避免变成公开代理。
 */

const ALLOWED_HOSTS = new Set([
  "cdn.steamusercontent.com",
  "images.steamusercontent.com",
  "steamusercontent-a.akamaihd.net",
  "steamcommunity.com",
  "api.steampowered.com",
]);

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function cors(headers = {}) {
  return {
    ...headers,
    "access-control-allow-origin": "*",
    "access-control-allow-headers": "range, if-range, content-type",
    "access-control-expose-headers": "content-length, content-range, accept-ranges",
  };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      return json({ ok: true, service: "l4d2-workshop-mirror" });
    }

    if (url.pathname !== "/file") {
      return json({ error: "not found", usage: "/file?url=<encoded steam url>" }, 404);
    }

    if (env.MIRROR_TOKEN && url.searchParams.get("token") !== env.MIRROR_TOKEN) {
      return json({ error: "forbidden" }, 403);
    }

    let target;
    try {
      target = new URL(url.searchParams.get("url") || "");
    } catch {
      return json({ error: "invalid url" }, 400);
    }

    if (!ALLOWED_HOSTS.has(target.hostname)) {
      return json({ error: `host not allowed: ${target.hostname}` }, 403);
    }

    const headers = new Headers();
    const range = request.headers.get("range");
    const ifRange = request.headers.get("if-range");
    if (range) headers.set("range", range);
    if (ifRange) headers.set("if-range", ifRange);
    headers.set("user-agent", "l4d2-workshop-mirror/1.0");

    const upstream = await fetch(target.toString(), {
      method: request.method === "HEAD" ? "HEAD" : "GET",
      headers,
      redirect: "follow",
    });

    const out = new Headers(upstream.headers);
    out.delete("content-security-policy");
    out.delete("content-encoding");
    const final = cors(Object.fromEntries(out.entries()));

    return new Response(request.method === "HEAD" ? null : upstream.body, {
      status: upstream.status,
      headers: final,
    });
  },
};
