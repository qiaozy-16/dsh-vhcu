/**
 * @file coords.js — dynamic image-space to physical-space mapping.
 *
 * The model sees a resized PNG. Never infer a scale from DPI or a fixed
 * multiplier: pass the actual physical capture dimensions and the actual PNG
 * dimensions that were handed to the model. X and Y scales are independent so
 * a non-uniform resize can never silently use the wrong axis.
 */

export function parseFrameSize(value) {
  const match = /^(\d+)x(\d+)$/i.exec(String(value ?? '').trim());
  if (!match) return null;
  const width = Number(match[1]);
  const height = Number(match[2]);
  if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) {
    return null;
  }
  return { width, height };
}

export function computeImageScale(physicalWidth, physicalHeight, imageWidth, imageHeight) {
  const numbers = [physicalWidth, physicalHeight, imageWidth, imageHeight].map(Number);
  if (!numbers.every((value) => Number.isFinite(value) && value > 0)) {
    throw Object.assign(
      new Error('physical and image dimensions must be finite positive numbers'),
      { code: 'E_COORDINATES' },
    );
  }
  const [pw, ph, iw, ih] = numbers;
  const scaleX = pw / iw;
  const scaleY = ph / ih;
  if (!Number.isFinite(scaleX) || !Number.isFinite(scaleY) || scaleX <= 0 || scaleY <= 0) {
    throw Object.assign(new Error('dynamic image scale is invalid'), { code: 'E_COORDINATES' });
  }
  // Keep a legacy scalar only when the resize was uniform; callers that need
  // per-axis correctness must always read scaleX/scaleY.
  const scaleFactor = Math.abs(scaleX - scaleY) < 1e-9 ? scaleX : null;
  return { scaleX, scaleY, scaleFactor, physicalWidth: pw, physicalHeight: ph, imageWidth: iw, imageHeight: ih };
}

export function scaleImagePoint(x, y, scaleX, scaleY, originX = 0, originY = 0) {
  const px = Number(x);
  const py = Number(y);
  const sx = Number(scaleX);
  const sy = Number(scaleY);
  if (![px, py, sx, sy].every((value) => Number.isFinite(value)) || sx <= 0 || sy <= 0) {
    throw Object.assign(new Error('invalid image point or scale'), { code: 'E_COORDINATES' });
  }
  const ox = Number(originX);
  const oy = Number(originY);
  if (!Number.isFinite(ox) || !Number.isFinite(oy)) {
    throw Object.assign(new Error('invalid image origin'), { code: 'E_COORDINATES' });
  }
  return [Math.round(ox + px * sx), Math.round(oy + py * sy)];
}

export default computeImageScale;
