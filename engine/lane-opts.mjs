// A1a: Resolve the outline threshold for code files read by a lane.
// When REASONIX_LANE_OUTLINE_THRESHOLD_BYTES is a positive int, a lane reading a
// file larger than it gets the engine's outline (metadata+head+symbol outline) instead
// of the full raw dump — the mechanical fix for lanes that ingest too many files and
// time out. Unset/invalid -> undefined -> engine default 64 KiB (today's behavior).
export function resolveOutlineThreshold(env) {
  const raw = ((env && env.REASONIX_LANE_OUTLINE_THRESHOLD_BYTES) || "").trim();
  if (!/^\d+$/.test(raw)) return undefined;   // only pure digits
  const n = Number(raw);
  return Number.isInteger(n) && n > 0 ? n : undefined;
}
