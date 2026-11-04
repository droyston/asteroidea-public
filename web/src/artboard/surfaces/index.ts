// The surface registry: every artboard the harness renders. A surface becomes real by replacing
// `render` and setting `pending: null`; a new surface is added here.
import type { Surface } from "../harness";
import { findSurface } from "./find";
import { homeSurfaces } from "./home";
import { inspectSurface } from "./inspect";
import { participantSurfaces } from "./participant";
import { resultsSurfaces } from "./results";
import { sessionSurfaces } from "./session";
import { shell } from "./shell";
import { signalsSurfaces } from "./signals";
import { statesSurfaces } from "./states";
import { studySurfaces } from "./study";

export const SURFACES: readonly Surface[] = [
  shell,
  ...homeSurfaces,
  ...studySurfaces,
  ...participantSurfaces,
  ...sessionSurfaces,
  inspectSurface,
  ...signalsSurfaces,
  findSurface,
  ...resultsSurfaces,
  ...statesSurfaces,
];
