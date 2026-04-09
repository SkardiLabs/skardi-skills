// Next.js catch-all Route Handler: strips Origin/Referer so Skardi's CSRF
// middleware sees no cross-origin header and allows the request.
//
// Place at: src/app/api/skardi/[...path]/route.ts
//
// Env vars:
//   SKARDI_UPSTREAM_URL   — server-side: K8s service URL or localhost (never exposed to browser)
//   NEXT_PUBLIC_SKARDI_URL — browser-side: this app's own domain + /api/skardi
//
// Usage in client code:
//   const res = await fetch(`${process.env.NEXT_PUBLIC_SKARDI_URL}/${pipeline}/execute`, {
//     method: 'POST',
//     headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
//     body: JSON.stringify(params),
//   });

import { NextRequest, NextResponse } from 'next/server';

const SKARDI_URL = process.env.SKARDI_UPSTREAM_URL ?? 'http://localhost:18080';
const STRIP = new Set(['origin', 'referer', 'host']);

async function proxy(req: NextRequest, path: string[]): Promise<NextResponse> {
  const upstream = `${SKARDI_URL}/${path.join('/')}`;
  const headers = new Headers();
  req.headers.forEach((v, k) => {
    if (!STRIP.has(k.toLowerCase())) headers.set(k, v);
  });
  const body = req.method === 'GET' || req.method === 'HEAD' ? undefined : req.body;
  const res = await fetch(upstream, {
    method: req.method,
    headers,
    body,
    duplex: 'half',
  } as RequestInit);
  return new NextResponse(res.body, { status: res.status, headers: res.headers });
}

// Next.js 14: params is a plain object. Next.js 15+: params is a Promise — use params.then(p => proxy(req, p.path))
type Ctx = { params: { path: string[] } };
const handle = (req: NextRequest, { params }: Ctx) => proxy(req, params.path);

export const GET     = handle;
export const POST    = handle;
export const PUT     = handle;
export const DELETE  = handle;
export const OPTIONS = handle;
