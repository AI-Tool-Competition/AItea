/** Coalesce token bursts into a paint, with a bound when animation frames pause. */
export function createStreamPublisher(publish: () => void) {
  let frame: number | undefined;
  let timeout: ReturnType<typeof setTimeout> | undefined;
  const cancel = () => {
    if (frame !== undefined) cancelAnimationFrame(frame);
    clearTimeout(timeout);
    frame = undefined;
    timeout = undefined;
  };
  const flush = () => {
    cancel();
    publish();
  };
  return {
    cancel,
    flush,
    schedule() {
      if (timeout !== undefined) return;
      if (typeof requestAnimationFrame === "function")
        frame = requestAnimationFrame(flush);
      // Background tabs may suspend rAF; do not retain an unbounded UI backlog.
      timeout = setTimeout(flush, 50);
    },
  };
}
