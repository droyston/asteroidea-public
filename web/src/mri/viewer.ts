// The one module that imports NiiVue. Only `Volume` (mri/Volume.tsx) reaches it, through `import()`,
// so the library lands in its own chunk and the main chunk is unchanged. The volume arrives as bytes
// (the app fetched the presigned URL once); NiiVue never touches the network here.
//
// World coordinates: the crosshair reads as `frac2mm(crosshairPos, 0, true)`. The force flag
// is what returns the file's own affine (sform, else qform); without it NiiVue returns its oblique-corrected,
// axis-aligned mm. The readout names the voxel's centre, so the mm, the ijk and the intensity agree.
import { Niivue } from "@niivue/niivue";

export type Layout = "grid" | "row" | "column";
const LAYOUT_CODE: Record<Layout, number> = { column: 1, grid: 2, row: 3 };

type Vec3 = [number, number, number];

export interface Readout {
  mm: Vec3;
  vox: Vec3;
  value: number | null;
}

export interface Viewer {
  /** The NiiVue instance, for the e2e probe (hung on the canvas as `astMri`). */
  nv: Niivue;
  /** The volume's own display range at load, and its full intensity range. */
  initial: { min: number; max: number };
  full: { min: number; max: number };
  readout(): Readout;
  setMm(mm: readonly number[]): void;
  setLayout(layout: Layout): void;
  setRange(min: number, max: number): void;
  /** Move the crosshair by whole voxels. */
  nudge(dx: number, dy: number, dz: number): void;
  reset(): void;
  dispose(): void;
}

function triple(v: ArrayLike<number>): Vec3 {
  return [v[0] ?? 0, v[1] ?? 0, v[2] ?? 0];
}

export async function openViewer(
  canvas: HTMLCanvasElement,
  buffer: ArrayBuffer,
  name: string,
  onMove: (r: Readout) => void,
  colours: { crosshair: number[]; back: number[] },
): Promise<Viewer> {
  const nv = new Niivue({
    crosshairColor: colours.crosshair,
    // A thin line in percent of the field of view: the default (one voxel) is 16 px on a 4 mm fixture.
    crosshairWidth: 0.4,
    crosshairWidthUnit: "percent",
    isColorbar: false,
    isOrientCube: false,
    show3Dcrosshair: false,
    multiplanarShowRender: 0,
    backColor: colours.back,
  });
  await nv.attachToCanvas(canvas);
  await nv.loadFromArrayBuffer(buffer, name);
  const vol = nv.volumes[0];
  if (vol === undefined) throw new Error("NiiVue loaded no volume from the file");
  nv.setSliceMM(true);
  nv.setSliceType(nv.sliceTypeMultiplanar);
  nv.setMultiplanarLayout(LAYOUT_CODE.grid);
  const initial = { min: vol.cal_min ?? 0, max: vol.cal_max ?? 1 };
  const full = { min: vol.global_min ?? initial.min, max: vol.global_max ?? initial.max };

  const dims = [vol.dims?.[1] ?? 1, vol.dims?.[2] ?? 1, vol.dims?.[3] ?? 1];
  const voxelAt = (frac: ArrayLike<number>): Vec3 => {
    const v = triple(nv.frac2vox([frac[0] ?? 0, frac[1] ?? 0, frac[2] ?? 0], 0));
    return v.map((n, a) => Math.min((dims[a] ?? 1) - 1, Math.max(0, Math.round(n)))) as Vec3;
  };
  /** Put the crosshair on a voxel centre, so the readout names one voxel and that voxel's centre mm. */
  const place = (vox: Vec3) => {
    nv.scene.crosshairPos = triple(nv.vox2frac(vox, 0));
  };
  const centre = () => place(dims.map((n) => Math.floor(n / 2)) as Vec3);
  const readout = (): Readout => {
    const [i, j, k] = voxelAt(nv.scene.crosshairPos);
    const mm = triple(nv.frac2mm(nv.vox2frac([i, j, k], 0), 0, true));
    const value = Number.isFinite(vol.getValue(i, j, k)) ? vol.getValue(i, j, k) : null;
    return { mm, vox: [i, j, k], value };
  };
  const emit = () => {
    const vox = voxelAt(nv.scene.crosshairPos);
    const want = nv.vox2frac(vox, 0);
    const have = nv.scene.crosshairPos;
    if ([0, 1, 2].some((a) => Math.abs((want[a] ?? 0) - (have[a] ?? 0)) > 1e-6)) {
      place(vox);
      nv.drawScene();
    }
    onMove(readout());
  };
  nv.onLocationChange = emit;
  const redraw = () => {
    nv.drawScene();
    emit();
  };
  centre();
  redraw();

  return {
    nv,
    initial,
    full,
    readout,
    setMm(mm) {
      nv.scene.crosshairPos = nv.mm2frac([mm[0] ?? 0, mm[1] ?? 0, mm[2] ?? 0], 0, true);
      redraw();
    },
    setLayout(layout) {
      nv.setMultiplanarLayout(LAYOUT_CODE[layout]);
    },
    setRange(min, max) {
      vol.cal_min = min;
      vol.cal_max = max;
      nv.updateGLVolume();
    },
    nudge(dx, dy, dz) {
      nv.moveCrosshairInVox(dx, dy, dz);
      emit();
    },
    reset() {
      vol.cal_min = initial.min;
      vol.cal_max = initial.max;
      nv.setMultiplanarLayout(LAYOUT_CODE.grid);
      nv.updateGLVolume();
      centre();
      redraw();
    },
    dispose() {
      nv.onLocationChange = () => undefined;
      nv.gl?.getExtension("WEBGL_lose_context")?.loseContext();
    },
  };
}
