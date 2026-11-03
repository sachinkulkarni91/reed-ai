/**
 * Injected into the meeting tab by the Playwright bot.
 *
 * Captures the tab's audio via getDisplayMedia({preferCurrentTab}) - Chromium is
 * launched with --auto-accept-this-tab-capture so no picker appears - and buffers
 * WebM/Opus chunks in memory as base64 strings for the Python side to drain via
 * page.evaluate().
 *
 * This does NOT open a WebSocket from inside the page. Meeting sites set their own
 * Content-Security-Policy, and a strict connect-src (Microsoft Teams' is a hard
 * allow-list of its own domains) blocks any outbound connection the page itself
 * tries to make - including one we inject. page.evaluate() runs over the DevTools
 * protocol, which is not a page-initiated network request, so CSP does not apply
 * to it; only the Python process ever opens the real ingest connection. Buffering
 * as base64 costs ~33% size but chunks are tiny (a few KB every 250ms), so this is
 * a non-issue in practice.
 *
 * window.__reed exposes state the Python side polls: running, buffered chunk
 * count, any capture error, and a stop() the bot calls on the way out. drain()
 * hands back everything buffered since the last call and clears the buffer.
 */
async function reedStartCapture(chunkMs) {
  if (window.__reed && window.__reed.running) return "already-running";

  const state = {
    running: false,
    bytesBuffered: 0,
    chunks: 0,
    error: null,
    startedAt: Date.now(),
    _queue: [],
    _ended: false,
    stop: () => {},
    drain: () => {
      const out = state._queue;
      state._queue = [];
      return { chunks: out, ended: state._ended };
    },
  };
  window.__reed = state;

  let stream;
  try {
    // Video must be requested for tab capture to be offered; we drop the track
    // immediately and only ever record audio.
    stream = await navigator.mediaDevices.getDisplayMedia({
      video: true,
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      preferCurrentTab: true,
      systemAudio: "include",
      selfBrowserSurface: "include",
    });
  } catch (err) {
    state.error = "getDisplayMedia failed: " + err.message;
    return state.error;
  }

  stream.getVideoTracks().forEach((t) => t.stop());
  const audioTracks = stream.getAudioTracks();
  if (audioTracks.length === 0) {
    state.error = "tab capture returned no audio track";
    return state.error;
  }

  const audioStream = new MediaStream(audioTracks);

  function bufferToBase64(buffer) {
    let binary = "";
    const bytes = new Uint8Array(buffer);
    const chunkSize = 0x8000; // avoid call-stack limits on String.fromCharCode
    for (let i = 0; i < bytes.length; i += chunkSize) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
    }
    return btoa(binary);
  }

  const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
    ? "audio/webm;codecs=opus"
    : "audio/webm";
  const recorder = new MediaRecorder(audioStream, {
    mimeType: mime,
    audioBitsPerSecond: 64000,
  });

  recorder.ondataavailable = async (event) => {
    if (!event.data || event.data.size === 0) return;
    const buffer = await event.data.arrayBuffer();
    state.bytesBuffered += buffer.byteLength;
    state.chunks += 1;
    state._queue.push(bufferToBase64(buffer));
  };

  recorder.onstop = () => {
    state.running = false;
    state._ended = true;
    audioStream.getTracks().forEach((t) => t.stop());
  };

  state.stop = () => {
    if (recorder.state !== "inactive") recorder.stop();
  };

  recorder.start(chunkMs || 250);
  state.running = true;
  return "started";
}
