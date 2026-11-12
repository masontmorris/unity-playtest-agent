// The capture worker: draws a grabbed frame, samples it for visibility when asked, and encodes it as JPEG,
// off the page's main thread (see capture.js). One message in, one answer out: `{ visible, jpeg }` with the
// JPEG as base64, or `{ error }`.

self.onmessage = async ({ data: { image, width, height, checkVisible, quality } }) => {
  try {
    const canvas = new OffscreenCanvas(width, height);
    const context = canvas.getContext("2d", { willReadFrequently: true });
    context.drawImage(image, 0, 0, width, height);
    let visible = null;
    if (checkVisible) {
      const sample = context.getImageData(0, 0, width, height).data;
      visible = false;
      for (let i = 3; i < sample.length; i += 4 * 97) {
        if (sample[i] !== 0) { visible = true; break; }
      }
    }
    const blob = await canvas.convertToBlob({ type: "image/jpeg", quality });
    // A browser that cannot encode JPEG answers with another type.
    if (blob.type !== "image/jpeg") throw new Error("the canvas could not be encoded as JPEG");
    self.postMessage({ visible, jpeg: base64(new Uint8Array(await blob.arrayBuffer())) });
  } catch (error) {
    self.postMessage({ error: error?.message ?? String(error) });
  } finally {
    image.close();
  }
};

function base64(bytes) {
  if (typeof bytes.toBase64 === "function") return bytes.toBase64();
  let text = "";
  for (let i = 0; i < bytes.length; i += 0x8000) text += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(text);
}
