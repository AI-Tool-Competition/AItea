import { afterEach, expect, it, rs } from "@rstest/core";

import { createStreamPublisher } from "@/core/tea/stream-publisher";

afterEach(() => {
  rs.useRealTimers();
  rs.unstubAllGlobals();
});

it("publishes a burst once at the next paint and cancels its fallback", () => {
  rs.useFakeTimers();
  let frame!: FrameRequestCallback;
  const request = rs.fn((callback: FrameRequestCallback) => {
    frame = callback;
    return 1;
  });
  rs.stubGlobal("requestAnimationFrame", request);
  rs.stubGlobal("cancelAnimationFrame", rs.fn());
  const publish = rs.fn();
  const stream = createStreamPublisher(publish);
  for (let i = 0; i < 100; i++) stream.schedule();
  expect(publish).not.toHaveBeenCalled();
  expect(request).toHaveBeenCalledTimes(1);
  frame(16);
  expect(publish).toHaveBeenCalledTimes(1);
  rs.advanceTimersByTime(100);
  expect(publish).toHaveBeenCalledTimes(1);
});

it("updates paused background frames within a bounded interval and clears pending work on stop", () => {
  rs.useFakeTimers();
  rs.stubGlobal(
    "requestAnimationFrame",
    rs.fn(() => 1),
  );
  const cancel = rs.fn();
  rs.stubGlobal("cancelAnimationFrame", cancel);
  const publish = rs.fn();
  const stream = createStreamPublisher(publish);
  stream.schedule();
  rs.advanceTimersByTime(50);
  expect(publish).toHaveBeenCalledTimes(1);
  expect(cancel).toHaveBeenCalledWith(1);
  stream.schedule();
  stream.cancel();
  rs.advanceTimersByTime(100);
  expect(publish).toHaveBeenCalledTimes(1);
});

it("flushes pending text immediately for first output or a tool boundary without a duplicate paint", () => {
  rs.useFakeTimers();
  rs.stubGlobal("requestAnimationFrame", undefined);
  const publish = rs.fn();
  const stream = createStreamPublisher(publish);
  stream.schedule();
  stream.flush();
  expect(publish).toHaveBeenCalledTimes(1);
  rs.advanceTimersByTime(100);
  expect(publish).toHaveBeenCalledTimes(1);
});
