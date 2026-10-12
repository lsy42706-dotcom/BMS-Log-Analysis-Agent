import { handleRequest } from "../../../lib/http.mjs";
export const dynamic = "force-dynamic";
export async function POST(request: Request) { return handleRequest(request, "window"); }
export async function OPTIONS() { return new Response(null, {status:403}); }
