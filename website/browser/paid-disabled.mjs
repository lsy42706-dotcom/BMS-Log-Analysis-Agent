/** Build-time paid-provider boundary for the browser bundle. Never imports an API client. */
export async function runAgent() { throw new Error('Paid inference is not available in the local bundle'); }
