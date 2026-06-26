// Lane retry harness: a PROGRESSING, non-bloating, bounded acceptance-test loop.
// Reflexion-style (learn from the failed test) but lesson-only (no accumulated history,
// which Reflexion warns grows cost ~quadratically) + a progress-gate (stop on stagnation,
// not a blunt iteration cap) + the caller's budgetUsd cap. Pure logic; deps are injected
// so it is unit-testable without DeepSeek or a shell.
export async function runHarness({ runAttempt, runTest, maxAttempts = 4 }) {
  let prev = null;          // { failCount, errorSig } of the previous attempt
  let lesson = null;        // short lesson carried to the next attempt
  let testResult = null;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    await runAttempt(lesson);
    testResult = await runTest();
    if (testResult.ok) return { status: "pass", attempts: attempt, lastLesson: lesson, testResult };
    // progress-gate: did we make measurable progress vs the previous attempt?
    if (prev !== null) {
      const progressed = testResult.failCount < prev.failCount || testResult.errorSig !== prev.errorSig;
      if (!progressed) {
        return { status: "stagnated", attempts: attempt, lastLesson: lesson, testResult };
      }
    }
    // lesson-only carry: a SHORT lesson for the next attempt (caller rebuilds fresh context)
    lesson = `Previous attempt failed: ${testResult.failCount} test(s) failing — ${testResult.errorSig}. Fix the cause, do not repeat the same change.`;
    prev = { failCount: testResult.failCount, errorSig: testResult.errorSig };
  }
  return { status: "exhausted", attempts: maxAttempts, lastLesson: lesson, testResult };
}
