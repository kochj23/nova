#!/usr/bin/env python3
"""nova_make_part.py — runs INSIDE the build123d venv (python@3.12).

Execs LLM-generated build123d source, exports an STL, validates the geometry
with trimesh (watertight / bbox / volume), and renders a PNG preview. Emits one
JSON line on stdout so the orchestrator (nova_make.py, system python) can read it.

Untrusted-code note: the generated source is exec'd here, in a throwaway venv
process with no secrets in the environment. The orchestrator runs it with
`env -u PYTHONPATH` so it can't reach the shared /Volumes/Data package dir.

Usage: nova_make_part.py <src.py> <out.stl> <out.png>
"""
import json
import sys
import traceback


def render(m, png_path):
    """Cheap CPU render of the mesh (no GPU/OpenGL needed) for the vision check."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    fig = plt.figure(figsize=(4, 4))
    ax = fig.add_subplot(111, projection="3d")
    ax.add_collection3d(Poly3DCollection(
        m.triangles, facecolor="#86b8e6", edgecolor="k", linewidths=0.1, alpha=0.95))
    b = m.bounds
    ax.set_xlim(b[0][0], b[1][0]); ax.set_ylim(b[0][1], b[1][1]); ax.set_zlim(b[0][2], b[1][2])
    ax.set_box_aspect(tuple(m.extents))
    ax.view_init(elev=25, azim=-50)
    ax.set_axis_off()
    plt.tight_layout()
    plt.savefig(png_path, dpi=110)
    plt.close()


def main():
    src_path, stl_path, png_path = sys.argv[1], sys.argv[2], sys.argv[3]
    out = {"ok": False}
    # 1. exec generated build123d code -> STL
    try:
        import build123d as b3d
        import runpy
        # Generated build123d code is run as its own module namespace (same as the
        # former exec(compile(...), ns)); its globals come back as `ns`.
        ns = runpy.run_path(src_path, run_name="nova_make_generated")
        obj = ns.get("result", ns.get("part"))
        if obj is None:
            raise ValueError("generated code must assign the final solid to `result`")
        if hasattr(obj, "part"):       # BuildPart context object -> its .part
            obj = obj.part
        b3d.export_stl(obj, stl_path)
    except Exception:
        out["error"] = traceback.format_exc(limit=5)
        print(json.dumps(out)); return
    # 2. validate geometry + render
    try:
        import trimesh
        m = trimesh.load(stl_path, force="mesh")
        out.update(
            ok=True,
            watertight=bool(m.is_watertight),
            winding_consistent=bool(m.is_winding_consistent),
            dims_mm=[round(float(x), 2) for x in m.extents],
            volume_mm3=round(float(m.volume), 2) if m.is_watertight else None,
            faces=int(len(m.faces)),
        )
        try:
            render(m, png_path); out["render"] = png_path
        except Exception as e:
            out["render_error"] = str(e)
    except Exception:
        out["ok"] = False
        out["error"] = traceback.format_exc(limit=5)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
