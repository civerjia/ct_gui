/*
 * Shared CT machine constants + math helpers.
 *
 * World coordinates (mm): origin = iso-center, +x right, +y up (screen y is
 * flipped on draw), angles in degrees, 0° = +x, 90° = +y, CCW positive.
 */

export const CT = {
  N_FILAMENTS: 96,
  R_SOURCE: 436 / 2,        // 218.0 mm (mutable: source-ring diameter / 2)
  R_DETECTOR: 308 / 2,      // 154.0 mm (mutable: detector-ring diameter / 2)
  COVERAGE: 35,             // filaments under the collimator
  DET_PIXELS: 256,
  DET_PIXEL_MM: 0.1,        // -> 25.6 mm panel
  get DET_WIDTH() { return this.DET_PIXELS * this.DET_PIXEL_MM; },
  get STEP_DEG() { return 360 / this.N_FILAMENTS; }, // 3.75°
  R_FOV: 50,                // scan field-of-view radius (mm), visual
  MAS_GAP: 6,               // gap from source ring to mAs band
  MAS_LEN: 24,              // mAs band depth (mm)
  BAND_GAP: 10,             // gap between mAs and V/I bands
  VI_LEN: 30,               // V/I band depth (mm)
};

export const D2R = Math.PI / 180;
export const mod = (n, m) => ((n % m) + m) % m;
export const clamp01 = (v) => (v < 0 ? 0 : v > 1 ? 1 : v);

// nominal (gantry = 0) angle of filament i. Filament 0 -> +y; index increases CW.
export const filamentBaseAngle = (i) => 90 - i * CT.STEP_DEG;
