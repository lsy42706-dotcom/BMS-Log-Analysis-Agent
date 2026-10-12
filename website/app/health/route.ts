import { healthResponse } from "../../lib/http.mjs";
export const dynamic = "force-dynamic";
export function GET() { return healthResponse(); }
