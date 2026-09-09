import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const api = require("./client.cjs");

export const createClient = api.createClient;
export const reportError = api.reportError;
export const reportNetworkError = api.reportNetworkError;
export const flush = api.flush;
export const close = api.close;
export const getSessionId = api.getSessionId;
export const getTraceId = api.getTraceId;
export const setTraceId = api.setTraceId;
export default api;
