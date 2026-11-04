import { describe, expect, it, vi } from "vitest";
import {
  bytesPerVoxel,
  fetchVolume,
  formatIntensity,
  fromWindowLevel,
  MAX_COMPRESSED_BYTES,
  mmSearch,
  parseMm,
  refusal,
  toWindowLevel,
  volumeHeader,
} from "./mri";

const T1 = { shape: [256, 256, 176], datatype: "int16" };

describe("refusal", () => {
  it("lets a plain 3D T1 through", () => {
    expect(refusal(T1, 10.7 * 2 ** 20)).toBeNull();
  });

  it("refuses 4D with its shape and the functional-run text", () => {
    const text = refusal({ shape: [64, 64, 40, 300], datatype: "int16" }, 5 * 2 ** 20);
    expect(text).toContain("64 x 64 x 40 x 300");
    expect(text).toContain("Functional runs are not viewable yet");
  });

  it("treats a trailing length-1 axis as 3D", () => {
    expect(refusal({ shape: [64, 64, 40, 1], datatype: "int16" }, 1000)).toBeNull();
  });

  it("refuses over 64 MiB compressed, and not at exactly 64 MiB", () => {
    expect(refusal(T1, MAX_COMPRESSED_BYTES)).toBeNull();
    expect(refusal(T1, MAX_COMPRESSED_BYTES + 1)).toContain("compressed");
  });

  it("refuses over 512 MiB decompressed from shape x bytes per voxel", () => {
    // 512 x 512 x 512 float32 = 512 MiB exactly: allowed; one more slice: refused.
    expect(refusal({ shape: [512, 512, 512], datatype: "float32" }, 1000)).toBeNull();
    expect(refusal({ shape: [512, 512, 513], datatype: "float32" }, 1000)).toContain(
      "decompressed",
    );
    expect(refusal({ shape: [1024, 1024, 1024], datatype: "uint8" }, 1000)).toContain(
      "decompressed",
    );
  });

  it("decides nothing it cannot measure", () => {
    expect(refusal({ shape: null, datatype: null }, null)).toBeNull();
    expect(refusal({ shape: [4096, 4096, 4096], datatype: null }, null)).toBeNull();
  });

  it("orders 4D before size", () => {
    expect(refusal({ shape: [64, 64, 40, 300], datatype: "int16" }, 2 ** 30)).toContain("4D");
  });
});

describe("header and dtype", () => {
  it("reads shape and datatype from the catalog row", () => {
    expect(volumeHeader({ nifti_shape: [1, 2, 3], nifti_datatype: "int16" })).toEqual({
      shape: [1, 2, 3],
      datatype: "int16",
    });
    expect(volumeHeader({ nifti_shape: "x" })).toEqual({ shape: null, datatype: null });
    expect(volumeHeader(undefined)).toEqual({ shape: null, datatype: null });
  });

  it("maps numpy names to bytes per voxel", () => {
    expect(bytesPerVoxel("uint8")).toBe(1);
    expect(bytesPerVoxel("int16")).toBe(2);
    expect(bytesPerVoxel("float32")).toBe(4);
    expect(bytesPerVoxel("complex64")).toBe(8);
    expect(bytesPerVoxel("float64")).toBe(8);
    expect(bytesPerVoxel("object")).toBeNull();
    expect(bytesPerVoxel(null)).toBeNull();
  });
});

describe("mm URL state", () => {
  it("parses three finite numbers", () => {
    expect(parseMm("?tab=image&mm=-14.75,1.9,11.64")).toEqual([-14.75, 1.9, 11.64]);
  });

  it("rejects anything else", () => {
    for (const bad of [
      "",
      "?mm=",
      "?mm=1,2",
      "?mm=1,2,3,4",
      "?mm=a,b,c",
      "?mm=1,,3",
      "?mm=1,2,Infinity",
    ]) {
      expect(parseMm(bad)).toBeNull();
    }
  });

  it("writes two decimals and keeps other parameters", () => {
    expect(mmSearch("?tab=image", [-14.7516, 1.9056, 11.6439])).toBe(
      "?tab=image&mm=-14.75%2C1.91%2C11.64",
    );
  });

  it("round-trips what it writes", () => {
    const mm = [-14.75, 1.91, 11.64];
    expect(parseMm(mmSearch("", mm))).toEqual(mm);
  });

  it("removes mm on null and leaves a clean query", () => {
    expect(mmSearch("?mm=1,2,3", null)).toBe("");
    expect(mmSearch("?tab=image&mm=1,2,3", null)).toBe("?tab=image");
  });
});

describe("window and level", () => {
  it("is its own inverse", () => {
    const { level, window } = toWindowLevel(100, 300);
    expect(level).toBe(200);
    expect(window).toBe(200);
    expect(fromWindowLevel(level, window)).toEqual([100, 300]);
  });

  it("never lets the window drop below the floor", () => {
    expect(fromWindowLevel(10, 0, 2)).toEqual([9, 11]);
  });
});

function response(chunks: Uint8Array[], headers: Record<string, string>, status = 200): Response {
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      for (const chunk of chunks) c.enqueue(chunk);
      c.close();
    },
  });
  return new Response(body, { status, headers });
}

describe("fetchVolume", () => {
  it("reads the stream whole, reports progress, sends no credentials", async () => {
    const fetchFn = vi.fn(async (_url: string, _init: RequestInit) =>
      response([new Uint8Array([1, 2]), new Uint8Array([3])], { "content-length": "3" }),
    );
    const seen: number[] = [];
    const got = await fetchVolume("https://x.test/a?sig=1", fetchFn, (p) => {
      expect(p.total).toBe(3);
      seen.push(p.loaded);
    });
    expect(got.kind).toBe("loaded");
    if (got.kind === "loaded") expect([...new Uint8Array(got.buffer)]).toEqual([1, 2, 3]);
    expect(seen).toEqual([2, 3]);
    expect(fetchFn).toHaveBeenCalledTimes(1);
    expect(fetchFn.mock.calls[0]?.[1].credentials).toBe("omit");
  });

  it("refuses on a Content-Length over the limit without reading", async () => {
    const got = await fetchVolume(
      "https://x.test/a",
      async () =>
        response([new Uint8Array(1)], { "content-length": String(MAX_COMPRESSED_BYTES + 1) }),
      () => undefined,
    );
    expect(got.kind).toBe("refused");
  });

  it("reports a non-200 and a thrown fetch as errors", async () => {
    const bad = await fetchVolume(
      "https://x.test/a",
      async () => response([], {}, 403),
      () => undefined,
    );
    expect(bad).toEqual({ kind: "error", message: "The download failed (HTTP 403)." });
    const thrown = await fetchVolume(
      "https://x.test/a",
      async () => {
        throw new TypeError("Failed to fetch");
      },
      () => undefined,
    );
    expect(thrown.kind).toBe("error");
  });

  it("has no total when Content-Length is absent", async () => {
    let total: number | null | undefined;
    await fetchVolume(
      "https://x.test/a",
      async () => response([new Uint8Array(2)], {}),
      (p) => {
        total = p.total;
      },
    );
    expect(total).toBeNull();
  });
});

describe("formatIntensity", () => {
  it("rounds a float to 4 significant digits", () => {
    expect(formatIntensity(69404.7890625)).toBe("69400");
    expect(formatIntensity(0.123456)).toBe("0.1235");
    expect(formatIntensity(-7.12345)).toBe("-7.123");
  });

  it("leaves integers as they are", () => {
    expect(formatIntensity(100)).toBe("100");
    expect(formatIntensity(123456)).toBe("123456");
    expect(formatIntensity(0)).toBe("0");
  });
});
