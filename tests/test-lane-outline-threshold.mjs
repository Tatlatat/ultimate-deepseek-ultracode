// Verifies the shim resolves a SMALLER outline threshold ONLY when the env var is a
// positive int, and otherwise passes undefined (engine default 64 KiB = today). We test
// the pure resolver, not a live DeepSeek call.
import assert from "node:assert";

// The shim must export resolveOutlineThreshold(env) returning a positive int or undefined.
const { resolveOutlineThreshold } = await import("../engine/lane-opts.mjs");

let p = 0, f = 0;
const chk = (c, m) => { if (c) { p++; console.log("  ok  ", m); } else { f++; console.log("  FAIL", m); } };

chk(resolveOutlineThreshold({}) === undefined, "unset -> undefined (engine default, byte-inert)");
chk(resolveOutlineThreshold({ REASONIX_LANE_OUTLINE_THRESHOLD_BYTES: "" }) === undefined, "empty -> undefined");
chk(resolveOutlineThreshold({ REASONIX_LANE_OUTLINE_THRESHOLD_BYTES: "0" }) === undefined, "0 -> undefined (no zero/negative cap)");
chk(resolveOutlineThreshold({ REASONIX_LANE_OUTLINE_THRESHOLD_BYTES: "-5" }) === undefined, "negative -> undefined");
chk(resolveOutlineThreshold({ REASONIX_LANE_OUTLINE_THRESHOLD_BYTES: "abc" }) === undefined, "non-numeric -> undefined");
chk(resolveOutlineThreshold({ REASONIX_LANE_OUTLINE_THRESHOLD_BYTES: "32768" }) === 32768, "32768 -> 32768");
chk(resolveOutlineThreshold({ REASONIX_LANE_OUTLINE_THRESHOLD_BYTES: "32abc" }) === undefined, "32abc -> undefined (strict, reject mixed alphanum)");
chk(resolveOutlineThreshold({ REASONIX_LANE_OUTLINE_THRESHOLD_BYTES: "32768.5" }) === undefined, "32768.5 -> undefined (strict, reject float)");
chk(resolveOutlineThreshold({ REASONIX_LANE_OUTLINE_THRESHOLD_BYTES: " 32768 " }) === 32768, " 32768  -> 32768 (strict, trim whitespace)");
console.log(`\n${p} passed, ${f} failed`);
process.exit(f ? 1 : 0);
